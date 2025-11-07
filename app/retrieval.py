# ============================================================================
# NOTICE: This file is an ALTERNATIVE IMPLEMENTATION that is NOT currently
# used by the main application (app/main.py).
#
# The active retrieval logic is in app/rag.py (VectorStore.search_hybrid).
# This file provides a simpler interface (BM25Index, VectorIndex, hybrid_search)
# but is kept for reference or future experimentation.
#
# To use this instead of rag.py, you would need to:
# 1. Adapt VectorIndex to work with your FAISS store
# 2. Replace search calls in main.py with functions from this module
# ============================================================================

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Any, Iterable, Optional, Tuple
import re
from collections import OrderedDict

from rank_bm25 import BM25Okapi
from rapidfuzz.fuzz import partial_ratio

try:
    from sentence_transformers import CrossEncoder
    _ce_model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
except Exception:
    _ce_model = None  # fallback to simple scoring

NUMERIC_METRIC_RE = re.compile(
    r"(capex|capital\s+expenditure|revenue|turnover|roi|irr|ebitda|wacc|cost\s+of\s+equity)"
    r"(?s).{0,120}?"
    r"(\d[\d,\.]*\s*(%|bn|billion|m|million|usd|vnd|trillion|tn|mn))",
    re.I,
)

@dataclass(frozen=True)
class Hit:
    doc: str
    page: Optional[int]
    text: str
    score: float
    meta: Dict[str, Any]

def _uniq(seq: Iterable[Hit]) -> List[Hit]:
    seen = set()
    out = []
    for h in seq:
        key = (h.doc, h.page, h.text[:160])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out

class BM25Index:
    """Tiny in-memory BM25 index fed from your existing corpus/chunk store."""
    def __init__(self, documents: List[Dict[str, Any]]):
        # each doc: {"doc": str, "page": int, "text": str, "meta": {...}}
        self.rows = documents
        self._tokenized = [self._tok(r["text"]) for r in documents]
        self._bm25 = BM25Okapi(self._tokenized)

    @staticmethod
    def _tok(s: str) -> List[str]:
        return re.findall(r"[A-Za-z0-9%$€¥₫.,-]+", s.lower())

    def search(self, q: str, k: int = 32) -> List[Hit]:
        scores = self._bm25.get_scores(self._tok(q))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        hits: List[Hit] = []
        for i in order:
            r = self.rows[i]
            hits.append(Hit(
                doc=r["doc"], page=r.get("page"), text=r["text"],
                score=float(scores[i]), meta=r.get("meta", {}),
            ))
        return hits

def cosine(a, b):
    import numpy as np
    na = np.linalg.norm(a); nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))

class VectorIndex:
    """Adapter around your existing vector DB; fill in `query` to return Hits."""
    def __init__(self, vdb):
        self.vdb = vdb  # whatever you already use (FAISS/Chroma/etc.)

    def search(self, q: str, k: int = 32) -> List[Hit]:
        # expected vdb.search returns list of dicts: doc,page,text,score,meta
        rows = self.vdb.search(q, k=k)
        hits = [
            Hit(doc=r["doc"], page=r.get("page"), text=r["text"], score=float(r["score"]), meta=r.get("meta", {}))
            for r in rows
        ]
        return hits

def hybrid_search(q: str, vdb: VectorIndex, bm25: BM25Index, k_vec=32, k_bm25=32) -> List[Hit]:
    a = vdb.search(q, k=k_vec)
    b = bm25.search(q, k=k_bm25)
    # simple score normalization + merge
    allhits = _uniq(a + b)
    return allhits

def rerank(query: str, hits: List[Hit], top_n: int = 8) -> List[Hit]:
    if not hits:
        return []
    if _ce_model is not None:
        pairs = [[query, h.text] for h in hits]
        scores = _ce_model.predict(pairs)
        ranked = sorted(zip(hits, scores), key=lambda x: float(x[1]), reverse=True)[:top_n]
        return [Hit(h.doc, h.page, h.text, float(s), h.meta) for (h, s) in ranked]
    # fallback: fuzzy + quick heuristic
    def cheap_score(h: Hit) -> float:
        return 0.6 * partial_ratio(query, h.text) + 0.4 * len(NUMERIC_METRIC_RE.findall(h.text))
    ranked = sorted(hits, key=cheap_score, reverse=True)[:top_n]
    return ranked

def filter_numeric_evidence(hits: List[Hit]) -> List[Hit]:
    out = []
    for h in hits:
        if NUMERIC_METRIC_RE.search(h.text):
            out.append(h)
    return out

SYN_EXPANSIONS = [
    ("capex", "capital expenditure investment maintenance growth capex"),
    ("revenue", "turnover sales"),
    ("roi", "return IRR payback"),
    ("guidance", "forecast outlook"),
    ("currency", "VND USD billion bn million m"),
]

def expand_query(q: str) -> str:
    ql = q.lower()
    extras = []
    for key, exp in SYN_EXPANSIONS:
        if key in ql:
            extras.append(exp)
    # lightly add years if present across the corpus is handled elsewhere; keep simple here
    return q + " " + " ".join(extras)
