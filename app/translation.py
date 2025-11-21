"""Translation utilities for Vietnamese → English text."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from openai import OpenAI

_OPENAI_MODEL = os.getenv("TRANSLATION_MODEL", "gpt-4o-mini")
_OPENAI_TEMPERATURE = float(os.getenv("TRANSLATION_TEMPERATURE", "0.1"))

_client: Optional[OpenAI] = None


def _get_client() -> Optional[OpenAI]:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        _client = OpenAI(api_key=api_key)
    return _client


@lru_cache(maxsize=1024)
def translate_text(text: str, source_lang: str = "vi", target_lang: str = "en") -> Optional[str]:
    """Translate text using OpenAI chat completions. Cached by full text string."""
    text = (text or "").strip()
    if not text:
        return None
    client = _get_client()
    if client is None:
        return None

    prompt = (
        f"Translate the following {source_lang.upper()} financial analysis into {target_lang.upper()}.
"
        f"Keep numbers, company names, tickers, and units unchanged."
    )
    try:
        response = client.chat.completions.create(
            model=_OPENAI_MODEL,
            temperature=_OPENAI_TEMPERATURE,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=2000,
        )
        translated = (response.choices[0].message.content or "").strip()
        if translated:
            return translated
    except Exception as exc:
        print(f"[translation] OpenAI translation failed: {exc}")
    return None
