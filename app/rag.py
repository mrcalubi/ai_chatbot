import os
import re
from typing import List, Tuple, Dict, Iterable, Optional

import numpy as np
import faiss
from rank_bm25 import BM25Okapi

# Config (via environment)
OCR_ENABLED = (os.getenv("OCR_ENABLED", "false").lower() == "true")
OCR_DPI = int(os.getenv("OCR_DPI", "220"))  # used if OCR is enabled

# Text cleanup
_WS = re.compile(r"\s+")
def _clean_text(t: Optional[str]) -> str:
    return _WS.sub(" ", (t or "")).strip()

# PDF extraction (+ optional OCR fallback)
def _ocr_pages_if_needed(path: str, page_idxs_needing_ocr: List[int]) -> Dict[int, str]:
    """
    Returns {page_index(0-based): ocr_text} for requested pages.
    Uses pdf2image + pytesseract only if OCR_ENABLED and libs are available.
    Silently skips OCR if not available.
    """
    out: Dict[int, str] = {}
    if not OCR_ENABLED or not page_idxs_needing_ocr:
        return out
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except Exception:
        # OCR libs not installed; skip
        return out

    # Render all pages; cheaper than multiple conversions in many cases
    try:
        images = convert_from_path(path, dpi=OCR_DPI)
    except Exception:
        return out

    for idx in page_idxs_needing_ocr:
        if 0 <= idx < len(images):
            try:
                txt = pytesseract.image_to_string(images[idx]) or ""
                out[idx] = _clean_text(txt)
            except Exception:
                pass
    return out

def extract_pdf_pages(path: str) -> Iterable[Dict]:
    """
    Yields dicts: {"text": <page text>, "page_number": <1-based int>}
    - Extracts digital text via PyPDF first.
    - If OCR is enabled, runs OCR on pages whose extracted text is too short.
    """
    reader = PdfReader(path)
    pages_raw: List[str] = []

    # 1) Try digital text extraction
    for pg in reader.pages:
        try:
            txt = pg.extract_text() or ""
        except Exception:
            txt = ""
        pages_raw.append(_clean_text(txt))

    # 2) Optional OCR for pages that look empty/very short
    need_ocr: List[int] = [i for i, t in enumerate(pages_raw) if len(t) < 40]
    if need_ocr:
        ocr_map = _ocr_pages_if_needed(path, need_ocr)
        for i, txt in ocr_map.items():
            if len(txt) > len(pages_raw[i]):
                pages_raw[i] = txt

    # 3) Yield cleaned pages
    for i, txt in enumerate(pages_raw, start=1):
        yield {"text": txt, "page_number": i}

# -----------------------------
# Chunking (page-merged with overlap)
# -----------------------------
def chunk_pages(pages: List[Dict], max_chars: int = 3500, overlap: int = 400):
    """
    Merge consecutive PDF pages into larger overlapping chunks.
    Returns (chunks: List[str], metas: List[{'page': start_page}])
    """
    chunks: List[str] = []
    metas: List[Dict] = []
    buf: List[str] = []
    start_page: Optional[int] = None
    total = 0

    for p in pages:
        text = p.get("text", "")
        page_no = p.get("page_number")
        if not text:
            continue

        if start_page is None:
            start_page = page_no

        buf.append(f"[p{page_no}] {text}")
        total += len(text)

        if total >= max_chars:
            joined = "\n".join(buf)
            chunks.append(joined)
            metas.append({"page": start_page})
            # overlap tail
            tail = joined[max(0, len(joined) - overlap):]
            buf = [tail]
            total = len(tail)
            start_page = page_no

    if buf:
        joined = "\n".join(buf)
        chunks.append(joined)
        metas.append({"page": start_page})

    return chunks, metas

# -----------------------------
# Dual-tier chunking (big + small)
# -----------------------------
def chunk_pages_dual(pages: List[Dict],
                     max_chars_big: int = 1800, overlap_big: int = 220,
                     max_chars_small: int = 480, overlap_small: int = 80):
    big_chunks, big_meta = [], []
    small_chunks, small_meta = [], []
    buf, start_page, total = [], None, 0

    for p in pages:
        text = p.get("text", "") or ""
        page = p.get("page_number")
        if not text:
            continue
        if start_page is None:
            start_page = page

        page_block = f"[p{page}] {text}"
        # big track
        buf.append(page_block)
        total += len(page_block)

        # small slices per page
        t = page_block
        i = 0
        while i < len(t):
            piece = t[i:i + max_chars_small]
            small_chunks.append(piece)
            small_meta.append({"page": page})
            if len(piece) < max_chars_small:
                break
            i += max_chars_small - overlap_small

        # flush big
        if total >= max_chars_big:
            joined = "\n".join(buf)
            big_chunks.append(joined)
            big_meta.append({"page": start_page})
            tail = joined[max(0, len(joined) - overlap_big):]
            buf, total, start_page = [tail], len(tail), page

    if buf:
        joined = "\n".join(buf)
        big_chunks.append(joined)
        big_meta.append({"page": start_page})

    return (big_chunks, big_meta), (small_chunks, small_meta)

# -----------------------------
# Fusion & Diversity helpers
# -----------------------------
def rrf_fuse(bm25_hits: List[Tuple[int, float]],
             dense_hits: List[Tuple[int, float]],
             k: int = 50,
             c: int = 60) -> List[Tuple[int, float]]:
    """
    Reciprocal Rank Fusion (RRF).
    Inputs: lists of (index, score) in ranked order.
    Output: list of (index, fused_score) truncated to k.
    """
    ranks: Dict[int, float] = {}
    # preserve order ranking (1-based)
    for hits in (bm25_hits, dense_hits):
        for r, (idx, _score) in enumerate(hits, start=1):
            ranks[idx] = ranks.get(idx, 0.0) + 1.0 / (c + r)
    fused = sorted(ranks.items(), key=lambda x: x[1], reverse=True)[:k]
    return [(i, float(s)) for i, s in fused]

def mmr_select(query_vec: np.ndarray,
               cand_embs: np.ndarray,
               lam: float = 0.7,
               topn: int = 12) -> List[int]:
    """
    Maximal Marginal Relevance selection.
    Returns indices into cand_embs in selected order.
    """
    if cand_embs.size == 0:
        return []
    # cosine sims (already normalized by _encode)
    q = query_vec.reshape(1, -1)
    sims = (cand_embs @ q.T).ravel()  # relevance to query
    selected: List[int] = []
    remaining = list(range(cand_embs.shape[0]))

    while remaining and len(selected) < topn:
        if not selected:
            # pick most relevant first
            j = max(remaining, key=lambda i: sims[i])
            selected.append(j)
            remaining.remove(j)
            continue
        # compute redundancy vs. already selected
        sel_embs = cand_embs[selected]
        # max similarity to any selected
        red = np.max(cand_embs[remaining] @ sel_embs.T, axis=1)
        # mmr score
        mmr_scores = lam * sims[remaining] - (1 - lam) * red
        j_rel = int(np.argmax(mmr_scores))
        j = remaining[j_rel]
        selected.append(j)
        remaining.remove(j)

    return selected

# -----------------------------
# Vector store (FAISS + ST + BM25 hybrid)
# -----------------------------
class VectorStore:
    """
    FAISS + SentenceTransformers cosine-sim store (dense)
    + BM25 (sparse) hybrid.
    Persists index + texts + metadata on disk and keeps them in sync.
    """
    def __init__(self, index_dir: str, embed_model: str):
        self.index_dir = index_dir
        os.makedirs(index_dir, exist_ok=True)

        self.model = SentenceTransformer(embed_model)
        self.dim = self.model.get_sentence_embedding_dimension()

        self.index_path = os.path.join(index_dir, "faiss.index")
        self.meta_path = os.path.join(index_dir, "meta.npy")
        self.text_path = os.path.join(index_dir, "texts.npy")

        self._load()

        # BM25 bits
        self._tokenizer = re.compile(r"[A-Za-z0-9%\-]+").findall
        self._bm25: Optional[BM25Okapi] = None
        self._bm25_tokens: List[List[str]] = []
        self._build_bm25()

    # ---------- BM25 helpers ----------
    def _build_bm25(self):
        corpus = self.texts or []
        self._bm25_tokens = [self._tokenizer((t or "").lower()) for t in corpus]
        self._bm25 = BM25Okapi(self._bm25_tokens) if self._bm25_tokens else None

    def _after_storage_change(self):
        # call whenever texts/meta/index changed
        self._build_bm25()
        self._save()

    # ---------- persistence ----------
    def _new_index(self):
        # Inner product + normalized vectors -> cosine similarity
        self.index = faiss.IndexFlatIP(self.dim)

    def _load(self):
        if os.path.exists(self.index_path):
            self.index = faiss.read_index(self.index_path)
            try:
                self.meta = np.load(self.meta_path, allow_pickle=True).tolist()
            except Exception:
                self.meta = []
            try:
                self.texts = np.load(self.text_path, allow_pickle=True).tolist()
            except Exception:
                self.texts = []

            # keep lengths consistent with index.ntotal
            n = min(self.index.ntotal, len(self.meta), len(self.texts))
            if n < self.index.ntotal:
                # safest reset if mismatch
                self._new_index()
            self.meta = self.meta[:n]
            self.texts = self.texts[:n]
        else:
            self._new_index()
            self.meta = []
            self.texts = []

    def remove_docs(self, docs: List[str]) -> int:
        """
        Remove all chunks belonging to the given doc filenames.
        Returns the number of chunks removed.
        """
        to_remove = set((docs or []))
        if not to_remove:
            return 0

        n_before = len(self.meta)
        keep_idx = [i for i, m in enumerate(self.meta) if (m or {}).get("doc") not in to_remove]

        # Nothing to remove
        if len(keep_idx) == n_before:
            return 0

        # Keep only remaining items
        self.texts = [self.texts[i] for i in keep_idx]
        self.meta  = [self.meta[i]  for i in keep_idx]

        # Rebuild FAISS from scratch for consistency
        self._new_index()
        if self.texts:
            embs = self._encode(self.texts)
            self.index.add(embs)

        # Rebuild BM25 + persist
        self._after_storage_change()

        return n_before - len(self.meta)


    def _save(self):
        faiss.write_index(self.index, self.index_path)
        np.save(self.meta_path, np.array(self.meta, dtype=object))
        np.save(self.text_path, np.array(self.texts, dtype=object))

    # ---------- encode ----------
    def _encode(self, texts: List[str]) -> np.ndarray:
        embs = self.model.encode(texts, normalize_embeddings=True)
        return np.asarray(embs, dtype="float32")

    # ---------- mutations ----------
    def add(self, chunks: List[str], metas: List[dict]):
        if not chunks:
            return
        # ensure metas length matches chunks
        if len(metas) != len(chunks):
            m = (metas or [])
            if len(m) < len(chunks):
                m = m + [{}] * (len(chunks) - len(m))
            metas = m[:len(chunks)]

        embs = self._encode(chunks)
        # build a new empty index if somehow missing
        if getattr(self, "index", None) is None:
            self._new_index()
        self.index.add(embs)
        self.texts.extend(chunks)
        self.meta.extend(metas)
        self._after_storage_change()

    # ---------- search (dense only) ----------
    def search(self, query: str, k: int = 6) -> List[Tuple[str, dict, float]]:
        n = len(self.texts or [])
        if n == 0:
            return []

        # cap k to available items (prevents FAISS -1 ids)
        k = max(1, min(k, n))

        # if index count drifted, rebuild safely
        if getattr(self.index, "ntotal", 0) != n:
            self._new_index()
            if n > 0:
                embs = self._encode(self.texts)
                self.index.add(embs)
                self._save()

        q = self._encode([query])
        try:
            D, I = self.index.search(q, k)
        except Exception:
            return []

        out: List[Tuple[str, dict, float]] = []
        seen = set()
        for idx, score in zip(I[0], D[0]):
            if idx is None or idx < 0:
                continue
            if idx >= len(self.texts) or idx >= len(self.meta):
                continue
            if idx in seen:
                continue
            seen.add(idx)
            out.append((self.texts[idx], self.meta[idx], float(score)))
        return out

    # ---------- search (hybrid: dense + BM25) ----------
    def search_hybrid(
        self,
        query: str,
        k: int = 12,
        alpha: float = 0.65,
        *,
        strategy: str = "alpha",  # "alpha" (blend) or "rrf"
        pre_k: Optional[int] = None,  # how many to consider before final k
        mmr_topn: Optional[int] = None,  # if set, apply MMR diversity
        mmr_lambda: float = 0.7,
    ) -> List[Tuple[str, dict, float]]:
        """
        Hybrid retrieval:
          - strategy="alpha": normalized linear blend (existing behavior)
          - strategy="rrf": Reciprocal Rank Fusion
          - Optionally apply MMR to diversify the final top-k.
        """
        n = len(self.texts or [])
        if n == 0:
            return []
        k = max(1, min(k, n))
        pre_k = max(k, 50) if pre_k is None else max(k, min(pre_k, n))

        # ensure FAISS index matches data
        if getattr(self.index, "ntotal", 0) != n:
            self._new_index()
            if n > 0:
                embs = self._encode(self.texts)
                self.index.add(embs)
                self._save()

        # ----- dense (top pre_k) -----
        dense_hits: List[Tuple[int, float]] = []
        try:
            q_vec = self._encode([query])  # shape (1, dim)
            D, I = self.index.search(q_vec, pre_k)
            for idx, score in zip(I[0], D[0]):
                if idx is not None and 0 <= idx < n:
                    dense_hits.append((int(idx), float(score)))
        except Exception:
            # dense fails -> leave empty
            q_vec = None  # type: ignore

        # ----- bm25 (top pre_k) -----
        bm25_hits: List[Tuple[int, float]] = []
        if self._bm25 is not None:
            toks = self._tokenizer(query.lower())
            scores = self._bm25.get_scores(toks)
            order = np.argsort(scores)[::-1][:pre_k]
            for idx in order:
                s = float(scores[idx])
                if s > 0:
                    bm25_hits.append((int(idx), s))

        # Fallback: if both failed, return []
        if not dense_hits and not bm25_hits:
            return []

        # ----- fusion -----
        fused_indices_scores: List[Tuple[int, float]]
        if strategy == "rrf":
            fused_indices_scores = rrf_fuse(bm25_hits=bm25_hits, dense_hits=dense_hits, k=pre_k, c=60)
        else:
            # normalized alpha blend (backward-compatible default)
            def _norm(pairs: List[Tuple[int, float]]):
                if not pairs:
                    return {}
                arr = np.array([s for _, s in pairs], dtype=float)
                lo, hi = float(arr.min()), float(arr.max())
                if hi - lo < 1e-9:
                    return {i: 1.0 for i, _ in pairs}
                return {i: (s - lo) / (hi - lo) for i, s in pairs}
            dn = _norm(dense_hits)
            bn = _norm(bm25_hits)
            combined: Dict[int, float] = {}
            for i, s in dn.items():
                combined[i] = combined.get(i, 0.0) + alpha * s
            for i, s in bn.items():
                combined[i] = combined.get(i, 0.0) + (1.0 - alpha) * s
            fused_indices_scores = sorted(combined.items(), key=lambda x: x[1], reverse=True)[:pre_k]

        # ----- optional MMR diversity -----
        cand_idxs = [i for i, _ in fused_indices_scores]
        cand_scores = {i: sc for i, sc in fused_indices_scores}

        if mmr_topn and mmr_topn > 0 and q_vec is not None:
            cand_texts = [self.texts[i] for i in cand_idxs]
            cand_embs = self._encode(cand_texts)  # (C, dim), normalized
            sel_local = mmr_select(q_vec[0], cand_embs, lam=mmr_lambda, topn=min(mmr_topn, k))
            sel_global = [cand_idxs[j] for j in sel_local]
            # build output in MMR order (use fused scores for reporting)
            out = [(self.texts[i], self.meta[i], float(cand_scores.get(i, 0.0))) for i in sel_global]
            return out[:k]

        # ----- no MMR: just take top-k fused -----
        out = [(self.texts[i], self.meta[i], float(cand_scores.get(i, 0.0))) for i in cand_idxs[:k]]
        return out
