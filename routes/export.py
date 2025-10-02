"""
Export endpoints for analyst-friendly retrieval:
- /export/full: dump full raw text for a doc (or all docs) without LLM.
- /export/matches: dump top-k raw chunks for a query (big+small), no LLM.
"""

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from typing import Optional, Iterable

from rag import hybrid_search, iter_all_docs_text  # make sure these exist in rag.py

router = APIRouter(prefix="/export", tags=["export"])

@router.get("/full", response_class=PlainTextResponse)
def export_full(doc: Optional[str] = Query(default=None, description="Optional document ID/name")):
    """
    If `doc` is provided, return concatenated plain text for that doc.
    Else, return all docs' text.
    """
    content = iter_all_docs_text(doc)
    if isinstance(content, str):
        return content

    def _gen():
        for chunk in content:
            yield chunk
    return StreamingResponse(_gen(), media_type="text/plain; charset=utf-8")

@router.get("/matches")
def export_matches(
    q: str = Query(..., description="Query to retrieve raw chunks for (no LLM)"),
    k: int = Query(50, ge=1, le=500, description="How many chunks to return"),
    doc: Optional[str] = Query(None, description="Optional document scope"),
):
    """
    Return the top-k raw chunks (big+small) for a query, without any model generation.
    """
    hits = hybrid_search(q, k=k, doc=doc, include_big_small=True)

    def _stream(hits_iter: Iterable):
        for i, h in enumerate(hits_iter, 1):
            meta = getattr(h, "meta", {}) or {}
            doc_id = meta.get("doc", "unknown_doc")
            page = meta.get("page", "unknown_page")
            score = getattr(h, "score", None)
            txt = getattr(h, "text", "")
            line1 = f"## {i}  [doc={doc_id}] [page={page}]"
            if score is not None:
                line1 += f" [score={score:.3f}]"
            yield line1 + "\n"
            yield txt + "\n\n"

    return StreamingResponse(_stream(hits), media_type="text/plain; charset=utf-8")
