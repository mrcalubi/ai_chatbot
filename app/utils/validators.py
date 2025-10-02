from __future__ import annotations
import re
from typing import List, Tuple

# Matches currencies and common finance suffixes (bn/b/m/mm/k)
_NUM_RE = re.compile(
    r"(?<![A-Za-z])[\$€£]?\d[\d,]*(?:\.\d+)?(?:\s*(?:bn|b|m|mm|k))?",
    re.IGNORECASE,
)

# NEW: patterns to strip page/citation labels from the *answer* before checking
_PAGE_PAREN_RE = re.compile(r"\([^)]*\bp\.\s*\d+\)", re.IGNORECASE)    # e.g. "(Doc.pdf, p.13)"
_PAGE_TOKEN_RE = re.compile(r"\b(?:p|page)\.?\s*\d+\b", re.IGNORECASE) # e.g. "p.13" or "page 7"
_DOC_BRACKET_RE = re.compile(r"\[[^\]]*\bp\d+\]", re.IGNORECASE)       # e.g. "[DOC=..., p12]"

_LIST_INDEX_RE  = re.compile(r"(?m)^\s*\d{1,2}[.)](?=\s)")

def _sanitize_answer(text: str) -> str:
    t = _PAGE_PAREN_RE.sub("", text or "")
    t = _PAGE_TOKEN_RE.sub("", t)
    t = _DOC_BRACKET_RE.sub("", t)
    t = _LIST_INDEX_RE.sub("", t)
    return t

def extract_numbers(s: str) -> List[str]:
    return _NUM_RE.findall(s or "")

def _normalize(n: str) -> str:
    return (n or "").replace(",", "").strip().lower()

def numbers_proved(answer: str, citations_texts: List[str]) -> bool:
    # NEW: sanitize page/page-chip numbers out of the answer first
    answer_clean = _sanitize_answer(answer)
    ans_nums = [_normalize(n) for n in extract_numbers(answer_clean)]
    if not ans_nums:
        return True  # nothing numeric to prove

    src_norm = set()
    for t in citations_texts or []:
        for n in extract_numbers(t):
            src_norm.add(_normalize(n))

    for n in ans_nums:
        if n not in src_norm and n.lstrip("$€£") not in src_norm:
            return False
    return True

def enforce_truth(answer: str, cited_texts: List[str]) -> Tuple[str, bool, List[str]]:
    answer_clean = _sanitize_answer(answer)  # NEW
    if not extract_numbers(answer_clean):
        return answer, True, []

    if numbers_proved(answer, cited_texts):
        return answer, True, []

    safe = (
        "I can’t verify the specific figures from the provided sources. "
        "Here are relevant excerpts instead:\n\n"
        + "\n\n---\n\n".join((cited_texts or [])[:6])
    )
    return safe, False, ["numbers_not_verified"]

# app/utils/validators.py (append this near the bottom)

def enforce_truth_soft(answer: str, cited_texts: List[str]) -> Tuple[str, bool, List[str]]:
    """
    Softer guard: keep the answer, but redact any numbers that are not present in sources.
    Returns (new_text, ok, flags). ok=False if any redactions happened.
    """
    answer_clean = _sanitize_answer(answer)
    nums = extract_numbers(answer_clean)
    if not nums:
        return answer, True, []

    # build source-num set
    src_norm = set()
    for t in cited_texts or []:
        for n in extract_numbers(t):
            src_norm.add(_normalize(n))

    # find unverified numbers (by normalized token)
    unverified = []
    for n in nums:
        norm = _normalize(n)
        if norm not in src_norm and norm.lstrip("$€£") not in src_norm:
            unverified.append(n)

    if not unverified:
        return answer, True, []

    # redact unverified numbers in the original (non-sanitized) answer text
    redacted = answer
    for n in sorted(set(unverified), key=len, reverse=True):
        # replace only exact token matches; keep punctuation
        pattern = re.escape(n)
        redacted = re.sub(pattern, "[unverified]", redacted)

    note = "\n\n⚠️ Some numbers were not found in sources and were redacted."
    return redacted + note, False, ["numbers_redacted"]

