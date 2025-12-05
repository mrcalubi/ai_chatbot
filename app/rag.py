# app/rag.py
# -----------------------------------------------------------------------------
# PDF Q&A Retrieval Core
# - Page text extraction WITHOUT pre-snipping table PNGs (lazy PNGs handled
#   via /tables/png endpoint in main.py).
# - Table indexing: adds table TEXT as chunks with meta {type:"table", bbox,...}
# - Hybrid retrieval (FAISS dense + BM25), HyDE query expansion, optional
#   CrossEncoder re-rank, and MMR diversity.
# - No strict/evidence-only mode logic here; normal-mode uses these scores.
# -----------------------------------------------------------------------------

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Tuple, Dict, Optional

import numpy as np
import faiss
from rank_bm25 import BM25Okapi
import fitz  # PyMuPDF

# Bring in your table detection (text-only) helper
from .tables import detect_tables_text_only

# -----------------------------
# Config (via environment)
# -----------------------------
EMBED_MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-base")
# OCR enabled by default for Vietnamese/mixed PDFs, can be disabled with OCR_ENABLED=false
OCR_ENABLED = os.getenv("OCR_ENABLED", "").lower() != "false"  # Default: true (enabled)
OCR_DPI = int(os.getenv("OCR_DPI", "300"))  # Increased from 220 for better quality
OCR_LANGUAGE = os.getenv("OCR_LANGUAGE", "eng")  # Options: 'eng', 'vie', 'vie+eng'
AUTO_DETECT_LANGUAGE = (os.getenv("AUTO_DETECT_LANGUAGE", "true").lower() == "true")
# Threshold for detecting scanned pages: if text density is low, likely scanned
OCR_TEXT_THRESHOLD = int(os.getenv("OCR_TEXT_THRESHOLD", "100"))  # Characters per page
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TRANSLATION_ENABLED = (os.getenv("TRANSLATION_ENABLED", "true").lower() == "true")
TRANSLATION_SOURCE_LANGS = {lang.strip().lower() for lang in os.getenv("TRANSLATION_SOURCE_LANGS", "vi").split(',') if lang.strip()}
TRANSLATION_MODEL = os.getenv("TRANSLATION_MODEL", "gpt-4o-mini")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "static"
TABLE_DIR = STATIC_DIR / "tables"
os.makedirs(TABLE_DIR, exist_ok=True)

if TRANSLATION_ENABLED:
    try:
        from .translation import translate_text
    except Exception:
        translate_text = None
else:
    translate_text = None

# Skip these vertical zones (points) to avoid header/footer bands that look like tables
TABLE_SKIP_HEADER_PX = int(os.getenv("TABLE_SKIP_HEADER_PX", "72"))  # ~1 inch
TABLE_SKIP_FOOTER_PX = int(os.getenv("TABLE_SKIP_FOOTER_PX", "60"))

# Ignore tiny “tables”
TABLE_MIN_WIDTH_PX = int(os.getenv("TABLE_MIN_WIDTH_PX", "120"))
TABLE_MIN_HEIGHT_PX = int(os.getenv("TABLE_MIN_HEIGHT_PX", "40"))

# Padding around crops (for legacy snips; not used in lazy flow)
TABLE_PAD_PX = int(os.getenv("TABLE_PAD_PX", "4"))

# -----------------------------
# Text cleanup & scoring
# -----------------------------
_WS = re.compile(r"\s+")

def _clean_text(t: Optional[str]) -> str:
    return _WS.sub(" ", (t or "")).strip()

_MONTHS = r"January|February|March|April|May|June|July|August|September|October|November|December"

def compute_fact_score(text: str) -> int:
    """
    Small, fast heuristic used downstream by reranker.
    Higher when the chunk looks figure-heavy or date/entity anchored.
    """
    score = 0
    if re.search(r"\b\d+(\.\d+)?\b", text):
        score += 2
    if re.search(r"%|\bVND\b|\bUSD\b|\bbn\b|\bmn\b", text, re.IGNORECASE):
        score += 2
    if re.search(r"\b20\d{2}\b", text):
        score += 2
    if re.search(_MONTHS, text, re.IGNORECASE):
        score += 1
    if len(re.findall(r"\b[A-Z][a-z]{2,}\b", text)) > 3:
        score += 1
    return score

def is_subjective(text: str) -> bool:
    subjective_markers = [
        "we believe","we expect","in our opinion","likely","unlikely",
        "should","could","may","might","optimistic","cautious",
        "anticipate","forecast","estimate","suggests","attractive",
        "undervalued","overvalued","confident","maintain our rating",
        "reiterate our","initiate coverage"
    ]
    t = (text or "").lower()
    return any(marker in t for marker in subjective_markers)

def _tokenize_for_bm25(text: str) -> List[str]:
    """Lightweight tokenizer that keeps numerics, currencies, % and dashes."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9%.$€£₫¥\- ]+", " ", text)
    return text.split()

def _clip_text(text: str, limit: int = 800) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"

def _should_translate(language: str) -> bool:
    if not TRANSLATION_ENABLED or not translate_text:
        return False
    if not language:
        return False
    return language.lower() in TRANSLATION_SOURCE_LANGS

def _translate_text_if_needed(text: str, language: str) -> Optional[str]:
    if not _should_translate(language) or not text or len(text.strip()) < 25:
        return None
    try:
        translated = translate_text(text, source_lang=language or 'vi', target_lang='en')
        if translated and translated.strip():
            return translated.strip()
    except Exception as exc:
        print(f"[translation] Failed to translate chunk: {exc}")
    return None

# -----------------------------
# Optional OCR (best-effort) with language support
# -----------------------------
def _ocr_pages_if_needed(path: str, page_idxs_needing_ocr: List[int], language: str = None, doc_name: str = None) -> Dict[int, str]:
    """
    OCR pages with language support (English or Vietnamese).
    
    Args:
        path: Path to PDF file
        page_idxs_needing_ocr: List of 0-based page indices needing OCR
        language: Language code ('eng', 'vie', or None for auto-detect)
    
    Returns:
        Dictionary mapping page index to OCR text
    """
    out: Dict[int, str] = {}
    if not OCR_ENABLED:
        print(f"[OCR] OCR disabled (OCR_ENABLED={OCR_ENABLED})")
        return out
    if not page_idxs_needing_ocr:
        print(f"[OCR] No pages need OCR")
        return out
    
    print(f"[OCR] Starting OCR for {len(page_idxs_needing_ocr)} pages...")
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except Exception as e:
        print(f"[OCR] Failed to import OCR dependencies: {e}")
        return out

    # Determine OCR language
    ocr_lang = language or OCR_LANGUAGE
    if AUTO_DETECT_LANGUAGE and language is None:
        # Try to auto-detect language from PDF
        try:
            from .language_detection import detect_pdf_language
            detected = detect_pdf_language(path, sample_pages=2)
            if detected == 'vi':
                ocr_lang = 'vie'
            elif detected == 'mixed':
                ocr_lang = 'vie+eng'  # Use both languages
            else:
                ocr_lang = 'eng'  # Default to English
        except Exception:
            ocr_lang = OCR_LANGUAGE  # Fallback to config

    try:
        images = convert_from_path(path, dpi=OCR_DPI)
        print(f"[OCR] Converted {len(images)} pages to images")
    except Exception as e:
        print(f"[OCR] Failed to convert PDF to images: {e}")
        return out

    for idx in page_idxs_needing_ocr:
        if 0 <= idx < len(images):
            try:
                # Improved OCR with better settings for scanned documents
                # Use PSM mode 6 (uniform block of text) for better table detection
                # Use PSM mode 3 (fully automatic) as fallback
                ocr_configs = [
                    f'--psm 6 -l {ocr_lang}',  # Uniform block (good for tables)
                    f'--psm 3 -l {ocr_lang}',  # Fully automatic
                    f'--psm 6 -l eng',  # Fallback to English
                ]
                
                txt = ""
                last_error = None
                for config in ocr_configs:
                    try:
                        txt = pytesseract.image_to_string(images[idx], config=config) or ""
                        if len(txt.strip()) > 50:  # If we got substantial text, use it
                            break
                    except Exception as e:
                        last_error = e
                        continue
                
                if txt and len(txt.strip()) > 10:
                    cleaned = _clean_text(txt)
                    out[idx] = cleaned
                    print(f"[OCR] Page {idx+1}: extracted {len(txt)} chars with lang={ocr_lang}")
                    # Debug: Show OCR text for page 4 of HAH (ship table)
                    if idx == 3 and doc_name and 'HAH' in doc_name.upper():  # Page 4 (0-based index 3) of HAH
                        preview = cleaned[:500].replace('\n', ' ').strip()
                        print(f"[OCR] HAH Page 4 preview (first 500 chars): {preview}...")
                        # Also check if ship names are present
                        ship_keywords = ['haian', 'gama', 'mind', 'link', 'west', 'east', 'view', 'park', 'time', 'bell', 'rose', 'city', 'alfa', 'beta', 'opus', 'charlie']
                        found_ships = [kw for kw in ship_keywords if kw.lower() in cleaned.lower()]
                        if found_ships:
                            print(f"[OCR] HAH Page 4: Found ship keywords: {found_ships}")
                        else:
                            print(f"[OCR] HAH Page 4: WARNING - No ship keywords found in OCR text!")
                elif last_error:
                    print(f"[OCR] Page {idx+1}: All OCR configs failed, last error: {last_error}")
            except Exception as e:
                print(f"[OCR] Failed on page {idx+1}: {e}")
                # Final fallback to English (no PSM mode)
                try:
                    txt = pytesseract.image_to_string(images[idx], lang='eng') or ""
                    if txt and len(txt.strip()) > 10:
                        out[idx] = _clean_text(txt)
                        print(f"[OCR] Page {idx+1}: fallback extracted {len(txt)} chars")
                except Exception as e2:
                    print(f"[OCR] Page {idx+1}: fallback also failed: {e2}")
    return out

# -----------------------------
# Lightweight graph heuristic
# -----------------------------
def _is_likely_graph(draw_objs: List[dict]) -> bool:
    if not draw_objs:
        return False
    lines = sum(1 for d in draw_objs if d.get("type") == "line")
    curves = sum(1 for d in draw_objs if d.get("type") in ("curve", "bezier", "qcurve", "rect"))
    return curves > (lines * 2 + 10)

# -----------------------------
# PDF extraction (NO eager table PNG snips)
# -----------------------------
def extract_pdf_pages(path: str):
    """
    Yield dicts:
      {
        "doc": <filename>,
        "text": <page text>,
        "page_number": <1-based>,
        "is_table": False,
        "table_html": "",
        "table_md": "",
        "table_img": None,
      }
    """
    path = str(path)
    doc_name = os.path.basename(path)
    result_pages: List[Dict] = []

    # Extract text with PyMuPDF for retrieval context
    with fitz.open(path) as doc:
        total_pages = doc.page_count
        print(f"[extract_pdf_pages] Processing {total_pages} pages from {doc_name}")
        page_images_count = []  # Track image count per page for scanned detection
        for i, page in enumerate(doc, start=1):
            blocks = page.get_text("blocks") or []
            text_blocks = [b for b in blocks if len(b) >= 5 and isinstance(b[4], str)]
            raw_text = " ".join(_clean_text(b[4]) for b in text_blocks if b[4])
            drawings = page.get_drawings() or []
            
            # Count images on page (indicator of scanned content)
            image_list = page.get_images()
            image_count = len(image_list) if image_list else 0
            text_len = len(raw_text.strip())
            page_images_count.append((i, image_count, text_len))

            # Always emit a normal page text chunk for retrieval unless clearly chart-only
            is_graph = _is_likely_graph(drawings)
            if not is_graph:
                result_pages.append({
                    "doc": doc_name,
                    "text": _clean_text(raw_text),
                    "page_number": i,
                    "is_table": False,
                    "table_html": "",
                    "table_md": "",
                    "table_img": None,
                })
            else:
                print(f"[extract_pdf_pages] Page {i} skipped (likely graph/chart)")
        
        print(f"[extract_pdf_pages] Extracted {len(result_pages)} pages from {doc_name}")

    # Detect language for this PDF (if auto-detect enabled)
    detected_language = 'en'  # Default
    if AUTO_DETECT_LANGUAGE:
        try:
            from .language_detection import detect_pdf_language
            detected_language = detect_pdf_language(path, sample_pages=3)
            print(f"[language_detection] Detected language for {doc_name}: {detected_language}")
        except Exception as e:
            print(f"[language_detection] Language detection failed: {e}, defaulting to English")
    
    # Improved OCR detection: pages with little text OR pages with images but low text density
    # This catches scanned PDFs that might have headers/footers but scanned body content
    ocr_targets = set()
    print(f"[OCR] Checking {len(page_images_count)} pages for OCR. OCR_ENABLED={OCR_ENABLED}, detected_language={detected_language}")
    
    # For Vietnamese/mixed PDFs, be VERY aggressive - OCR all pages with images OR low text
    # Many Vietnamese reports are fully scanned, so we need to OCR most pages
    is_vn_pdf = detected_language in ('vi', 'mixed')
    
    for page_num, image_count, text_len in page_images_count:
        page_idx = page_num - 1
        # Trigger OCR if:
        # 1. Very little text (< 20 chars) - always OCR
        # 2. Has images AND low text density - scanned page detection
        # 3. For Vietnamese/mixed PDFs: OCR if has images OR text < 200 chars (very aggressive)
        should_ocr = False
        if text_len < 20:
            should_ocr = True  # Always OCR nearly-empty pages
        elif is_vn_pdf:
            # For VN PDFs: OCR if has images OR text is low (likely scanned)
            if image_count > 0 or text_len < 200:
                should_ocr = True
        else:
            # For English PDFs: OCR if has images AND text is low
            threshold = OCR_TEXT_THRESHOLD
            if image_count > 0 and text_len < threshold:
                should_ocr = True
        
        if should_ocr:
            ocr_targets.add(page_idx)
            print(f"[OCR] Page {page_num} flagged: text_len={text_len}, images={image_count}, is_vn={is_vn_pdf}")
    
    ocr_targets = sorted(ocr_targets)
    print(f"[OCR] Total pages to OCR: {len(ocr_targets)} out of {len(page_images_count)}")
    if ocr_targets:
        # Determine OCR language based on detected language
        ocr_lang = None
        if detected_language == 'vi':
            ocr_lang = 'vie'
        elif detected_language == 'mixed':
            ocr_lang = 'vie+eng'
        # else: use default (eng)
        
        ocr_map = _ocr_pages_if_needed(path, ocr_targets, language=ocr_lang, doc_name=doc_name)
        print(f"[OCR] OCR completed, got results for {len(ocr_map)} pages")
        if ocr_map:
            # Apply OCR text to the last entry we emitted for each page_number.
            last_idx_by_page: Dict[int, int] = {}
            for idx, rp in enumerate(result_pages):
                pn = rp.get("page_number")
                if isinstance(pn, int):
                    last_idx_by_page[pn] = idx
            applied_count = 0
            for zero_based_idx, ocr_text in ocr_map.items():
                pn = zero_based_idx + 1
                doc_idx = last_idx_by_page.get(pn)
                if doc_idx is not None:
                    result_pages[doc_idx]["text"] = _clean_text(ocr_text)
                    applied_count += 1
            print(f"[OCR] Applied OCR text to {applied_count} pages")
        else:
            print(f"[OCR] WARNING: No OCR results returned for {len(ocr_targets)} flagged pages")
    
    # Add language metadata to each page
    for rp in result_pages:
        rp["language"] = detected_language

    # Yield with stable keys (including language)
    for p in result_pages:
        yield {
            "doc": p.get("doc", doc_name),
            "text": p.get("text", ""),
            "page_number": p.get("page_number"),
            "language": p.get("language", "en"),  # NEW: Language metadata
            "is_table": False,
            "table_html": "",
            "table_md": "",
            "table_img": None,
        }

# -----------------------------
# Chunking (coarse + fine)
# -----------------------------
def _split_smart(text: str, size: int, overlap: int) -> List[str]:
    text = text or ""
    if len(text) <= size:
        return [text] if text else []
    out = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        window = text[start:end]
        m = re.search(r'([\.:\;\?])\s+\S*$', window)
        if m and (end - (start + m.end())) < 120:
            end = start + m.end()
        seg = text[start:end].strip()
        if seg:
            out.append(seg)
        start = max(end - overlap, start + 1)
    return out

def chunk_pages_dual(pages, big_size=1600, small_size=600, overlap=60):
    """
    Returns: (big_chunks, big_meta), (small_chunks, small_meta)
    """
    big_chunks, big_meta = [], []
    small_chunks, small_meta = [], []

    for p in pages:
        original_text = p.get("text", "") or ""
        page_num = p.get("page_number")
        language = p.get("language", "en")
        doc_name = p.get("doc", "")

        translated_text = _translate_text_if_needed(original_text, language)
        text_for_chunks = translated_text or original_text

        # Prepare aligned original chunks if we translated
        original_big_chunks = _split_smart(original_text, big_size, overlap) if translated_text else None
        original_small_chunks = _split_smart(original_text, small_size, overlap) if translated_text else None

        bigs = _split_smart(text_for_chunks, big_size, overlap)
        for idx, c in enumerate(bigs):
            orig_chunk = original_big_chunks[idx] if translated_text and original_big_chunks and idx < len(original_big_chunks) else (original_text if not translated_text else "")
            meta = {
                "doc": doc_name,
                "page": page_num,
                "language": language,
                "fact_score": compute_fact_score(c),
                "subjective": is_subjective(c),
                "is_table": False,
            }
            if translated_text:
                meta.update({
                    "original_text": _clip_text(orig_chunk, 800),
                    "translated_text": c,
                    "translated": True,
                })
            big_meta.append(meta)
            big_chunks.append(c)

        smalls = _split_smart(text_for_chunks, small_size, overlap)
        for idx, c in enumerate(smalls):
            orig_chunk = original_small_chunks[idx] if translated_text and original_small_chunks and idx < len(original_small_chunks) else (original_text if not translated_text else "")
            meta = {
                "doc": doc_name,
                "page": page_num,
                "language": language,
                "fact_score": compute_fact_score(c),
                "subjective": is_subjective(c),
                "is_table": False,
            }
            if translated_text:
                meta.update({
                    "original_text": _clip_text(orig_chunk, 400),
                    "translated_text": c,
                    "translated": True,
                })
            small_meta.append(meta)
            small_chunks.append(c)

    return (big_chunks, big_meta), (small_chunks, small_meta)

# -----------------------------
# Table TEXT indexing (lazy PNGs are handled elsewhere)
# -----------------------------
def table_chunks_for_doc(pdf_path: Path) -> Tuple[List[str], List[Dict]]:
    """
    Detect tables via detect_tables_text_only() and return (texts, metas) to index.
    meta includes:
      {
        "doc": <name>,
        "page": <1-based>,
        "type": "table",
        "table_id": <stable id>,
        "bbox": [x0, y0, x1, y1]  # PDF coordinate space (float)
      }
    """
    texts: List[str] = []
    metas: List[Dict] = []
    for tm in detect_tables_text_only(pdf_path):
        texts.append(tm.text)
        metas.append({
            "doc": tm.doc,
            "page": tm.page + 1,  # 1-based for UI/citations
            "type": "table",
            "table_id": tm.table_id,
            "bbox": list(tm.bbox),
        })
    return texts, metas

# -----------------------------
# Embeddings (SentenceTransformers or OpenAI)
# -----------------------------
def _is_openai_model(name: str) -> bool:
    return name.lower().startswith("openai/")

def _openai_dim(name: str) -> int:
    nm = name.lower()
    if "text-embedding-3-large" in nm:
        return 3072
    return 1536

class _STEncoder:
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()
        # E5 models work better with prefixes (optional but recommended)
        self.is_e5_model = "e5" in model_name.lower() or "multilingual-e5" in model_name.lower()
    def encode(self, texts: List[str], is_query: bool = False) -> np.ndarray:
        # E5 models: prepend "query: " for queries, "passage: " for documents
        if self.is_e5_model:
            prefix = "query: " if is_query else "passage: "
            texts = [prefix + text if text and not text.startswith(prefix) else text for text in texts]
        embs = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(embs, dtype="float32")

class _OpenAIEncoder:
    def __init__(self, model_name: str, api_key: str):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY required for OpenAI embeddings.")
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)
        self.model = model_name.replace("openai/", "")
        self.dim = _openai_dim(model_name)
    def encode(self, texts: List[str]) -> np.ndarray:
        out: List[np.ndarray] = []
        B = 64
        for i in range(0, len(texts), B):
            batch = texts[i:i+B]
            resp = self.client.embeddings.create(model=self.model, input=batch)
            vecs = [np.asarray(d.embedding, dtype="float32") for d in resp.data]
            out.append(np.vstack(vecs))
        embs = np.vstack(out) if out else np.zeros((0, self.dim), dtype="float32")
        norms = np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9
        embs = embs / norms
        return embs

def _build_encoder(name: str):
    if _is_openai_model(name):
        enc = _OpenAIEncoder(name, OPENAI_API_KEY)
        return enc, enc.dim
    else:
        enc = _STEncoder(name)
        return enc, enc.dim

# -----------------------------
# Optional Cross-Encoder reranker (small & fast). Safe if unavailable.
# -----------------------------
try:
    from sentence_transformers import CrossEncoder  # type: ignore
    _CE = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
except Exception:
    _CE = None

def _hyde_prompts(q: str) -> List[str]:
    return [
        f"Write a concise passage that answers: {q}",
        f"Paraphrase and broaden with synonyms: {q}",
    ]

def _hyde(llm_fn, q: str, k: int = 2) -> List[str]:
    if not llm_fn:
        return []
    outs: List[str] = []
    for p in _hyde_prompts(q)[:k]:
        try:
            r = llm_fn(p)
            if r:
                outs.append(r.strip())
        except Exception:
            pass
    return [o for o in outs if o]

# -----------------------------
# Vector store (FAISS + BM25 hybrid)
# -----------------------------
class VectorStore:
    def __init__(self, index_dir: str, embed_model: str):
        self.index_dir = index_dir
        os.makedirs(index_dir, exist_ok=True)

        self.embed_model_name = embed_model
        self.encoder, self.dim = _build_encoder(self.embed_model_name)

        self.index_path = os.path.join(index_dir, "faiss.index")
        self.meta_path = os.path.join(index_dir, "meta.npy")
        self.text_path = os.path.join(index_dir, "texts.npy")

        self._new_index()
        self.meta: List[dict] = []
        self.texts: List[str] = []
        self._load()

        self._bm25: Optional[BM25Okapi] = None
        self._bm25_tokens: List[List[str]] = []
        self._build_bm25()

    # ---------- BM25 ----------
    def _build_bm25(self):
        corpus = self.texts or []
        self._bm25_tokens = [_tokenize_for_bm25(t or "") for t in corpus]
        self._bm25 = BM25Okapi(self._bm25_tokens) if self._bm25_tokens else None

    def _after_storage_change(self):
        self._build_bm25()
        self._save()

    # ---------- persistence ----------
    def _new_index(self):
        self.index = faiss.IndexFlatIP(self.dim)

    def _load(self):
        if os.path.exists(self.index_path) and os.path.exists(self.meta_path) and os.path.exists(self.text_path):
            try:
                idx = faiss.read_index(self.index_path)
                if idx.d != self.dim:
                    # Rebuild from persisted texts
                    self.meta = np.load(self.meta_path, allow_pickle=True).tolist()
                    self.texts = np.load(self.text_path, allow_pickle=True).tolist()
                    self._new_index()
                    if self.texts:
                        embs = self._encode(self.texts)
                        self.index.add(embs)
                    self._save()
                else:
                    self.index = idx
                    self.meta = np.load(self.meta_path, allow_pickle=True).tolist()
                    self.texts = np.load(self.text_path, allow_pickle=True).tolist()
            except Exception:
                try:
                    self.meta = np.load(self.meta_path, allow_pickle=True).tolist()
                    self.texts = np.load(self.text_path, allow_pickle=True).tolist()
                except Exception:
                    self.meta, self.texts = [], []
                self._new_index()
                if self.texts:
                    embs = self._encode(self.texts)
                    self.index.add(embs)
                self._save()
        else:
            self.meta, self.texts = [], []
            self._new_index()
            self._save()

        n = min(getattr(self.index, "ntotal", 0), len(self.meta), len(self.texts))
        if n != getattr(self.index, "ntotal", 0):
            self._new_index()
            if n > 0:
                embs = self._encode(self.texts[:n])
                self.index.add(embs)
        self.meta = self.meta[:n]
        self.texts = self.texts[:n]

    def remove_docs(self, docs: List[str]) -> int:
        """Remove all chunks belonging to any basename in docs (flexible matching)."""
        to_remove = set(docs or [])
        if not to_remove:
            return 0

        def norm(s: str) -> str:
            return os.path.basename((s or "").strip()).lower()

        # Build norm map of stored docs
        norm_map: Dict[str, List[int]] = {}
        for i, m in enumerate(self.meta):
            d = norm(m.get("doc") or "")
            norm_map.setdefault(d, []).append(i)

        # Resolve input -> indices
        drop: set[int] = set()
        for asked in to_remove:
            key = norm(asked)
            for idx in norm_map.get(key, []):
                drop.add(idx)

        if not drop:
            return 0

        keep = [i for i in range(len(self.meta)) if i not in drop]
        self.meta = [self.meta[i] for i in keep]
        self.texts = [self.texts[i] for i in keep]

        self._new_index()
        if self.texts:
            embs = self._encode(self.texts)
            self.index.add(embs)
        self._after_storage_change()
        return len(drop)

    def _save(self):
        faiss.write_index(self.index, self.index_path)
        np.save(self.meta_path, np.array(self.meta, dtype=object), allow_pickle=True)
        np.save(self.text_path, np.array(self.texts, dtype=object), allow_pickle=True)

    # ---------- encode ----------
    def _encode(self, texts: List[str], is_query: bool = False) -> np.ndarray:
        # Pass is_query flag to encoder (for E5 models)
        if hasattr(self.encoder, 'encode'):
            if hasattr(self.encoder, 'is_e5_model') and self.encoder.is_e5_model:
                return self.encoder.encode(texts, is_query=is_query)
        return self.encoder.encode(texts)

    # ---------- add ----------
    def add(self, chunks: List[str], metas: List[dict]):
        if not chunks:
            return
        if len(metas) != len(chunks):
            m = (metas or [])
            if len(m) < len(chunks):
                m = m + [{}] * (len(chunks) - len(m))
            metas = m[:len(chunks)]
        embs = self._encode(chunks)
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
        k = max(1, min(k, n))
        if getattr(self.index, "ntotal", 0) != n:
            self._new_index()
            if n > 0:
                embs = self._encode(self.texts)
                self.index.add(embs)
                self._save()
        q = self._encode([query], is_query=True)  # NEW: Mark as query for E5 models
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

    # ---------- search (hybrid: FAISS + BM25 + HyDE + CE + MMR) ----------
    def search_hybrid(
        self,
        query: str,
        k: int = 12,
        alpha: float = 0.65,
        *,
        strategy: str = "alpha",         # 'alpha' or 'rrf'
        pre_k: Optional[int] = None,     # fused short-list size
        mmr_topn: Optional[int] = None,  # e.g., 12
        mmr_lambda: float = 0.7,         # diversity strength
        llm_fn=None,                     # for HyDE expansions
        use_ce: bool = True,             # CrossEncoder rerank if available
    ) -> List[Tuple[str, dict, float]]:
        """
        Returns list of (chunk_text, chunk_meta, fused_score)
        """
        n = len(self.texts or [])
        if n == 0:
            return []
        k = max(1, min(k, n))
        pre_k = max(k, 50) if pre_k is None else max(k, min(pre_k, n))

        q_clean = (query or "").strip()
        if not q_clean:
            return []

        # Keep FAISS in sync (defensive)
        if getattr(self.index, "ntotal", 0) != n:
            self._new_index()
            if n > 0:
                embs = self._encode(self.texts)
                self.index.add(embs)
                self._save()

        # 1) Query expansions (HyDE)
        queries = [q_clean] + _hyde(llm_fn, q_clean, k=2)

        # 2) Retrieve candidates from both dense and BM25 for each expansion
        dense_hits: List[Tuple[int, float]] = []
        bm25_hits: List[Tuple[int, float]] = []
        seen = set()

        for qx in queries:
            # Dense cosine (inner product of normalized embs)
            try:
                qv = self._encode([qx], is_query=True)  # NEW: Mark as query for E5 models
                D, I = self.index.search(qv, max(1, pre_k // 2))
                for idx, score in zip(I[0], D[0]):
                    if 0 <= idx < n and idx not in seen:
                        dense_hits.append((int(idx), float(score)))
                        seen.add(int(idx))
            except Exception:
                pass

            # Lexical BM25
            if self._bm25 is not None:
                toks = _tokenize_for_bm25(qx)
                scores = self._bm25.get_scores(toks)
                order = np.argsort(scores)[::-1][:max(1, pre_k // 2)]
                for idx in order:
                    s = float(scores[idx])
                    if s > 0 and int(idx) not in seen:
                        bm25_hits.append((int(idx), s))
                        seen.add(int(idx))

        if not dense_hits and not bm25_hits:
            return []

        # 3) Fuse lexical + dense candidates
        if strategy == "rrf":
            # Reciprocal Rank Fusion
            ranks: Dict[int, float] = {}
            for hits in (bm25_hits, dense_hits):
                for r, (idx, _score) in enumerate(hits, start=1):
                    ranks[idx] = ranks.get(idx, 0.0) + 1.0 / (60 + r)
            fused_pairs = sorted(ranks.items(), key=lambda x: x[1], reverse=True)[:pre_k]
        else:
            # Alpha fusion (normalized)
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
            combined: Dict[int, float] = {
                i: alpha * dn.get(i, 0.0) + (1 - alpha) * bn.get(i, 0.0)
                for i in set(list(dn.keys()) + list(bn.keys()))
            }
            fused_pairs = sorted(combined.items(), key=lambda x: x[1], reverse=True)[:pre_k]

        cand_idxs = [i for i, _ in fused_pairs]
        cand_scores = {i: sc for i, sc in fused_pairs}

        # 4) Optional CrossEncoder re-rank on fused short-list
        if use_ce and _CE and cand_idxs:
            pairs = [(query, self.texts[i]) for i in cand_idxs[:max(k * 3, 24)]]
            try:
                ce_scores = _CE.predict(pairs)
                order = sorted(range(len(pairs)), key=lambda j: float(ce_scores[j]), reverse=True)
                cand_idxs = [cand_idxs[j] for j in order]
            except Exception:
                pass

        # 5) Optional MMR for diversity (using dense embeddings)
        if mmr_topn and mmr_topn > 0:
            try:
                qv = self._encode([q_clean])[0:1]  # shape (1, dim)
                cand_texts = [self.texts[i] for i in cand_idxs]
                cand_embs = self._encode(cand_texts)
                sims = (cand_embs @ qv.T).ravel()
                selected: List[int] = []
                remaining = list(range(cand_embs.shape[0]))
                while remaining and len(selected) < min(mmr_topn, k):
                    if not selected:
                        j0 = int(np.argmax(sims[remaining]))
                        pick = remaining[j0]
                        selected.append(pick)
                        remaining.remove(pick)
                        continue
                    sel = cand_embs[selected]
                    redundancy = np.max(cand_embs[remaining] @ sel.T, axis=1)
                    lam = float(mmr_lambda)
                    mmr = lam * sims[remaining] - (1 - lam) * redundancy
                    j = int(np.argmax(mmr))
                    pick = remaining[j]
                    selected.append(pick)
                    remaining.remove(pick)
                cand_idxs = [cand_idxs[j] for j in selected]
            except Exception:
                pass

        # Final top-k
        out = [(self.texts[i], self.meta[i], float(cand_scores.get(i, 0.0))) for i in cand_idxs[:k]]
        return out
