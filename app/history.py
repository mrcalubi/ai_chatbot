import re
import json
from typing import List, Tuple, Optional
from openai import OpenAI, Client

FOLLOWUP_PAT = re.compile(
    r"\b(previous year|prior year|last year|same year|next year|that|those|it|them|this|the former|the latter|and for|how about|what about|breakdown|by quarter|per quarter|details)\b",
    re.IGNORECASE
)

class RecentContextLinker:
    """
    Lightweight follow-up linker that:
    1) Heuristically detects context-dependent phrasing.
    2) Asks the LLM (JSON-only) which recent question best links to the current one.
    3) Rolls years (prev/next/same) from the linked question when applicable.

    It reads the QA cache (list of dicts) passed by reference from main.py,
    but does NOT write to it. Logging remains in main.py.
    """

    def __init__(self, client: Client, model: str, qa_cache_ref: List[dict], max_recent: int = 6):
        self.client: Client = client
        self.model = model
        self.qa_cache_ref = qa_cache_ref
        self.max_recent = max_recent

    def _recent_questions(self) -> List[str]:
        return [it.get("q", "") for it in reversed(self.qa_cache_ref[-self.max_recent:]) if isinstance(it, dict) and it.get("q")]

    @staticmethod
    def _looks_context_dependent(q: str) -> bool:
        q = q or ""
        return bool(FOLLOWUP_PAT.search(q)) or (len(q.split()) <= 8 and not re.search(r"\b\d{4}\b", q))

    @staticmethod
    def _roll_years_followup(curr_q: str, base_q: str) -> List[str]:
        years = [int(y) for y in re.findall(r"\b(20[0-9]{2})\b", base_q or "")]
        out: List[str] = []
        cq = (curr_q or "").lower()
        if not years:
            return out
        if re.search(r"\b(previous|prior|last)\s+year\b", cq):
            out.extend(str(y - 1) for y in years)
        if re.search(r"\bnext\s+year\b", cq):
            out.extend(str(y + 1) for y in years)
        if re.search(r"\bsame\s+year\b", cq):
            out.extend(str(y) for y in years)
        # de-dup preserve order
        return list(dict.fromkeys(out))

    def _llm_pick(self, current_q: str, recent_qs: List[str]) -> Tuple[bool, Optional[int]]:
        if not recent_qs:
            return (False, None)
        instr = (
            "Decide if the current question requires context from a prior question. "
            "If yes, pick exactly ONE prior question index that best resolves it. "
            "Return ONLY JSON: {\"use\": true|false, \"index\": integer_or_null} "
            "(0 = most recent)."
        )
        numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(recent_qs))
        prompt = f"{instr}\nCurrent: {current_q}\n\nRecent (0=most recent):\n{numbered}"
        try:
            resp = self.client.chat.completions.create(
                model=self.model, temperature=0,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
            )
            raw = (resp.choices[0].message.content or "").strip()
            m = re.search(r'\{.*\}', raw, re.S)
            if not m: return (False, None)
            js = json.loads(m.group(0))
            use = bool(js.get("use"))
            idx = js.get("index", None)
            if use and isinstance(idx, int) and 0 <= idx < len(recent_qs):
                return (True, idx)
        except Exception:
            pass
        return (False, None)

    def link(self, current_q: str) -> Tuple[Optional[str], List[str]]:
        """
        Returns (linked_previous_question_or_None, extra_year_strings)
        """
        if not self._looks_context_dependent(current_q):
            return (None, [])
        recent = self._recent_questions()
        use, idx = self._llm_pick(current_q, recent)
        if use and idx is not None:
            prev_q = recent[idx]
            extra_years = self._roll_years_followup(current_q, prev_q)
            return (prev_q, extra_years)
        return (None, [])
