# app/main.py
import os
import re
import json
from pathlib import Path
from typing import List, Tuple, Dict, Iterable

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, PlainTextResponse
from openai import OpenAI
from collections import Counter


from .rag import VectorStore, extract_pdf_pages, chunk_pages_dual
from .schemas import ChatRequest, ChatResponse, Citation
from .prompts import SYSTEM_PROMPT, USER_PROMPT

# Try to import strict numeric guard (optional, safe fallback if missing)
try:
    from .utils.validators import enforce_truth  # returns (text, ok, flags)
except Exception:
    def enforce_truth(answer: str, cited_texts: List[str]):
        # No-op fallback: allow everything
        return answer, True, []

# ---------- env ----------
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
TOP_K = int(os.getenv("TOP_K", "6"))
MODEL = os.getenv("MODEL", "gpt-4o-mini")
STRICT_MODE = os.getenv("STRICT_MODE", "true").lower() == "true"

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    raise RuntimeError(
        f"OPENAI_API_KEY not found. Expected it in {ENV_PATH}. "
        "Make sure .env exists and contains: OPENAI_API_KEY=sk-..."
    )

# ---------- finance query helpers ----------
FIN_SYNONYMS: Dict[str, List[str]] = {
    "capex": ["capex", "capital expenditure", "capital expenditures", "investment plan", "cap ex"],
    "funding": ["funding", "financing", "debt", "loan", "equity", "cash flow"],
    "guidance": ["guidance", "outlook", "forecast", "plan"],
    "capacity": ["capacity", "plant capacity", "production capacity"],
}

def expand_query(q: str) -> List[str]:
    ql = q.lower()
    expanded: List[str] = [q]
    for key, alts in FIN_SYNONYMS.items():
        if key in ql:
            expanded.extend(alts)
    years = re.findall(r"\b(20[0-9]{2})\b", q)
    expanded.extend(list(set(years)))
    # de-dup preserve order
    seen = set()
    out: List[str] = []
    for s in expanded:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out

def year_boost_score(text: str, years: List[str]) -> int:
    t = (text or "").lower()
    return sum(1 for y in years if y and y.lower() in t)

def extract_short_quotes(text: str, keywords: List[str], years: List[str], max_quotes: int = 4) -> List[str]:
    """
    Pull short exact phrases including a target year or finance keyword.
    Keeps quotes compact for analyst readability.
    """
    t = (text or "")
    parts = re.split(r'(?<=[\.\:\;])\s+', t)  # light sentence split
    picks: List[str] = []
    for s in parts:
        ls = s.strip()
        if not ls:
            continue
        sl = ls.lower()
        if any(y in sl for y in years) or any(k in sl for k in keywords):
            if len(ls) > 220:
                m = re.search(r'(?:20[0-9]{2}|[0-9][0-9\.,]{2,})', ls)
                if m:
                    i = max(0, m.start() - 80)
                    j = min(len(ls), m.end() + 80)
                    ls = ls[i:j].strip()
                else:
                    ls = ls[:220]
            picks.append(ls)
        if len(picks) >= max_quotes:
            break
    return picks

# ---------- app ----------
app = FastAPI(title="MW PDF Q&A", version="1.3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later
    allow_methods=["*"],
    allow_headers=["*"],
)

# serve the UI from the same origin to avoid CORS headaches
app.mount("/ui", StaticFiles(directory="web", html=True), name="ui")

client = OpenAI(api_key=OPENAI_KEY)
store = VectorStore(index_dir="index", embed_model=EMBED_MODEL)

DATA_DIR = os.getenv("DATA_DIR", "data")
INDEX_DIR = os.getenv("INDEX_DIR", "index")
store = VectorStore(index_dir=INDEX_DIR, embed_model=EMBED_MODEL)
os.makedirs(DATA_DIR, exist_ok=True)

# ---------- helpers ----------
def _keyword_rerank(hits: List[Tuple[str, dict, float]], k: int, extra_kw: List[str] | None = None):
    KW = [
        "capex", "capital expenditure", "fund", "funded", "funding", "loan", "debt", "equity",
        "capacity", "plant", "facility", "customer", "sell to", "buyer", "volume", "price", "guidance",
        "revenue", "ebitda", "profit", "cash flow", "ocf", "fcf"
    ]
    if extra_kw:
        KW.extend(extra_kw)
    KW = list({w.lower() for w in KW})
    def score(text: str) -> int:
        t = (text or "").lower()
        return sum(1 for kw in KW if kw in t)
    return sorted(hits, key=lambda h: score(h[0]), reverse=True)[:k]

def _build_context_and_cites(hits: List[Tuple[str, dict, float]]):
    context_blocks: List[str] = []
    cites: List[Citation] = []
    seen = set()
    for text, meta, _ in hits:
        doc = (meta or {}).get("doc", "UnknownDoc")
        page = (meta or {}).get("page")
        key = (doc, page)
        if key in seen:
            continue
        seen.add(key)
        snippet = (text or "")[:1200]
        context_blocks.append(f"[DOC={doc} PAGE={page}]\n{snippet}")
        cites.append(Citation(doc=doc, page=page, snippet=snippet[:280]))
    return context_blocks, cites

def _indexed_docs() -> List[str]:
    """Unique doc names seen in the vector store metadata."""
    metas = getattr(store, "meta", []) or []
    docs = [m.get("doc") for m in metas if isinstance(m, dict) and m.get("doc")]
    uniq = sorted(set(docs))
    return uniq

def _iter_doc_plaintext(doc: str) -> Iterable[str]:
    """Stream plain text for a single stored PDF."""
    pdf_path = os.path.join(DATA_DIR, doc)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, f"{doc} not found in data/")
    pages = extract_pdf_pages(pdf_path)
    for p in pages:
        page_no = p.get("page_number")
        text = p.get("text", "") or ""
        yield f"[DOC={doc} p{page_no}]\n{text}\n\n"

# ---------- endpoints ----------
@app.get("/health")
def health():
    return {
        "ok": True,
        "model": MODEL,
        "embed_model": EMBED_MODEL,
        "docs_indexed": len(getattr(store, "meta", []) or []),
    }

@app.get("/files")
def list_files():
    metas = getattr(store, "meta", []) or []
    docs = [m.get("doc") for m in metas if isinstance(m, dict) and m.get("doc")]
    counts = Counter(docs)
    files = [{"doc": d, "chunks": c} for d, c in counts.items()]
    files.sort(key=lambda x: x["doc"].lower())
    return {
        "files": files,
        "count": len(files),
        "total_chunks": sum(counts.values()),
    }

@app.post("/reset")
def reset_store():
    # clear in-memory
    store.texts = []
    store.meta = []
    # wipe index dir and rebuild empty index
    import shutil
    if os.path.exists(store.index_dir):
        shutil.rmtree(store.index_dir, ignore_errors=True)
    os.makedirs(store.index_dir, exist_ok=True)
    store._load()  # VectorStore._load() handles empty init
    return {"ok": True, "message": "Vector store cleared."}

@app.post("/upload")
async def upload(files: List[UploadFile] = File(...)):
    added = 0
    for uf in files:
        # persist the file
        path = os.path.join(DATA_DIR, uf.filename)
        with open(path, "wb") as out:
            out.write(await uf.read())
        # index pages
        pages = list(extract_pdf_pages(path))
        if not pages:
            continue

        # dual-tier chunking (big + small)
        (big_c, big_m), (small_c, small_m) = chunk_pages_dual(pages)

        store.add(big_c,   [{**m, "doc": uf.filename, "tier": "big"} for m in big_m])
        store.add(small_c, [{**m, "doc": uf.filename, "tier": "small"} for m in small_m])

        added += len(big_c) + len(small_c)
    return {"ok": True, "chunks_added": added}

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    k = req.k or TOP_K

    # decide per-request strictness first (so we can use it below)
    use_strict = req.strict if (hasattr(req, "strict") and req.strict is not None) else STRICT_MODE

    # FULL mode unchanged...
    if req.query.startswith("FULL:"):
        q = req.query[len("FULL:"):].strip()
        hits = store.search_hybrid(q, k=max(k, 50), alpha=0.65)
        ql = q.lower()
        bullets = []
        for text, meta, _ in hits:
            if ql in (text or "").lower():
                doc = (meta or {}).get("doc", "UnknownDoc")
                page = (meta or {}).get("page")
                bullets.append(f"• {doc} p.{page}: " + (text or "")[:220])
        answer = "\n".join(bullets) or "No literal matches found."
        return ChatResponse(answer=answer, citations=[], found=bool(bullets))

    # --- retrieval (unchanged) ---
    subqs = expand_query(req.query)
    years = re.findall(r"\b(20[0-9]{2})\b", req.query)

    raw_hits: List[Tuple[str, dict, float]] = []
    for s in subqs:
        raw_hits.extend(store.search_hybrid(s, k=max(k, 12), alpha=0.65))

    seen_map: Dict[Tuple[str, int, int], Tuple[str, dict, float]] = {}
    for text, meta, score in raw_hits:
        doc = (meta or {}).get("doc")
        page = (meta or {}).get("page")
        key = (doc, int(page) if page is not None else -1, hash((text or "")[:96]))
        prev = seen_map.get(key)
        if prev is None or score > prev[2]:
            seen_map[key] = (text, meta, score)
    deduped = list(seen_map.values())

    def rank_key(item: Tuple[str, dict, float]):
        text, meta, score = item
        return (year_boost_score(text, years), score)

    deduped.sort(key=rank_key, reverse=True)
    hits = deduped[:max(k, 12)]

    # build context + citations + quotes
    context_blocks: List[str] = []
    cites: List[Citation] = []
    quotes: List[Tuple[str, int, str]] = []
    seen_pages = set()

    for text, meta, _ in hits:
        doc = (meta or {}).get("doc", "UnknownDoc")
        page = (meta or {}).get("page")
        key = (doc, page)
        if key in seen_pages:
            continue
        seen_pages.add(key)

        snippet = (text or "")[:1200]
        context_blocks.append(f"[DOC={doc} PAGE={page}]\n{snippet}")
        cites.append(Citation(doc=doc, page=page, snippet=snippet[:280]))

        quotes.extend(
            (doc, page, q)
            for q in extract_short_quotes(
                text,
                keywords=["capex","capital expenditure","fund","financing","debt","equity","capacity","guidance"],
                years=years,
                max_quotes=2,
            )
        )

    context_str = "\n\n---\n\n".join(context_blocks) if context_blocks else "(no matching excerpts)"
    prompt = USER_PROMPT.format(question=req.query, context=context_str)
    if not use_strict:
        prompt += (
            "\n\nUI rule: Do NOT add a separate 'Sources' section. "
            "Citations are shown by the UI. Avoid fabricating any references."
        )

    # LLM call
    llm_answer = ""
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        if resp and resp.choices:
            llm_answer = (resp.choices[0].message.content or "").strip()
    except Exception:
        llm_answer = ""

    # choose final answer
    strict_answer = ""
    if quotes:
        bullets = [f'• "{q}" ({doc}, p.{page})' for doc, page, q in quotes[:6]]
        strict_answer = "Key excerpts:\n" + "\n".join(bullets)

    if use_strict and strict_answer:
        final_answer = strict_answer
    elif llm_answer:
        final_answer = llm_answer
    elif strict_answer:
        final_answer = strict_answer
    else:
        final_answer = "Not found in uploaded PDFs."

    # numeric guard (soft) only when NOT strict
    validation_texts = [t for (t, _, _) in hits]
    validation_texts.extend([q for (_, _, q) in quotes])
    if not use_strict:
        from .utils.validators import enforce_truth_soft
        final_answer, _ok, _flags = enforce_truth_soft(final_answer, validation_texts)

    return ChatResponse(
        answer=final_answer,
        citations=cites if context_blocks else [],
        found=bool(context_blocks or quotes),
    )


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Streams the assistant answer (first line sends citations metadata as JSON).
    Client should treat the first newline-delimited chunk as a JSON header:
      {"type":"meta","citations":[...]}
    All subsequent bytes are raw answer tokens.
    """
    k = req.k or TOP_K
    # Keep the simple non-hybrid path for streaming (can be upgraded later)
    hits = store.search(req.query, k=k)
    hits = _keyword_rerank(hits, k)
    context_blocks, cites = _build_context_and_cites(hits)
    context_str = "\n\n---\n\n".join(context_blocks) if context_blocks else "(no matching excerpts)"
    header = {"type": "meta", "citations": [c.dict() for c in cites]}

    def gen():
        # send metadata header line first
        yield (json.dumps(header) + "\n").encode("utf-8")
        # then stream tokens
        stream = client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT.format(question=req.query, context=context_str)},
            ],
            stream=True,
        )
        for chunk in stream:
            delta = getattr(chunk.choices[0].delta, "content", None) if chunk and chunk.choices else None
            if delta:
                yield delta.encode("utf-8")
        yield b"\n"

    return StreamingResponse(gen(), media_type="text/plain")

# ---------- Debug / Explore / Export ----------
@app.get("/debug/grep")
def debug_grep(q: str, k: int = 12):
    # hybrid search
    hits = store.search_hybrid(q, k=max(k, 12), alpha=0.65)
    ql = q.lower()
    toks = [t for t in re.split(r"\s+", ql) if t]
    def has_token(txt: str) -> bool:
        tl = (txt or "").lower()
        return any(t in tl for t in toks)
    filtered = [(t, m, s) for (t, m, s) in hits if has_token(t)]
    if not filtered:  # fall back to hybrid hits if no literal match
        filtered = hits
    return [
        {"doc": m.get("doc"), "page": m.get("page"), "snippet": (t or "")[:400]}
        for (t, m, s) in filtered[:k]
    ]

@app.get("/debug/pages")
def debug_pages(doc: str):
    """
    Re-extracts text per page for a stored PDF and shows stats.
    Use: /debug/pages?doc=FILENAME.pdf
    """
    pdf_path = os.path.join(DATA_DIR, doc)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, f"{doc} not found in data/")
    pages = list(extract_pdf_pages(pdf_path))
    report = []
    for p in pages:
        text = p.get("text", "")
        report.append({
            "page": p.get("page_number"),
            "chars": len(text),
            "empty": len(text.strip()) == 0,
            "preview": text[:240]
        })
    return {"doc": doc, "pages": len(report), "report": report}

@app.get("/debug/chunks")
def debug_chunks(doc: str = ""):
    """
    Shows chunk sizes and starting pages in the vector store.
    If doc is provided, filters to that document only.
    """
    texts = getattr(store, "texts", []) or []
    metas = getattr(store, "meta", []) or []
    out = []
    for t, m in zip(texts, metas):
        if doc and m.get("doc") != doc:
            continue
        out.append({
            "doc": m.get("doc"),
            "start_page": m.get("page"),
            "tier": m.get("tier"),
            "chars": len(t),
            "preview": (t or "")[:200]
        })
    out.sort(key=lambda x: (x["doc"] or "", x["start_page"] or 0, x.get("tier") or ""))
    return {"count": len(out), "chunks": out}

@app.get("/export/text")
def export_text(doc: str):
    """
    Full plain-text export of a stored PDF (joined per page).
    """
    pdf_path = os.path.join(DATA_DIR, doc)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, f"{doc} not found in data/")
    pages = list(extract_pdf_pages(pdf_path))
    full_text = "\n\n".join(f"[p{p['page_number']}] {p['text']}" for p in pages)
    return {"doc": doc, "chars": len(full_text), "text": full_text}

@app.get("/export/json")
def export_json(doc: str):
    """
    JSON export of a stored PDF (page-by-page).
    """
    pdf_path = os.path.join(DATA_DIR, doc)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, f"{doc} not found in data/")
    pages = list(extract_pdf_pages(pdf_path))
    return {"doc": doc, "pages": pages}

# NEW: Analyst-friendly streaming exports (no LLM)
@app.get("/export/full", response_class=PlainTextResponse)
def export_full(doc: str | None = Query(default=None, description="Optional document to restrict to")):
    """
    If doc is provided, stream that doc's plain text.
    Else, stream all indexed docs, one after another.
    """
    def _gen_all():
        docs = _indexed_docs() if doc is None else [doc]
        if not docs:
            yield "No documents indexed.\n"
            return
        for d in docs:
            yield f"===== {d} =====\n\n"
            for chunk in _iter_doc_plaintext(d):
                yield chunk
            yield "\n"

    return StreamingResponse(_gen_all(), media_type="text/plain; charset=utf-8")

@app.get("/export/matches")
def export_matches(
    q: str = Query(..., description="Query to retrieve raw chunks for (no LLM)"),
    k: int = Query(50, ge=1, le=500, description="How many chunks to return"),
    doc: str | None = Query(None, description="Optional document scope"),
):
    """
    Return the top-k raw chunks (big+small) for a query, without any model generation.
    """
    hits = store.search_hybrid(q, k=max(k, 50), alpha=0.65)

    def _stream(hits_iter: Iterable[Tuple[str, dict, float]]):
        i = 0
        for (txt, meta, score) in hits_iter:
            if doc and (meta or {}).get("doc") != doc:
                continue
            i += 1
            doc_id = (meta or {}).get("doc", "unknown_doc")
            page = (meta or {}).get("page", "unknown_page")
            line1 = f"## {i}  [doc={doc_id}] [page={page}] [score={score:.3f}]\n"
            yield line1
            yield (txt or "") + "\n\n"
            if i >= k:
                break

    return StreamingResponse(_stream(hits), media_type="text/plain; charset=utf-8")

@app.get("/search/doc")
def search_doc(doc: str, q: str, max_hits: int = 500):
    """
    Exhaustive literal matches within a single doc (no LLM).
    """
    texts = getattr(store, "texts", []) or []
    metas  = getattr(store, "meta", []) or []

    items = [(t, m) for t, m in zip(texts, metas) if (m or {}).get("doc") == doc]
    ql = q.lower()
    hits = []
    for t, m in items:
        tl = (t or "").lower()
        if ql in tl:
            hits.append({
                "page": (m or {}).get("page"),
                "tier": (m or {}).get("tier"),
                "chars": len(t or ""),
                "snippet": (t or "")[:600]
            })
            if len(hits) >= max_hits:
                break
    return {"doc": doc, "query": q, "count": len(hits), "hits": hits}

@app.get("/chunks/doc")
def chunks_doc(doc: str, offset: int = 0, limit: int = 50):
    """
    Paginated listing of all chunks for a given doc.
    """
    texts = getattr(store, "texts", []) or []
    metas  = getattr(store, "meta", []) or []

    items = [{"doc": (m or {}).get("doc"),
              "page": (m or {}).get("page"),
              "tier": (m or {}).get("tier"),
              "chars": len(t or ""),
              "snippet": (t or "")[:400]}
             for t, m in zip(texts, metas) if (m or {}).get("doc") == doc]

    total = len(items)
    slice_ = items[offset: offset+limit]
    return {"doc": doc, "total": total, "offset": offset, "limit": limit, "items": slice_}

@app.delete("/files")
def delete_file(
    doc: str = Query(..., description="Exact filename to remove (case-sensitive)"),
    purge_disk: bool = Query(False, description="Also delete the PDF from data/"),
):
    removed = store.remove_docs([doc])
    if removed == 0:
        raise HTTPException(status_code=404, detail=f"{doc} not found in index")
    if purge_disk:
        pdf_path = os.path.join(DATA_DIR, doc)
        if os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception:
                pass
    return {"ok": True, "doc": doc, "chunks_removed": removed, "purge_disk": purge_disk}

