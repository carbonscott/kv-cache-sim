"""Token counting for pasted text.

Uses a tiktoken encoding (default o200k_base, set via config.tokenizer). This is an
*approximation* -- o200k_base is OpenAI's tokenizer; Anthropic does not publish a
public tokenizer for the 4.x models, and newer Opus reportedly runs heavier on the
same text. Counts are realistic and consistent, not true Anthropic counts. The
encoding name is config-swappable for exactly this reason.

If tiktoken (or the encoding's vocab) is unavailable, falls back to a rough
chars/4 estimate so the tool still runs offline.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=None)
def _get_encoding(encoding_name: str):
    """Load and cache a tiktoken encoding, or return None if unavailable."""
    try:
        import tiktoken

        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return None


def count_tokens(text: str, encoding_name: str = "o200k_base") -> int:
    """Count tokens in `text` using the named encoding.

    Falls back to len(text) // 4 (a common rough heuristic) if the encoding can't
    be loaded.
    """
    encoding = _get_encoding(encoding_name)
    if encoding is None:
        return max(0, len(text) // 4)
    return len(encoding.encode(text))


def is_approximate(encoding_name: str = "o200k_base") -> bool:
    """True if we fell back to the heuristic (the real encoding didn't load)."""
    return _get_encoding(encoding_name) is None
