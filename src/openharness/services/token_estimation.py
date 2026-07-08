"""Simple token estimation utilities."""

from __future__ import annotations

import re

# CJK-ish codepoints tokenize at roughly one token per character in modern
# BPE vocabularies, unlike English-like text at roughly four characters per
# token. Treating them the same underestimates Chinese conversations by 4-6x,
# which makes auto-compact fire far too late.
_CJK_RE = re.compile(
    "["
    "⺀-〿"  # CJK radicals, Kangxi radicals, CJK punctuation
    "぀-ヿ"  # Hiragana, Katakana
    "㐀-䶿"  # CJK unified ideographs extension A
    "一-鿿"  # CJK unified ideographs
    "가-힯"  # Hangul syllables
    "豈-﫿"  # CJK compatibility ideographs
    "＀-￯"  # fullwidth / halfwidth forms
    "]"
)


def estimate_tokens(text: str) -> int:
    """Estimate tokens from plain text using a character heuristic.

    CJK characters count as one token each; everything else uses the
    ~4-characters-per-token approximation.
    """
    if not text:
        return 0
    cjk_count = len(text) - len(_CJK_RE.sub("", text))
    other_count = len(text) - cjk_count
    return max(1, cjk_count + (other_count + 3) // 4)


def estimate_message_tokens(messages: list[str]) -> int:
    """Estimate tokens for a collection of message strings."""
    return sum(estimate_tokens(message) for message in messages)
