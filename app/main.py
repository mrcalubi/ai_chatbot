import os
import uvicorn
import re
import json
import hashlib
import html
import threading
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Dict, Iterable

import numpy as np
import unicodedata
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, PlainTextResponse, RedirectResponse, FileResponse
from openai import OpenAI
from collections import Counter
from pydantic import BaseModel

# Project modules
from .rag import VectorStore, extract_pdf_pages, chunk_pages_dual, table_chunks_for_doc
from .schemas import ChatRequest, ChatResponse, Citation
from .prompts import SYSTEM_PROMPT, USER_PROMPT
from .tables import render_table_png
from .history import RecentContextLinker  # NEW: separate follow-up linker
from functools import lru_cache
import time as _time

@lru_cache(maxsize=512)
def _cached_search(query: str, alpha: float, use_ce: bool, pre_k: int, mmr_topn: int, mmr_lambda: float, k: int):
    # NOTE: scope filtering still happens outside
    return tuple(store.search_hybrid(
        query, k=k, alpha=alpha, llm_fn=None, use_ce=use_ce, pre_k=pre_k, mmr_topn=mmr_topn, mmr_lambda=mmr_lambda
    ))



# ---------------- Env ----------------
load_dotenv()

EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
TOP_K = int(os.getenv("TOP_K", "6"))
MODEL = os.getenv("MODEL", "gpt-4o-mini")

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    raise RuntimeError("OPENAI_API_KEY not found. Create a .env with: OPENAI_API_KEY=sk-...")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_DIR = os.getenv("INDEX_DIR", str(PROJECT_ROOT / "index"))
DATA_DIR = os.getenv("DATA_DIR", str(PROJECT_ROOT / "data"))
STATIC_DIR = PROJECT_ROOT / "static"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(INDEX_DIR, exist_ok=True)
os.makedirs(STATIC_DIR / "tables", exist_ok=True)

# ---------------- App ----------------
app = FastAPI(title="MW PDF Q&A (Local)", version="2.0-intents-scope-followup")

UI_DIR = PROJECT_ROOT / "web"
# Add cache-busting headers for static files
class NoCacheStaticFiles(StaticFiles):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

app.mount("/ui", NoCacheStaticFiles(directory=str(UI_DIR), html=True), name="ui")
app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR), html=False), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(api_key=OPENAI_KEY)
store = VectorStore(index_dir=INDEX_DIR, embed_model=EMBED_MODEL)

# --------- Q/A log (for simple similar-question boosting & recency memory) ---------
_QA_LOG_PATH = PROJECT_ROOT / "data" / "qa_log.jsonl"
_QA_LOCK = threading.Lock()
_QA_CACHE: List[dict] = []  # each: {"q": str, "when": str, "cites": [{"doc":..., "page":...}], "q_vec": list}


def _qa_log_init():
    try:
        _QA_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        raw = []
        if _QA_LOG_PATH.exists():
            with _QA_LOG_PATH.open("r", encoding="utf-8") as f:
                raw = [json.loads(line) for line in f if line.strip()]
    except Exception:
        raw = []
    global _QA_CACHE
    _QA_CACHE = raw[-400:]

def _truncate_at_word_boundary(text: str, max_len: int = 280) -> str:
    """Truncate text at word boundary to avoid cutting mid-word"""
    if not text or len(text) <= max_len:
        return text
    # Find last space before max_len
    truncated = text[:max_len]
    last_space = truncated.rfind(' ')
    if last_space > max_len * 0.7:  # Only use if not too short
        return truncated[:last_space] + "..."
    return truncated + "..."

def _qa_log_append(question: str, citations: List["Citation"], q_vec: "np.ndarray|None" = None):
    try:
        item = {
            "q": question,
            "when": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "cites": [{"doc": c.doc, "page": c.page} for c in (citations or [])],
        }
        if q_vec is not None:
            try:
                import numpy as _np
                item["q_vec"] = _np.asarray(q_vec, dtype="float32").ravel().tolist()
            except Exception:
                item["q_vec"] = None
        with _QA_LOCK:
            with _QA_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            _QA_CACHE.append(item)
            if len(_QA_CACHE) > 500:
                _QA_CACHE[:] = _QA_CACHE[-400:]
    except Exception:
        pass

def _similar_boosts(question: str, top_n: int = 5, min_sim: float = 0.82) -> Dict[tuple, float]:
    """Return {(doc, page): weight} map from most similar past Qs for learning boost."""
    try:
        import numpy as _np
        if not _QA_CACHE:
            return {}
        qv = store._encode([question])[0]
        sims = []
        for item in reversed(_QA_CACHE[-300:]):  # recent 300
            v = _np.asarray(item.get("q_vec") or [], dtype="float32")
            if v.size == 0 or v.shape[0] != qv.shape[0]:
                vv = store._encode([item["q"]])[0]
                item["q_vec"] = vv.tolist()
                v = vv
            sim = float((qv * v).sum())  # vectors L2-normalized by encoder
            sims.append((sim, item))
        sims.sort(key=lambda x: x[0], reverse=True)
        picks = [it for (s, it) in sims if s >= min_sim][:top_n]
        boosts: Dict[tuple, float] = {}
        for it in picks:
            for c in it.get("cites") or []:
                key = (c.get("doc"), int(c.get("page") or 0))
                boosts[key] = boosts.get(key, 0.0) + 1.0
        return boosts
    except Exception:
        return {}

_qa_log_init()

# ---------- Follow-up linker (separate module) ----------
linker = RecentContextLinker(client=client, model=MODEL, qa_cache_ref=_QA_CACHE)

# ---------- LLM helper for HyDE ----------
def _llm_fn(prompt: str) -> str:
    try:
        resp = client.chat.completions.create(
            model=MODEL, temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
        )
        if resp and resp.choices:
            return (resp.choices[0].message.content or "").strip()
    except Exception:
        pass
    return ""

# ---------------- Finance helpers ----------------
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
    seen = set()
    out: List[str] = []
    for s in expanded:
        if s not in seen:
            seen.add(s); out.append(s)
    return out

# ---- Intent helpers ----
SUMMARIZE_PAT = re.compile(
    r"\b(summarize|summarise|summary|summaries|overview|key ideas|key points|main points|high level|valuation|investment thesis|consensus|broker.*consensus)\b",
    re.IGNORECASE
)
def looks_like_summarize_intent(q: str) -> bool:
    return bool(SUMMARIZE_PAT.search(q or ""))

SUBJECTIVE_MARKERS = [
    "we believe","we expect","in our opinion","likely","unlikely","should","could","may","might",
    "optimistic","cautious","anticipate","forecast","estimate","suggests","attractive","undervalued",
    "overvalued","confident","maintain our rating","reiterate our","initiate coverage"
]
# Add synonyms that often appear instead of "revenue growth"
# Find FIN_KEYWORDS_BASE and make sure it includes these:
FIN_KEYWORDS_BASE = [
    "revenue","sales","turnover","net sales","operating revenue","topline",
    "yoy","y/y","growth","increase","rose","decline","decrease",
    "npat","pat","pbt","ebit","ebitda","eps","margin","capex","capital expenditure",
    "guidance","outlook","forecast","order book","backlog","utilisation","capacity"
]

# --- Numeric retrieval helpers (inlined) ---
import re as _re

FIN_METRIC_SEEDS = [
    "capex","capital expenditure","revenue","sales","turnover",
    "ebitda","ebit","eps","earnings per share","pat","pbt",
    "margin","operating margin","net margin","gross margin",
    "roi","roic","roe","free cash flow","fcf","ocf",
    "growth","guidance","forecast","outlook","figures","numbers","table"
]

_NUMERIC_TOKEN = _re.compile(
    r'\b(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:%|k|m|mm|b|bn|million|billion|trillion)?\b',
    _re.IGNORECASE
)

# Loosen "numeric-ish" detection to catch text like "double-digit", "high teens"
NUMERIC_WORD_RE = re.compile(r"(double\-digit|single\-digit|triple\-digit|high\s+teens|low\s+teens)", re.I)


def _contains_numberish(text: str) -> bool:
    if not text: return False
    return bool(_NUMERIC_TOKEN.search(text) or NUMERIC_WORD_RE.search(text))


def _count_numberish(text: str) -> int:
    return len(_NUMERIC_TOKEN.findall(text or ""))

def _norm_years(q: str) -> list[str]:
    return _re.findall(r"\b(20[0-4]\d)\b", q or "")

def _guess_company_tokens(q: str, linked_prev_q: str | None) -> list[str]:
    base = " ".join(t for t in [q or "", linked_prev_q or ""] if t).lower()
    toks = _re.findall(r"[A-Za-z]{2,}\d*", base)
    # de-dup preserve order
    out = []
    seen = set()
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:6]

MAX_NUMERIC_QUERIES = 4  # keep this small for speed


def _make_numeric_queries(base_q: str, linked_prev_q: str | None) -> list[str]:
    base_q = (base_q or "").strip()
    years = _norm_years(base_q)
    comps = _guess_company_tokens(base_q, linked_prev_q)

    seeds = FIN_METRIC_SEEDS

    variants = set()
    # base
    if base_q:
        variants.add(base_q)
    # company × metric × (year?)
    for c in comps:
        for m in seeds:
            variants.add(f"{c} {m}")
            variants.add(f"{m} {c}")
            for y in years:
                variants.add(f"{c} {m} {y}")
                variants.add(f"{m} {c} {y}")
    # base × metric × (year?)
    for m in seeds:
        if base_q:
            variants.add(f"{base_q} {m}")
            for y in years:
                variants.add(f"{base_q} {m} {y}")
        for y in years:
            variants.add(f"{m} {y}")
    # catch-all
    if base_q:
        variants.add(f"{base_q} financial metrics")
    return list(variants)[:MAX_NUMERIC_QUERIES]

def _contains_any(text_l: str, needles: list[str]) -> bool:
    return any(n in text_l for n in needles)

def _count_any(text_l: str, needles: list[str]) -> int:
    return sum(1 for n in needles if n in text_l)

def detect_intent_metrics(q: str) -> bool:
    return bool(re.search(
        r"\b(revenue|sales|npat|pat|pbt|ebit|ebitda|eps|margin|yoy|qoq|capex|order book|dividend|roe|roa|roce|roic|utilisation|capacity|backlog)\b",
        (q or "").lower()
    ))

def pick_representative_chunks(
    all_items: List[Tuple[str, dict, float]],
    *, limit_chars: int = 6000, prefer_early_pages: bool = True,
) -> List[Tuple[str, dict]]:
    scored = []
    for text, meta, _ in all_items:
        if not text: continue
        m = meta or {}
        page = int(m.get("page") or 9999)
        fact = float(m.get("fact_score", 0))
        subj_penalty = 1.0 if m.get("subjective") else 0.0
        early_bonus = 1.0 if (prefer_early_pages and page <= 3) else 0.0
        is_table = 1.0 if m.get("is_table") else 0.0
        score = (2.0 * fact) + (0.6 * early_bonus) + (0.4 * is_table) - (0.8 * subj_penalty)
        scored.append((score, text, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = []
    seen_pages = set()
    total = 0
    for _score, text, m in scored:
        key = (m.get("doc"), (m or {}).get("page"))
        if key in seen_pages: 
            continue
        seen_pages.add(key)
        picked.append((text, m))
        total += len(text or "")
        if total >= limit_chars:
            break
    return picked

# --------- Simple reranker (weights) ----------
WEIGHTS_BASE = {
    "kw_match_count": 1.5,
    "kw_match_any":   2.0,
    "has_number":     2.0,
    "has_percent":    2.5,
    "has_currency":   1.2,
    "year_overlap":   1.6,
    "has_year":       0.6,
    "is_tabley":      0.8,
    "len_chars":     -0.5,
    "fact_score":     0.8,
    "is_subjective": -2.5,
    "page_is_early":  0.4,
    "user_boost":     0.25,
}
WEIGHTS_QUAL_OVERLAY = {"kw_match_count":+0.4,"has_number":-0.6,"has_percent":-0.6,"is_subjective":+0.3}

def _blend_weights(base: dict, overlays: list[dict]) -> dict:
    out = dict(base)
    for ov in overlays:
        for k, v in ov.items():
            out[k] = out.get(k, 0.0) + v
    return out

def weighted_rerank(
    hits: List[Tuple[str, dict, float]],
    query: str,
    k: int,
    strict: bool,
    boosts: Dict[tuple, float] | None = None,
) -> List[Tuple[str, dict, float, float]]:
    is_metrics = detect_intent_metrics(query)
    overlays = []
    if not is_metrics: overlays.append(WEIGHTS_QUAL_OVERLAY)
    W = _blend_weights(WEIGHTS_BASE, overlays)
    fused = [h[2] for h in hits] or [0.0]
    lo, hi = float(min(fused)), float(max(fused))
    def norm(x: float) -> float: return 0.0 if hi - lo < 1e-9 else (x - lo) / (hi - lo)

    ranked = []
    for (text, meta, fused_score) in hits:
        tl = (text or "").lower()
        doc_key = ((meta or {}).get("doc"), int((meta or {}).get("page") or 0))
        user_boost = float((boosts or {}).get(doc_key, 0.0))
        feats = {
            "kw_match_count": _count_any(tl, FIN_KEYWORDS_BASE),
            "kw_match_any": 1.0 if _contains_any(tl, FIN_KEYWORDS_BASE) else 0.0,
            "has_number": 1.0 if re.search(r"\b\d+([\.,]\d+)?\b", tl) else 0.0,
            "has_percent": 1.0 if "%" in tl or re.search(r"\b(yoy|qoq)\b", tl) else 0.0,
            "has_currency": 1.0 if re.search(r"\b(vnd|usd|bn|mn|trillion|billion)\b", tl) else 0.0,
            "year_overlap": len(set(re.findall(r"\b(20[0-9]{2})\b", (query or "").lower())) &
                                set(re.findall(r"\b(20[0-9]{2})\b", tl))),
            "has_year": 1.0 if re.search(r"\b(20[0-9]{2})\b", tl) else 0.0,
            "is_tabley": 1.0 if any(ch in (text or "") for ch in "|,;%") else 0.0,
            "len_chars": min(len(text or ""), 4000) / 4000.0,
            "fact_score": float((meta or {}).get("fact_score", 0)),
            "is_subjective": 1.0 if _contains_any(tl, SUBJECTIVE_MARKERS) or (meta or {}).get("subjective") else 0.0,
            "page_is_early": 1.0 if ((meta or {}).get("page") or 9999) <= 3 else 0.0,
            "user_boost": user_boost,
        }
        s_linear = float(sum(W.get(k, 0.0) * float(feats.get(k, 0.0)) for k in feats.keys()))
        final = s_linear + 0.4 * norm(float(fused_score))
        ranked.append((text, meta, fused_score, final))
    ranked.sort(key=lambda x: x[3], reverse=True)
    return ranked[:k]

# --------- Context + cites ----------
def _build_context_and_cites(hits: List[Tuple[str, dict, float]]):
    context_blocks: List[str] = []
    cites: List[Citation] = []
    seen = set()
    for text, meta, _ in hits:
        doc = (meta or {}).get("doc", "UnknownDoc")
        page = (meta or {}).get("page")
        key = (doc, page)
        if key in seen: continue
        seen.add(key)
        snippet = (text or "")[:1200]
        context_blocks.append(f"[Document: {doc}, Page: {page}]\n{snippet}")
        cites.append(Citation(doc=doc, page=page, snippet=_truncate_at_word_boundary(snippet, 280)))
    return context_blocks, cites

def _indexed_docs() -> List[str]:
    metas = getattr(store, "meta", []) or []
    docs = [m.get("doc") for m in metas if isinstance(m, dict) and m.get("doc")]
    return sorted(set(docs))

def _iter_doc_plaintext(doc: str) -> Iterable[str]:
    pdf_path = os.path.join(DATA_DIR, doc)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, f"{doc} not found in data/")
    pages = extract_pdf_pages(pdf_path)
    for p in pages:
        page_no = p.get("page_number"); text = p.get("text", "") or ""
        yield f"[DOC={doc} p{page_no}]\n{text}\n\n"

# ---------------- Delete file ----------------
class DeleteReq(BaseModel):
    doc: str
    purge_disk: bool = False

def _normalize_docname(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFC", s)
    s = os.path.basename(s)
    return s

@app.get("/files")
def list_files():
    metas = getattr(store, "meta", []) or []
    docs = [m.get("doc") for m in metas if isinstance(m, dict) and m.get("doc")]
    counts = Counter(docs)
    files = []
    for d, c in counts.items():
        files.append({"doc": d, "doc_norm": _normalize_docname(d), "chunks": c})
    files.sort(key=lambda x: x["doc"].lower())
    print("[/files] docs in store:", [f["doc"] for f in files])
    return {"files": files, "count": len(files), "total_chunks": sum(counts.values())}

def _delete_file_impl(doc: str, purge_disk: bool = False):
    doc_req = doc
    doc_norm = _normalize_docname(doc_req)
    metas = getattr(store, "meta", []) or []
    docs_in_store = [m.get("doc") for m in metas if isinstance(m, dict)]
    exact_hit = doc_req if doc_req in docs_in_store else None
    if not exact_hit:
        for d in docs_in_store:
            if _normalize_docname(d) == doc_norm:
                exact_hit = d; break
    target_name = exact_hit or doc_norm
    removed = store.remove_docs([target_name])
    deleted_pdf = False
    try:
        for p in (os.path.join(DATA_DIR, target_name), os.path.join(DATA_DIR, doc_norm)):
            if purge_disk and os.path.exists(p):
                os.remove(p); deleted_pdf = True
    except Exception as e:
        print("[/files/delete] Disk delete error:", e)
    print(f"[/files/delete] req={doc_req} -> target={target_name} removed_chunks={removed} deleted_pdf={deleted_pdf}")
    return {"ok": True, "requested": doc_req, "target": target_name, "removed_chunks": removed, "deleted_pdf": deleted_pdf}

# ---------------- Lazy table PNG endpoint ----------------
@app.get("/tables/png")
def tables_png(
    doc: str = Query(..., description="Exact filename in /data"),
    page: int = Query(..., description="0-based page index for cropping"),
    bbox: str = Query(..., description="x0,y0,x1,y1 in PDF coordinate space"),
):
    x0, y0, x1, y1 = [float(x) for x in (bbox or "").split(",")]
    pdf_path = Path(DATA_DIR) / os.path.basename(doc)
    if not pdf_path.exists():
        raise HTTPException(404, f"{doc} not found")
    url = render_table_png(pdf_path, page, (x0, y0, x1, y1))
    return {"png": url}

# ---------------- PDF Viewer Endpoint ----------------
@app.get("/pdf/view")
def view_pdf(
    doc: str = Query(..., description="Exact filename in /data"),
):
    """
    Serve PDF file for viewing in browser.
    Frontend will use PDF.js to render and navigate to specific pages.
    """
    # Security: only serve from DATA_DIR, prevent path traversal
    safe_doc = os.path.basename(doc)
    pdf_path = Path(DATA_DIR) / safe_doc
    
    if not pdf_path.exists():
        raise HTTPException(404, f"PDF not found: {doc}")
    
    if not pdf_path.suffix.lower() == '.pdf':
        raise HTTPException(400, "Only PDF files can be viewed")
    
    # Serve the PDF with inline disposition so browser can display it
    # Note: Avoid setting Content-Disposition/filename to prevent latin-1 encoding errors
    # when filenames contain non-ASCII characters (e.g., curly apostrophes).
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf"
    )

# ---------------- Helpers: doc scope detection ----------------
# --- Conditional hybrid booster helper ---
def _needs_hybrid_boost(hits, min_numeric=3, min_total=8):
    """Return True if current merged hits look weak (few numeric pages or too few hits)."""
    if not hits:
        return True
    numeric = 0
    for (_t, _m, _s) in hits:
        if (_m or {}).get("numeric_hit"):
            numeric += 1
    return (numeric < min_numeric) or (len(hits) < min_total)


# Company name aliases/normalizations (lowercase key -> uppercase ticker)
COMPANY_ALIASES = {
    "metrobrands": "METROBRA",
    "metrobrand": "METROBRA",
    "metro brands": "METROBRA",
    "metro": "METROBRA",  # Only if context suggests it
    "viglacera": "VGC",
    "duc giang": "DGC",
    "ducgiang": "DGC",
    "dgc": "DGC",
    "vgc": "VGC",
    "metrob": "METROBRA",
}

# Common words that should NEVER trigger document matches
COMMON_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by",
    "from", "up", "about", "into", "through", "during", "including", "against", "among",
    "key", "consensus", "behind", "what", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "should", "could", "may",
    "might", "must", "can", "this", "that", "these", "those", "which", "who", "whom",
    "broker", "brokers", "report", "reports", "year", "years", "yearly", "quarter", "quarters",
    "revenue", "growth", "capex", "eps", "pat", "ebitda", "yoy", "qoq", "guidance", "outlook",
    "summarise", "summarize", "summary", "summaries", "overview", "highlights",
}

def _detect_doc_scope_from_query(query: str) -> List[str]:
    """
    Enhanced entity extraction for document scope detection with company name normalization.
    
    Strategy:
    1. Extract company codes/tickers (2-10 uppercase chars)
    2. Extract proper nouns (capitalized words)
    3. Normalize company names using aliases (e.g., "metrobrands" -> "METROBRA")
    4. Extract longer lowercase company-like tokens (7+ chars)
    5. Match against indexed document filenames
    6. Prioritize exact/prefix matches and filter common words
    """
    if not query:
        return []
    
    docs = _indexed_docs()
    if not docs:
        return []
    
    query_lower = query.lower()
    
    # Extract potential company identifiers
    # 1. Stock tickers/codes (2-10 uppercase letters)
    tickers = set(re.findall(r'\b([A-Z]{2,10})\b', query))
    
    # 2. Capitalized words (proper nouns like "Viglacera", "Berkshire", "Metrobrands")
    proper_nouns = set(re.findall(r'\b([A-Z][a-z]{2,15})\b', query))
    
    # 3. Longer lowercase tokens (7+ chars) that might be company names
    # This catches "metrobrands", "viglacera", "ducgiang" etc.
    long_lowercase_tokens = set(re.findall(r'\b([a-z]{7,15})\b', query_lower))
    
    # 4. Normalize company names using aliases
    normalized_tickers = set()
    for alias, ticker in COMPANY_ALIASES.items():
        if alias in query_lower:
            normalized_tickers.add(ticker)
            # Also remove the alias from long_lowercase_tokens to avoid double-counting
            long_lowercase_tokens.discard(alias.replace(" ", ""))
    
    # Combine all candidates, filtering out common words
    all_candidates = tickers | proper_nouns | normalized_tickers
    
    # Add long lowercase tokens but filter common words
    for token in long_lowercase_tokens:
        if token not in COMMON_WORDS:
            all_candidates.add(token.upper())  # Convert to uppercase for matching
    
    # Remove common words from candidates
    all_candidates = {c for c in all_candidates if c.lower() not in COMMON_WORDS}
    
    print(f"[scope_detection] Extracted - Tickers: {tickers}, Proper: {proper_nouns}, Normalized: {normalized_tickers}, Long tokens: {long_lowercase_tokens}")
    print(f"[scope_detection] Final candidates (after filtering): {all_candidates}")
    
    if not all_candidates:
        return []
    
    # Extract known company codes from document filenames (e.g., METROBRA, DGC, VGC)
    known_codes = set()
    for doc in docs:
        # Extract company code from filename (usually first word before year/number)
        parts = doc.split()
        if parts:
            first_part = parts[0].upper()
            # Check if it looks like a company code (all caps, 2-10 chars)
            if re.match(r'^[A-Z]{2,10}$', first_part):
                known_codes.add(first_part)
    
    # Score each document by how well it matches
    doc_scores = {}
    for doc in docs:
        doc_lower = doc.lower()
        doc_upper = doc.upper()
        doc_base = os.path.splitext(doc)[0]  # Remove .pdf extension
        
        score = 0
        for candidate in all_candidates:
            cand_lower = candidate.lower()
            cand_upper = candidate.upper()
            
            # Prioritize exact company code matches (e.g., "METROBRA" in "METROBRA 2025...")
            if doc_upper.startswith(cand_upper + " ") or doc_upper.startswith(cand_upper + "_"):
                score += 200  # Very high priority
                continue
            
            # Exact word match at start of filename (highest priority)
            if doc_lower.startswith(cand_lower + " "):
                score += 150
                continue
            
            # Exact match as a word in filename (e.g., "METROBRA" in "METROBRA 2025 01 04 Emkay BUY.pdf")
            if re.search(r'\b' + re.escape(cand_lower) + r'\b', doc_lower):
                score += 100
            
            # Starts with candidate (without word boundary check for prefixes)
            elif doc_lower.startswith(cand_lower):
                score += 80
            
            # Contains at word boundary (e.g., "Berkshire" in filename)
            elif re.search(r'\b' + re.escape(cand_lower) + r'\b', doc_lower):
                score += 60
            
            # Partial match (fallback, low score)
            elif cand_lower in doc_lower:
                score += 10
        
        # Bonus: If candidate matches a known company code and doc starts with that code
        for code in known_codes:
            if code in all_candidates and doc_upper.startswith(code):
                score += 50  # Additional boost for known company codes
        
        if score > 0:
            doc_scores[doc] = score
    
    if not doc_scores:
        return []
    
    # Sort by score and return only high-confidence matches
    sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)
    
    # Return documents with score >= 100 (strong match threshold)
    # This ensures we only return documents that have clear company code matches
    strong_matches = [doc for doc, score in sorted_docs if score >= 100]
    
    if strong_matches:
        print(f"[scope_detection] ✅ Strong matches ({len(strong_matches)}): {strong_matches[:5]}")
        return strong_matches[:10]  # Limit to top 10 to avoid performance issues
    
    # If no strong matches (score >= 100), but we have candidates, return top 3
    # This handles edge cases but with lower confidence
    if sorted_docs and sorted_docs[0][1] >= 50:
        top_docs = [doc for doc, _ in sorted_docs[:3]]
        print(f"[scope_detection] ⚠️  Moderate matches (score < 100): {top_docs}")
        return top_docs
    
    # No matches found
    print(f"[scope_detection] ❌ No matches found for candidates: {all_candidates}")
    return []

def _filter_hits_to_scope(hits: List[Tuple[str, dict, float]], scope_docs: List[str]) -> List[Tuple[str, dict, float]]:
    if not scope_docs:
        return hits
    scope = set(scope_docs)
    filtered = [h for h in hits if (h[1] or {}).get("doc") in scope]
    
    # Safety filter: If scope contains company-specific docs (VGC, DGC, METROBRA),
    # remove Warren Buffett documents even if they somehow passed through
    if scope_docs and any(company in ''.join(scope_docs).upper() for company in ['VGC', 'DGC', 'METROBRA']):
        filtered = [h for h in filtered if 'Warren' not in (h[1] or {}).get("doc", "") and 'Buffett' not in (h[1] or {}).get("doc", "")]
        print(f"[scope_filter] Applied safety filter to remove Warren Buffett docs")
    
    return filtered

# ---------------- Endpoints ----------------
@app.get("/health")
def health():
    return {
        "ok": True,
        "model": MODEL,
        "embed_model": EMBED_MODEL,
        "docs_indexed": len(getattr(store, "meta", []) or []),
    }

@app.post("/reset")
def reset_store():
    store.texts = []; store.meta = []
    import shutil
    if os.path.exists(store.index_dir):
        shutil.rmtree(store.index_dir, ignore_errors=True)
    os.makedirs(store.index_dir, exist_ok=True)
    store._load()
    return {"ok": True, "message": "Vector store cleared."}

@app.post("/upload")
async def upload(files: List[UploadFile] = File(...)):
    added = 0
    for uf in files:
        path = os.path.join(DATA_DIR, uf.filename)
        with open(path, "wb") as out:
            out.write(await uf.read())

        pages = list(extract_pdf_pages(path))
        if not pages:
            continue
        (big_c, big_m), (small_c, small_m) = chunk_pages_dual(pages)
        store.add(big_c,   [{**m, "doc": uf.filename, "tier": "big"} for m in big_m])
        store.add(small_c, [{**m, "doc": uf.filename, "tier": "small"} for m in small_m])
        added += len(big_c) + len(small_c)

        t_texts, t_metas = table_chunks_for_doc(Path(path))
        if t_texts:
            store.add(t_texts, [{**m, "doc": uf.filename, "tier": "table"} for m in t_metas])
            added += len(t_texts)

    return {"ok": True, "chunks_added": added}

@app.post("/files/delete")
def delete_file(req: DeleteReq = Body(...)):
    print(f"[/files/delete] doc={req.doc} purge_disk={req.purge_disk}")
    return _delete_file_impl(req.doc, purge_disk=req.purge_disk)

def _collect_table_badges(hits: List[Tuple[str, dict, float]], max_tables: int = 6) -> str:
    badges = []
    seen = set()
    for _text, meta, _score in hits:
        m = meta or {}
        if m.get("type") != "table":
            continue
        bbox = m.get("bbox"); doc  = m.get("doc")
        page1 = int(m.get("page") or 1)  # stored 1-based in meta
        if not (doc and bbox and isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        key = (doc, page1, tuple(bbox))
        if key in seen:
            continue
        seen.add(key)
        page0 = max(0, page1 - 1)  # API expects 0-based
        bbox_str = ",".join(str(float(x)) for x in bbox)
        badges.append(
            "<span class='table-badge' "
            f"data-doc='{html.escape(str(doc))}' "
            f"data-page='{page0}' "
            f"data-bbox='{html.escape(bbox_str)}' "
            "title='Click to preview'>📊 Table</span>"
        )
        if len(badges) >= max_tables:
            break
    if not badges:
        return ""
    return (
        "<div style='margin-top:10px'><b>Tables</b></div>"
        "<div class='ev-tables' style='margin:6px 0; display:flex; flex-wrap:wrap'>"
        + "".join(badges) +
        "</div>"
    )

# ---------------- Chat Endpoint ----------------
@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    k = req.k or TOP_K

    # FULL literal search (debug tool)
    if req.query.startswith("FULL:"):
        q = req.query[len("FULL:"):].strip()
        hits = store.search_hybrid(
            q, k=max(k, 50), alpha=0.65, llm_fn=_llm_fn,
            use_ce=True, pre_k=80, mmr_topn=12, mmr_lambda=0.7
        )
        ql = q.lower()
        bullets = []
        for text, meta, _ in hits:
            if ql in (text or "").lower():
                doc = (meta or {}).get("doc", "UnknownDoc")
                page = (meta or {}).get("page")
                bullets.append(f"• {doc} p.{page}: " + (text or "")[:220])
        answer = "\n".join(bullets) or "No literal matches found."
        return ChatResponse(answer=answer, citations=[], found=bool(bullets))

    # --- Follow-up linker (recency + LLM) ---
    linked_prev_q, extra_years = linker.link(req.query)

    # --- Scope detection (company / doc type) ---
    scope_docs = _detect_doc_scope_from_query(req.query)
    print(f"[scope_filter] Query: '{req.query[:60]}...' → Detected scope: {scope_docs}")
    
    # If follow-up provides clearer scope, USE IT (don't just fallback)
    if linked_prev_q:
        prev_scope = _detect_doc_scope_from_query(linked_prev_q)
        if prev_scope:
            if not scope_docs:
                # Current query has no scope, use previous
                scope_docs = prev_scope
                print(f"[scope_filter] Using scope from linked query: '{linked_prev_q[:60]}...' → {scope_docs}")
            else:
                # Both have scope, use intersection if overlap, otherwise current query wins
                common = list(set(scope_docs) & set(prev_scope))
                if common:
                    scope_docs = common
                    print(f"[scope_filter] Using intersection of scopes: {scope_docs}")
                else:
                    print(f"[scope_filter] No overlap between scopes, using current query scope")
    
    # FALLBACK: If still no scope, check last 10 questions for ANY company mentions
    if not scope_docs:
        print(f"[scope_filter] No scope yet, checking recent conversation history...")
        recent_qs = [item.get("q", "") for item in reversed(_QA_CACHE[-10:]) if isinstance(item, dict) and item.get("q")]
        for idx, recent_q in enumerate(recent_qs):
            recent_scope = _detect_doc_scope_from_query(recent_q)
            if recent_scope:
                scope_docs = recent_scope
                print(f"[scope_filter] ✅ Found scope in recent Q-{idx}: '{recent_q[:50]}...' → {scope_docs}")
                break
        
        if not scope_docs:
            print(f"[scope_filter] ⚠️  No scope found in recent history")

    # --- Retrieval: expand queries (HyDE+CE), include follow-up rollovers ---
    subqs = expand_query(req.query)
    if linked_prev_q:
        subqs += expand_query(linked_prev_q)
    for y in extra_years:
        if y not in subqs:
            subqs.append(y)

    boosts_map = _similar_boosts(req.query)
    raw_hits: List[Tuple[str, dict, float]] = []
    for s in subqs:
        raw_hits.extend(store.search_hybrid(
            s, k=max(k, 12), alpha=0.65, llm_fn=_llm_fn,
            use_ce=True, pre_k=80, mmr_topn=12, mmr_lambda=0.7
        ))
    # Scope filter (to avoid cross-company leakage)
    if scope_docs:
        before_count = len(raw_hits)
        # Log what docs we have before filtering
        before_docs = set((h[1] or {}).get("doc") for h in raw_hits if h[1])
        print(f"[scope_filter] Before filter - {before_count} hits from docs: {sorted(before_docs)[:5]}")
        
        raw_hits = _filter_hits_to_scope(raw_hits, scope_docs)
        
        after_count = len(raw_hits)
        after_docs = set((h[1] or {}).get("doc") for h in raw_hits if h[1])
        print(f"[scope_filter] After filter - {after_count} hits from docs: {sorted(after_docs)[:5]}")
        print(f"[scope_filter] Filtered hits: {before_count} → {after_count} (removed {before_count - after_count})")
    else:
        print(f"[scope_filter] WARNING: No scope detected, not filtering hits (total: {len(raw_hits)})")
        hit_docs = set((h[1] or {}).get("doc") for h in raw_hits if h[1])
        print(f"[scope_filter] Unfiltered docs: {sorted(hit_docs)[:10]}")

    # Deduplicate by (doc, page, md5(text))
    seen_map: Dict[Tuple[str, int, str], Tuple[str, dict, float]] = {}
    for text, meta, score in raw_hits:
        doc = (meta or {}).get("doc")
        page = (meta or {}).get("page")
        h = hashlib.md5((text or "").encode("utf-8")).hexdigest()
        key = (doc, int(page) if page is not None else -1, h)
        prev = seen_map.get(key)
        if prev is None or score > prev[2]:
            seen_map[key] = (text, meta, score)
    deduped = list(seen_map.values())

    # ---- Summarize-intent path
    if looks_like_summarize_intent(req.query):
        # For summarize queries, use retrieved hits first (they're query-relevant)
        # Then supplement with representative chunks if needed
        summarize_hits = []
        if deduped:
            # Use the retrieved hits (they're already query-relevant)
            summarize_hits = [(t, m, 0.0) for (t, m, _) in deduped[:max(k, 15)]]
        else:
            # Fallback: pick representative chunks if no hits
            texts = getattr(store, "texts", []) or []
            metas = getattr(store, "meta", []) or []
            all_items = [(t, m, 0.0) for t, m in zip(texts, metas)]
            # Scope filter if any
            if scope_docs:
                all_items = [(t, m, 0.0) for (t, m, _) in all_items if (m or {}).get("doc") in set(scope_docs)]
            summarize_hits = pick_representative_chunks(all_items, limit_chars=6500, prefer_early_pages=True)
            summarize_hits = [(t, m) for (t, m) in summarize_hits]

        context_blocks, cites = [], []
        for item in summarize_hits:
            if len(item) == 3:
                text, meta, _ = item
            else:
                text, meta = item
            doc = (meta or {}).get("doc", "UnknownDoc")
            page = (meta or {}).get("page")
            snippet = (text or "")[:1200]
            if snippet.strip():  # Only add non-empty snippets
                context_blocks.append(f"[Document: {doc}, Page: {page}]\n{snippet}")
                cites.append(Citation(doc=doc, page=page, snippet=_truncate_at_word_boundary(snippet, 280)))

        if not context_blocks:
            try:
                q_vec = store._encode([req.query])[0]
            except Exception:
                q_vec = None
            _qa_log_append(req.query, [], q_vec)
            return ChatResponse(answer="I couldn't build a summary from the uploaded PDFs.", citations=[], found=False)

        context_str = "\n\n---\n\n".join(context_blocks)
        print(f"[summarize] Built {len(context_blocks)} context blocks, total chars: {len(context_str)}")
        
        guardrails = (
            "\n\nIMPORTANT REMINDERS:\n"
            "- Synthesize information from the context to provide a comprehensive answer.\n"
            "- For summarize/consensus questions, extract and synthesize key themes from the broker reports.\n"
            "- Look for broker opinions, ratings, forecasts, and key findings across all provided documents.\n"
            "- Only state 'not available' for specific facts that are truly missing from the context.\n"
            "- ALWAYS cite sources using the exact format: (Filename.pdf, p.5) - do NOT use dashes or other formats.\n"
            "- Use **bold** headings, bullets for key facts.\n"
            "- Citations must be inline within sentences, not at the end as separate lines.\n"
        )
        prompt = USER_PROMPT.format(question=req.query, context=context_str) + guardrails
        if linked_prev_q:
            prompt = f"Prior related user question: {linked_prev_q}\n\n" + prompt

        llm_answer = ""
        try:
            resp = client.chat.completions.create(
                model=MODEL, temperature=0.2,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
            )
            if resp and resp.choices:
                llm_answer = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"[summarize] LLM error: {e}")
            pass

        if not llm_answer or len(llm_answer.strip()) < 20:
            llm_answer = "I couldn't build a summary from the uploaded PDFs. Please try rephrasing your question or check if the relevant documents are uploaded."

        # Prepare hits for table badges
        table_badge_hits = []
        for item in summarize_hits[:10]:
            if len(item) == 3:
                t, m, _ = item
                table_badge_hits.append((t, m, 0.0))
            else:
                t, m = item
                table_badge_hits.append((t, m, 0.0))
        tables_html = _collect_table_badges(table_badge_hits, max_tables=6)
        final_answer = llm_answer
        if tables_html:
            final_answer += "\n\n" + tables_html

        try:
            q_vec = store._encode([req.query])[0]
        except Exception:
            q_vec = None
        _qa_log_append(req.query, cites, q_vec)
        return ChatResponse(answer=final_answer, citations=cites, found=True)

    # ---- Normal query path
    # Scope filter *again* after re-ranking (defensive)
    reranked = weighted_rerank(deduped, req.query, k=max(k, 12), strict=False, boosts=boosts_map)
    hits_all = [(t, m, s) for (t, m, _f, s) in reranked]
    if scope_docs:
        before_rerank_count = len(hits_all)
        before_rerank_docs = set((h[1] or {}).get("doc") for h in hits_all if h[1])
        print(f"[scope_filter] Before post-rerank filter - {before_rerank_count} hits from: {sorted(before_rerank_docs)[:5]}")
        
        hits_all = _filter_hits_to_scope(hits_all, scope_docs)
        
        after_rerank_count = len(hits_all)
        after_rerank_docs = set((h[1] or {}).get("doc") for h in hits_all if h[1])
        print(f"[scope_filter] After post-rerank filter - {after_rerank_count} hits from: {sorted(after_rerank_docs)[:5]}")

    # Build context + citations
    context_blocks, cites = [], []
    seen_pages = set()
    for text, meta, _ in hits_all:
        doc = (meta or {}).get("doc", "UnknownDoc")
        page = (meta or {}).get("page")
        key = (doc, page)
        if key in seen_pages:
            continue
        seen_pages.add(key)
        snippet = (text or "")[:1200]
        context_blocks.append(f"[Document: {doc}, Page: {page}]\n{snippet}")
        cites.append(Citation(doc=doc, page=page, snippet=_truncate_at_word_boundary(snippet, 280)))

    context_str = "\n\n---\n\n".join(context_blocks) if context_blocks else "(no matching excerpts)"
    guardrails = (
        "\n\nRules:\n"
        "- NEVER invent or estimate numbers.\n"
        "- If not present, say 'not specified in the document.'\n"
        "- Use **bold** headings, bullets for key facts.\n"
    )
    prompt = USER_PROMPT.format(question=req.query, context=context_str) + guardrails
    if linked_prev_q:
        prompt = f"Prior related user question: {linked_prev_q}\n\n" + prompt

    llm_answer = ""
    try:
        resp = client.chat.completions.create(
            model=MODEL, temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        if resp and resp.choices:
            llm_answer = (resp.choices[0].message.content or "").strip()
    except Exception:
        pass

    tables_html = _collect_table_badges(hits_all, max_tables=6)
    final_answer = (llm_answer or "Not found in uploaded PDFs.") + (("\n\n" + tables_html) if tables_html else "")

    try:
        q_vec = store._encode([req.query])[0]
    except Exception:
        q_vec = None
    _qa_log_append(req.query, cites, q_vec)

    return ChatResponse(answer=final_answer, citations=cites, found=bool(context_blocks))

# ---------- Streaming ----------
@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    k = req.k or TOP_K

    # Follow-up linker for streaming path
    linked_prev_q, extra_years = linker.link(req.query)

    # Build merged queries
    stream_subqs = expand_query(req.query)
    if linked_prev_q:
        stream_subqs += expand_query(linked_prev_q)
    for y in extra_years:
        if y not in stream_subqs:
            stream_subqs.append(y)

    # De-dup and cap sub-queries so we don’t explode retrieval work
    stream_subqs = list(dict.fromkeys(stream_subqs))[:2]


    # --- Multi-company expansion (cheap) ---
    # Very light tokenizer: keep tokens with letters/numbers and length >= 2
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", req.query)
    # Heuristic: company-like tokens are 2–10 chars and not generic words
    GENERIC = {"and","or","for","in","on","the","of","what","is","yoy","revenue","growth","capex","eps","pat","vgc","vgs"}  # keep your tickers in; remove if unwanted
    company_like = [t for t in tokens if 2 <= len(t) <= 10 and t.lower() not in GENERIC]

    # If more than one distinct company-like token appears, run per-company subqueries
    company_like = list(dict.fromkeys(company_like))[:3]  # limit to avoid explosion
    if len(company_like) >= 2:
        per_company_subqs = []
        for c in company_like:
            # bind company token with the first two subqs
            for s in stream_subqs[:2]:
                per_company_subqs.append(f"{c} {s}")
        # put them in front to bias retrieval without changing total query count much
        stream_subqs = list(dict.fromkeys(per_company_subqs + stream_subqs))[:6]  # clamp


    # --- Scope detection (company / doc type) ---
    scope_docs = _detect_doc_scope_from_query(req.query)
    print(f"[scope_filter] Query: '{req.query[:60]}...' → Detected scope: {scope_docs}")
    
    # If follow-up provides clearer scope, USE IT (don't just fallback)
    if linked_prev_q:
        prev_scope = _detect_doc_scope_from_query(linked_prev_q)
        if prev_scope:
            if not scope_docs:
                # Current query has no scope, use previous
                scope_docs = prev_scope
                print(f"[scope_filter] Using scope from linked query: '{linked_prev_q[:60]}...' → {scope_docs}")
            else:
                # Both have scope, use intersection if overlap, otherwise current query wins
                common = list(set(scope_docs) & set(prev_scope))
                if common:
                    scope_docs = common
                    print(f"[scope_filter] Using intersection of scopes: {scope_docs}")
                else:
                    print(f"[scope_filter] No overlap between scopes, using current query scope")
    
    # FALLBACK: If still no scope, check last 10 questions for ANY company mentions
    if not scope_docs:
        print(f"[scope_filter] No scope yet, checking recent conversation history...")
        recent_qs = [item.get("q", "") for item in reversed(_QA_CACHE[-10:]) if isinstance(item, dict) and item.get("q")]
        for idx, recent_q in enumerate(recent_qs):
            recent_scope = _detect_doc_scope_from_query(recent_q)
            if recent_scope:
                scope_docs = recent_scope
                print(f"[scope_filter] ✅ Found scope in recent Q-{idx}: '{recent_q[:50]}...' → {scope_docs}")
                break
        
        if not scope_docs:
            print(f"[scope_filter] ⚠️  No scope found in recent history")

    import time  # top of file once is fine
    t0 = time.time()

    
    # 1) Primary hybrid search (BM25-only / FASTEST)
    primary_hits = []
    for s in stream_subqs:
        _t = time.time()
        primary_hits.extend(_cached_search(
            s, 1.0, False, 20, 4, 0.5, max(k, 8)
        ))

        print(f"[perf-subq] primary '{s[:50]}…' -> {time.time()-_t:.3f}s")



    # 2) Numeric retrieval pass (BM25-only / FAST) with early stop
    numeric_hits = []
    seen_numeric_pages = set()

    # keep very few numeric variants
    MAX_NUMERIC_QUERIES = 3  # <= add/ensure at module level (or here)
    for nq in _make_numeric_queries(req.query, linked_prev_q)[:MAX_NUMERIC_QUERIES]:
        _t = time.time()
        hs = _cached_search(
            nq, 1.0, False, 20, 4, 0.5, max(k, 8)
        )

        for (t, m, s) in hs:
            key = (str((m or {}).get("doc")), int((m or {}).get("page") or 0))
            if _contains_numberish(t) or (m or {}).get("type") == "table" or (m or {}).get("tables"):
                if key not in seen_numeric_pages:
                    m = dict(m or {}); m["numeric_hit"] = True
                    numeric_hits.append((t, m, s))
                    seen_numeric_pages.add(key)
        print(f"[perf-subq] numeric '{nq[:50]}…' -> {time.time()-_t:.3f}s")
        if len(seen_numeric_pages) >= 3:  # early stop as soon as we have 3 pages
            break




    t1 = time.time()
    # 3) Soft scope filter AFTER merge to avoid wiping candidates
    merged = primary_hits + numeric_hits
    
    # Scope filter (to avoid cross-company leakage)
    if scope_docs:
        before_count = len(merged)
        before_docs = set((h[1] or {}).get("doc") for h in merged if h[1])
        print(f"[scope_filter] Before filter - {before_count} hits from docs: {sorted(before_docs)[:5]}")
        
        merged = _filter_hits_to_scope(merged, scope_docs)
        
        after_count = len(merged)
        after_docs = set((h[1] or {}).get("doc") for h in merged if h[1])
        print(f"[scope_filter] After filter - {after_count} hits from docs: {sorted(after_docs)[:5]}")
        print(f"[scope_filter] Filtered hits: {before_count} → {after_count} (removed {before_count - after_count})")
    else:
        print(f"[scope_filter] WARNING: No scope detected, not filtering hits (total: {len(merged)})")
        hit_docs = set((h[1] or {}).get("doc") for h in merged if h[1])
        print(f"[scope_filter] Unfiltered docs: {sorted(hit_docs)[:10]}")

    # --- One-shot hybrid booster (fires ONLY if BM25+numeric looks weak) ---
    if _needs_hybrid_boost(merged, min_numeric=3, min_total=max(8, (k or 8))):
        _t = time.time()
        # small, cheap hybrid blend; no cross-encoder
        booster = store.search_hybrid(
            req.query,
            k=max(k, 12),          # was 10
            alpha=0.40,            # was 0.35; a tad more vector signal
            llm_fn=None,
            use_ce=False,
            pre_k=36,              # was 30
            mmr_topn=8,            # was 6
            mmr_lambda=0.5,
        )


        # Mark booster hits that look numeric-ish or carry tables
        boosted = []
        seen_boost = set()
        for (t, m, s) in booster:
            key = (str((m or {}).get("doc")), int((m or {}).get("page") or 0))
            if key in seen_boost:
                continue
            m = dict(m or {})
            if _contains_numberish(t) or m.get("type") == "table" or m.get("tables"):
                m["numeric_hit"] = m.get("numeric_hit", True)
            boosted.append((t, m, s))
            seen_boost.add(key)

        # Scope filter booster results (to avoid cross-company leakage)
        if scope_docs:
            before_boost_count = len(boosted)
            before_boost_docs = set((h[1] or {}).get("doc") for h in boosted if h[1])
            print(f"[scope_filter] Before booster filter - {before_boost_count} hits from docs: {sorted(before_boost_docs)[:5]}")
            
            boosted = _filter_hits_to_scope(boosted, scope_docs)
            
            after_boost_count = len(boosted)
            after_boost_docs = set((h[1] or {}).get("doc") for h in boosted if h[1])
            print(f"[scope_filter] After booster filter - {after_boost_count} hits from docs: {sorted(after_boost_docs)[:5]}")
            print(f"[scope_filter] Booster filtered hits: {before_boost_count} → {after_boost_count} (removed {before_boost_count - after_boost_count})")

        merged += boosted
        print(f"[perf] hybrid_boost took {time.time()-_t:.3f}s, added={len(boosted)}")



    # 4) Deduplicate by (doc, page) keeping best score and merging flags/tables
    best_by_page = {}
    def _key_doc_page(tp):
        _, meta, _ = tp
        return (str((meta or {}).get("doc")), int((meta or {}).get("page") or 0))

    for tup in merged:
        kdp = _key_doc_page(tup)
        prev = best_by_page.get(kdp)
        if prev is None or tup[2] > prev[2]:
            best_by_page[kdp] = tup
        else:
            pt, pm, ps = best_by_page[kdp]
            tm, sm = dict(pm or {}), dict(tup[1] or {})
            if sm.get("numeric_hit"): tm["numeric_hit"] = True
            if sm.get("tables"): tm["tables"] = (tm.get("tables") or []) + sm["tables"]
            best_by_page[kdp] = (pt, tm, ps)

    merged_hits = list(best_by_page.values())

    # 5) Light numeric-friendly rerank with subjectivity penalty
    def _score_num(item):
        text, meta, base = item
        tl = (text or "").lower()
        # lexical signals
        kw   = _count_any(tl, FIN_KEYWORDS_BASE)            # financial terms
        nums = 0.10 * _count_numberish(text)                # gentle numeric density
        subj = -0.12 if _contains_any(tl, SUBJECTIVE_MARKERS) else 0
        nflag= 0.7  if (meta or {}).get("numeric_hit") else 0
        fsc  = float((meta or {}).get("fact_score", 0))

        # Build this once per request, near the top of chat_stream after you have req.query:
        query_toks = set(re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", (req.query or "").lower()))

        # In _score_num, after computing tl/title:
        title = ((meta or {}).get("doc_title") or (meta or {}).get("doc") or "").lower()
        title_tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", title))

        # overlap-based prior (very small, but helps multi-company)
        overlap = len(query_toks & title_tokens)
        title_boost = 0.12 * min(overlap, 5)  # cap influence

        # reciprocal-rank-like bump from base rank
        # (base may already be a composite; convert to a small monotone bump)
        rrf = 1.0 / (50.0 + max(0.0, 50.0 - min(50.0, base)))  # ~[0.019..0.02] small stabilizer

        return (base * 0.6) + kw + nums + nflag + subj + fsc + title_boost + (2.5 * rrf)


    merged_hits.sort(key=_score_num, reverse=True)

    t2 = time.time()
    print(f"[perf] primary={(t1-t0):.3f}s, merge_rerank={(t2-t1):.3f}s, total={(t2-t0):.3f}s")


    # 6) Guarantee: keep a floor of numeric chunks at the top
    NUMERIC_FLOOR = 3
    numeric_kept = []
    seen_k = set()
    for h in merged_hits:
        meta = h[1] if isinstance(h, tuple) else h.get("meta")
        if (meta or {}).get("numeric_hit"):
            key = _key_doc_page(h)
            if key not in seen_k:
                numeric_kept.append(h)
                seen_k.add(key)
        if len(numeric_kept) >= NUMERIC_FLOOR:
            break
    rest = [h for h in merged_hits if _key_doc_page(h) not in seen_k]
    hits = (numeric_kept + rest)[:max(k, len(numeric_kept))]

    # Trim long chunks to reduce token load
    def _trim_chunk(text: str, limit: int = 900) -> str:
        t = (text or "").strip()
        return t if len(t) <= limit else (t[:limit] + " …")

    hits = [(_trim_chunk(t), m, s) for (t, m, s) in hits]

    # Optional: drop near-duplicate text chunks (crude but effective)
    def _simkey(t: str) -> str:
        import re
        core = "".join(re.findall(r"[a-z0-9]+", (t or "").lower()))
        return core[:300]

    _seen = set()
    _dedup = []
    for (t, m, s) in hits:
        kkey = (_simkey(t), (m or {}).get("doc"))
        if kkey in _seen:
            continue
        _seen.add(kkey)
        _dedup.append((t, m, s))
    hits = _dedup[:k]


    # Build context & citations
    context_blocks, cites = _build_context_and_cites(hits)
    context_str = "\n\n---\n\n".join(context_blocks) if context_blocks else "(no matching excerpts)"
    
    # Debug logging
    print(f"[stream] Built {len(context_blocks)} context blocks, {len(cites)} citations")
    if context_blocks:
        print(f"[stream] Context preview: {context_str[:200]}...")
    else:
        print(f"[stream] WARNING: No context blocks built from {len(hits)} hits")
    
    header = {"type": "meta", "citations": [c.dict() for c in cites]}

    guardrails = (
        "\n\nIMPORTANT REMINDERS:\n"
        "- Synthesize information from the context to answer the question comprehensively.\n"
        "- For summarize/consensus questions, extract key themes and findings from all relevant sources.\n"
        "- Only state 'not available' for specific facts that are truly missing from the context.\n"
        "- ALWAYS cite sources using the exact format: (Filename.pdf, p.5) - do NOT use dashes, brackets, or other formats.\n"
        "- Citations must be inline within sentences using parentheses format.\n"
        "- Prefer natural-language sentences; ignore table-like number runs.\n"
    )

    tables_html = _collect_table_badges(hits, max_tables=6)

    def gen():
        yield (json.dumps(header) + "\n").encode("utf-8")
        stream = client.chat.completions.create(
            model=MODEL,
            temperature=0.1,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content":
                    ((f"Prior related user question: {linked_prev_q}\n\n") if linked_prev_q else "")
                    + USER_PROMPT.format(question=req.query, context=context_str)
                    + guardrails
                },
            ],
            stream=True,   
        )
        
        for chunk in stream:
            delta = getattr(chunk.choices[0].delta, "content", None) if chunk and chunk.choices else None
            if delta:
                yield delta.encode("utf-8")
        if tables_html:
            yield ("\n\n" + tables_html).encode("utf-8")
        yield b"\n"

    try:
        q_vec = store._encode([req.query])[0]
    except Exception:
        q_vec = None
    _qa_log_append(req.query, cites, q_vec)

    return StreamingResponse(gen(), media_type="text/plain")

# ---------- Debug / Export ----------
@app.get("/debug/grep")
def debug_grep(q: str, k: int = 12):
    hits = store.search_hybrid(q, k=max(k, 12), alpha=0.65, llm_fn=_llm_fn, use_ce=True, pre_k=80, mmr_topn=12, mmr_lambda=0.7)
    ql = q.lower()
    toks = [t for t in re.split(r"\s+", ql) if t]
    def has_token(txt: str) -> bool:
        tl = (txt or "").lower()
        return any(t in tl for t in toks)
    filtered = [(t, m, s) for (t, m, s) in hits if has_token(t)] or hits
    return [{"doc": (m or {}).get("doc"), "page": (m or {}).get("page"), "snippet": (t or "")[:400]} for (t, m, s) in filtered[:k]]

@app.get("/debug/pages")
def debug_pages(doc: str):
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
    texts = getattr(store, "texts", []) or []
    metas = getattr(store, "meta", []) or []
    out = []
    for t, m in zip(texts, metas):
        if doc and (m or {}).get("doc") != doc: continue
        out.append({"doc": (m or {}).get("doc"), "start_page": (m or {}).get("page"), "tier": (m or {}).get("tier"),
                    "chars": len(t), "snippet": (t or "")[:200]})
    out.sort(key=lambda x: ((x["doc"] or ""), (x["start_page"] or 0), (x.get("tier") or "")))
    return {"count": len(out), "chunks": out}

@app.get("/debug/tables")
def debug_tables(doc: str, regenerate: bool = False):
    from .rag import list_table_images_for_doc, render_table_images, ensure_table_images_for_doc
    pdf_path = os.path.join(DATA_DIR, doc)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, f"{doc} not found in data/")
    if regenerate:
        render_table_images(pdf_path)
        mapping = list_table_images_for_doc(pdf_path)
    else:
        mapping = ensure_table_images_for_doc(pdf_path)
    total = sum(len(v) for v in mapping.values())
    return {"doc": doc, "pages_with_tables": len(mapping), "total_images": total, "images": mapping}

@app.get("/export/text")
def export_text(doc: str):
    pdf_path = os.path.join(DATA_DIR, doc)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, f"{doc} not found in data/")
    pages = list(extract_pdf_pages(pdf_path))
    full_text = "\n\n".join(f"[p{p['page_number']}] {p['text']}" for p in pages)
    return {"doc": doc, "chars": len(full_text), "text": full_text}

@app.get("/export/json")
def export_json(doc: str):
    pdf_path = os.path.join(DATA_DIR, doc)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, f"{doc} not found in data/")
    pages = list(extract_pdf_pages(pdf_path))
    return {"doc": doc, "pages": pages}

@app.get("/export/full", response_class=PlainTextResponse)
def export_full(doc: str | None = Query(default=None, description="Optional document to restrict to")):
    def _gen_all():
        docs = _indexed_docs() if doc is None else [doc]
        if not docs:
            yield "No documents indexed.\n"; return
        for d in docs:
            yield f"===== {d} =====\n\n"
            for chunk in _iter_doc_plaintext(d): yield chunk
            yield "\n"
    return StreamingResponse(_gen_all(), media_type="text/plain; charset=utf-8")

@app.get("/export/matches")
def export_matches(
    q: str = Query(..., description="Query to retrieve raw chunks for (no LLM)"),
    k: int = Query(50, ge=1, le=500, description="How many chunks to return"),
    doc: str | None = Query(None, description="Optional document scope"),
):
    hits = store.search_hybrid(q, k=max(k, 50), alpha=0.65, llm_fn=_llm_fn, use_ce=True, pre_k=80, mmr_topn=12, mmr_lambda=0.7)
    def _stream(hits_iter):
        i = 0
        for (txt, meta, score) in hits_iter:
            if doc and ((meta or {}).get("doc") != doc): continue
            i += 1
            doc_id = (meta or {}).get("doc", "unknown_doc")
            page = (meta or {}).get("page", "unknown_page")
            yield f"## {i}  [doc={doc_id}] [page={page}] [score={score:.3f}]\n"
            yield (txt or "") + "\n\n"
            if i >= k: break
    return StreamingResponse(_stream(hits), media_type="text/plain; charset=utf-8")

@app.get("/search/doc")
def search_doc(doc: str, q: str, max_hits: int = 500):
    texts = getattr(store, "texts", []) or []
    metas  = getattr(store, "meta", []) or []
    items = [(t, m) for t, m in zip(texts, metas) if (m or {}).get("doc") == doc]
    ql = q.lower()
    hits = []
    for t, m in items:
        tl = (t or "").lower()
        if ql in tl:
            hits.append({"page": (m or {}).get("page"), "tier": (m or {}).get("tier"),
                         "chars": len(t or ""), "snippet": (t or "")[:600]})
            if len(hits) >= max_hits: break
    return {"doc": doc, "query": q, "count": len(hits), "hits": hits}

@app.get("/chunks/doc")
def chunks_doc(doc: str, offset: int = 0, limit: int = 50):
    texts = getattr(store, "texts", []) or []
    metas  = getattr(store, "meta", []) or []
    items = [{"doc": (m or {}).get("doc"),
              "page": (m or {}).get("page"),
              "tier": (m or {}).get("tier"),
              "chars": len(t or ""),
              "snippet": (t or "")[:400]}
             for t, m in zip(texts, metas) if (m or {}).get("doc") == doc]
    total = len(items); slice_ = items[offset: offset+limit]
    return {"doc": doc, "total": total, "offset": offset, "limit": limit, "items": slice_}

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui/")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse(url="https://muddywatersasia.com/wp-content/themes/opportunity/assets/img/favicon.ico")

# ---------- Entrypoint ----------
if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
