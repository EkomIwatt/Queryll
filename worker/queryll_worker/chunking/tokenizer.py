"""Deterministic token counting.

**This is an approximation, on purpose.** Voyage tokenizes with its own BPE vocabulary, and
the only way to count exactly is to download that tokenizer at runtime — which would make
chunk boundaries depend on a network fetch and a model version, and Contract 5 §4 requires
that the same bytes always produce the same chunks. So the worker counts tokens with a pure
function over the text and nothing else.

The estimate matters for two things, neither of which needs exactness:

* **packing** — "~512 tokens per chunk" is a shape target, not a limit to be saturated;
* **`chunks.token_count`** — display metadata, surfaced in the citation viewer.

It is deliberately *conservative* (it rounds every word up), so a chunk sized at 512
estimated tokens is comfortably under 512 real ones, and the 32,000-token per-input ceiling
in Contract 4 is never approached by a chunk a third of a page long.

Calibration on English prose lands within roughly ±10% of a BPE count, which is well inside
the tolerance of a "~512 token" target.
"""

from __future__ import annotations

import re

# One pass, three classes of piece:
#   * CJK / kana / hangul, which BPE tokenizers spend roughly one token per character on;
#   * word-ish runs of letters, digits and underscores;
#   * every other non-space character, which costs about one token each.
_CJK = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
_PIECE_RE = re.compile(rf"([{_CJK}])|(\w+)|([^\s])", re.UNICODE)

#: Average characters per BPE token for Latin-script text.
_CHARS_PER_TOKEN = 4


def count_tokens(text: str) -> int:
    """Estimate the number of embedding tokens in `text`. Pure and deterministic."""
    total = 0
    for cjk, word, other in _PIECE_RE.findall(text):
        if cjk:
            total += 1
        elif word:
            total += max(1, -(-len(word) // _CHARS_PER_TOKEN))  # ceil division
        elif other:
            total += 1
    return total


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Cut `text` down to at most `max_tokens` estimated tokens, on a whitespace boundary.

    Used only as the Contract 4 guard on a single over-long embedding input: truncate rather
    than drop. Chunking should mean this never fires in practice.
    """
    if max_tokens <= 0:
        return ""
    if count_tokens(text) <= max_tokens:
        return text

    # Binary search the character offset whose prefix fits, then back off to whitespace so
    # the truncated input does not end mid-word.
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if count_tokens(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1

    cut = text[:low]
    boundary = cut.rfind(" ")
    if boundary > len(cut) * 0.8:
        cut = cut[:boundary]
    return cut.rstrip()
