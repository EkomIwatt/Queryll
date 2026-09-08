"""Deterministic sentence segmentation, returned as spans rather than strings.

Everything in the chunker works in spans of the canonical text, never in rebuilt strings.
That is the whole trick behind Contract 5 §5: if a chunk is only ever
`(first_unit.start, last_unit.end)`, then `text[char_start:char_end] == chunk.text` holds by
construction and cannot be broken by an off-by-one in the overlap logic.

No model, no training data, no `nltk.download()` — a regex and a list of abbreviations. Same
bytes, same sentences, forever (Contract 5 §4).
"""

from __future__ import annotations

import re

#: Words that end in a period without ending a sentence.
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "rev", "hon",
        "e.g", "i.e", "eg", "ie", "etc", "vs", "cf", "al", "ca", "approx",
        "fig", "figs", "eq", "eqs", "no", "nos", "vol", "vols", "ch", "chap",
        "sec", "secs", "p", "pp", "para", "ref", "refs", "ed", "eds",
        "inc", "ltd", "co", "corp", "dept", "univ", "assn",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
        "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    }
)

#: A candidate boundary: terminal punctuation, optional closers, then whitespace.
_BOUNDARY_RE = re.compile(r"[.!?…]['\"’”)\]]*(?=\s)")  # noqa: RUF001 - curly quotes are real punctuation
#: The token immediately before the punctuation, used for the abbreviation check.
_TRAILING_WORD_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")
#: How far back to look for that token. Only the word touching the punctuation matters, and
#: taking the whole prefix instead would copy the document once per sentence — which turns
#: chunking a large file from linear into quadratic.
_LOOKBEHIND = 48
_OPENERS = "\"'‘“([{"  # noqa: RUF001 - curly quotes are real punctuation


def _is_boundary(text: str, punct_index: int, next_index: int) -> bool:
    """Judge one candidate boundary inside `text` (absolute offsets)."""
    if text[punct_index] in "!?…":
        return True

    prefix = text[max(0, punct_index - _LOOKBEHIND) : punct_index]
    match = _TRAILING_WORD_RE.search(prefix)
    if match:
        word = match.group(1)
        # "J. Smith", "R. J. Doe" — a lone initial is not the end of a sentence.
        if len(word) == 1 and word.isupper():
            return False
        if word.rstrip(".").lower() in _ABBREVIATIONS:
            return False
        # "U.S." / "e.g." style: an interior period means the word is an abbreviation.
        interior = word[:-1] if word.endswith(".") else word
        if "." in interior:
            return False

    following = text[next_index]
    return following.isupper() or following.isdigit() or following in _OPENERS


def sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split `text[start:end]` into sentence spans with whitespace trimmed from the edges.

    Spans are ordered and non-overlapping, and the only characters between consecutive spans
    are whitespace — so a run of them can be re-joined by taking `text[first.start:last.end]`
    and getting the original substring back verbatim.
    """
    region_start, region_end = _trim(text, start, end)
    if region_start >= region_end:
        return []

    spans: list[tuple[int, int]] = []
    cursor = region_start
    for match in _BOUNDARY_RE.finditer(text, region_start, region_end):
        punct_index = match.start()
        after = match.end()
        if after >= region_end:
            continue
        # Skip past the whitespace to the character that opens the next sentence.
        probe = after
        while probe < region_end and text[probe].isspace():
            probe += 1
        if probe >= region_end:
            continue
        if not _is_boundary(text, punct_index, probe):
            continue
        span_start, span_end = _trim(text, cursor, after)
        if span_end > span_start:
            spans.append((span_start, span_end))
        cursor = probe

    tail_start, tail_end = _trim(text, cursor, region_end)
    if tail_end > tail_start:
        spans.append((tail_start, tail_end))
    return spans


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def word_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Word spans, used only to break a single sentence that is longer than a whole chunk."""
    return [
        (start + match.start(), start + match.end())
        for match in re.finditer(r"\S+", text[start:end])
    ]


def line_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Line spans, used to break an over-long pre-formatted block without reflowing it."""
    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        newline = text.find("\n", cursor, end)
        stop = end if newline == -1 else newline
        span_start, span_end = _trim(text, cursor, stop)
        if span_end > span_start:
            spans.append((span_start, span_end))
        cursor = stop + 1
    return spans
