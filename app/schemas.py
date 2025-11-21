from typing import Optional, List
from pydantic import BaseModel

class Citation(BaseModel):
    doc: str
    page: Optional[int] = None
    snippet: Optional[str] = None
    language: Optional[str] = None  # NEW: Language of source document ('vi', 'en', 'mixed')
    original_text: Optional[str] = None  # NEW: Original text (for Vietnamese PDFs)
    translated_text: Optional[str] = None  # NEW: Translated text (for Vietnamese PDFs)

class ChatRequest(BaseModel):
    query: str
    k: Optional[int] = None
    strict: Optional[bool] = None  # UI toggle per request

class ChatResponse(BaseModel):
    answer: str
    citations: List[Citation] = []
    found: bool = False
