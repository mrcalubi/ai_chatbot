from typing import Optional, List
from pydantic import BaseModel

class Citation(BaseModel):
    doc: str
    page: Optional[int] = None
    snippet: Optional[str] = None

class ChatRequest(BaseModel):
    query: str
    k: Optional[int] = None
    strict: Optional[bool] = None  # UI toggle per request

class ChatResponse(BaseModel):
    answer: str
    citations: List[Citation] = []
    found: bool = False
