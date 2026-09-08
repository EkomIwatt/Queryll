"""Structure-aware chunking (Contract 5).

The chunker never builds a string. It works entirely in *spans* of
`ExtractedDocument.text`, packs those spans into chunks, and only slices the text once at the
very end. Two properties fall out of that for free, and both are load-bearing for the
product:

* `text[chunk.char_start:chunk.char_end] == chunk.text` — always, including across overlap,
  which is where this invariant is normally lost to an off-by-one. A citation that highlights
  the wrong passage is worse than no citation.
* The whole pass is a pure function of the extracted text, so the same bytes produce the same
  chunk boundaries, ordinals, offsets and token counts on every run and in every process
  (Contract 5 §4) — which is what makes a re-index safe to run at any time.

Structure comes first and size second (Contract 5 §3): the unit list is built from headings,
paragraphs and sentences, and packing only ever chooses *how many whole units* fit. Nothing
is split mid-sentence unless a single sentence is longer than an entire chunk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from queryll_worker.chunking.sentences import line_spans, sentence_spans, word_spans
from queryll_worker.chunking.tokenizer import count_tokens
from queryll_worker.config import ChunkingSettings
from queryll_worker.parsing.base import BlockKind, ExtractedDocument, page_range

_WS_RE = re.compile(r"\s+")
_HEADING_MARKUP_RE = re.compile(r"^#{1,6}\s+|\s+#*\s*$")


@dataclass(frozen=True)
class ChunkCandidate:
    """One chunk, ready to be embedded and written. Mirrors the `chunks` table."""

    ordinal: int
    text: str
    token_count: int
    char_start: int
    char_end: int
    page_start: int | None
    page_end: int | None
    heading_path: str | None


@dataclass(frozen=True)
class _Unit:
    """The smallest thing the packer is allowed to move: a heading or a sentence."""

    char_start: int
    char_end: int
    tokens: int
    heading_path: str | None
    is_heading: bool


def _heading_label(document: ExtractedDocument, block) -> str:  # type: ignore[no-untyped-def]
    """The text that goes into `heading_path`, taken from the document itself."""
    if block.heading_text:
        return _WS_RE.sub(" ", block.heading_text).strip()
    raw = document.text[block.char_start : block.char_end]
    return _WS_RE.sub(" ", _HEADING_MARKUP_RE.sub("", raw)).strip()


def _split_oversized(
    document: ExtractedDocument,
    start: int,
    end: int,
    *,
    target: int,
    preformatted: bool,
) -> list[tuple[int, int]]:
    """Break a unit that is bigger than a whole chunk into span-sized pieces.

    Pre-formatted blocks break at line ends so a code block or table keeps its shape; prose
    breaks at word boundaries, which is the only place Contract 5 §3 permits a mid-sentence
    split at all.
    """
    pieces = (
        line_spans(document.text, start, end)
        if preformatted
        else word_spans(document.text, start, end)
    )
    if not pieces:
        return [(start, end)]

    spans: list[tuple[int, int]] = []
    current_start = pieces[0][0]
    current_end = pieces[0][1]
    current_tokens = count_tokens(document.text[current_start:current_end])

    for piece_start, piece_end in pieces[1:]:
        piece_tokens = count_tokens(document.text[piece_start:piece_end])
        if current_tokens + piece_tokens > target:
            spans.append((current_start, current_end))
            current_start, current_end, current_tokens = (
                piece_start,
                piece_end,
                piece_tokens,
            )
        else:
            current_end = piece_end
            current_tokens += piece_tokens
    spans.append((current_start, current_end))
    return spans


def _build_units(document: ExtractedDocument, settings: ChunkingSettings) -> list[_Unit]:
    """Flatten the document's blocks into the ordered unit list the packer consumes."""
    units: list[_Unit] = []
    stack: list[tuple[int, str]] = []  # (level, label)

    for block in document.blocks:
        if block.kind is BlockKind.HEADING:
            level = block.level or 1
            label = _heading_label(document, block)
            while stack and stack[-1][0] >= level:
                stack.pop()
            if label:
                stack.append((level, label))
            path = " > ".join(text for _, text in stack) or None
            units.append(
                _Unit(
                    char_start=block.char_start,
                    char_end=block.char_end,
                    tokens=count_tokens(document.text[block.char_start : block.char_end]),
                    heading_path=path,
                    is_heading=True,
                )
            )
            continue

        path = " > ".join(text for _, text in stack) or None
        preformatted = block.kind is BlockKind.PREFORMATTED
        spans = (
            [(block.char_start, block.char_end)]
            if preformatted
            else sentence_spans(document.text, block.char_start, block.char_end)
        )
        for span_start, span_end in spans:
            tokens = count_tokens(document.text[span_start:span_end])
            if tokens <= settings.target_tokens:
                units.append(
                    _Unit(
                        char_start=span_start,
                        char_end=span_end,
                        tokens=tokens,
                        heading_path=path,
                        is_heading=False,
                    )
                )
                continue
            for piece_start, piece_end in _split_oversized(
                document,
                span_start,
                span_end,
                target=settings.target_tokens,
                preformatted=preformatted,
            ):
                units.append(
                    _Unit(
                        char_start=piece_start,
                        char_end=piece_end,
                        tokens=count_tokens(document.text[piece_start:piece_end]),
                        heading_path=path,
                        is_heading=False,
                    )
                )
    return units


def _overlap_tail(units: list[_Unit], packed: list[int], budget: int) -> list[int]:
    """The trailing units of a finished chunk that should also open the next one.

    Capped at all-but-one unit: carrying an entire chunk forward would make the next chunk a
    superset of it and, with a large enough unit, never terminate.
    """
    if budget <= 0 or len(packed) <= 1:
        return []
    tail: list[int] = []
    total = 0
    for index in reversed(packed[1:]):
        tokens = units[index].tokens
        if total + tokens > budget:
            break
        tail.insert(0, index)
        total += tokens
    return tail


def _pack(units: list[_Unit], settings: ChunkingSettings) -> list[list[int]]:
    """Group units into chunks of roughly `target_tokens`, overlapping by `overlap_tokens`."""
    chunks: list[list[int]] = []
    current: list[int] = []
    current_tokens = 0

    for index, unit in enumerate(units):
        # A heading starts a section, so it starts a chunk — and it carries no overlap from
        # the section above it, which would otherwise open every section with the tail of the
        # previous one.
        if current and unit.is_heading and current_tokens >= settings.min_tokens:
            chunks.append(current)
            current, current_tokens = [], 0
        elif (
            current
            and current_tokens + unit.tokens > settings.target_tokens
            and current_tokens >= settings.min_tokens
        ):
            chunks.append(current)
            current = _overlap_tail(units, current, settings.overlap_tokens)
            current_tokens = sum(units[i].tokens for i in current)

        current.append(index)
        current_tokens += unit.tokens

    if current:
        chunks.append(current)
    return chunks


def _merge_short(
    units: list[_Unit], packed: list[list[int]], settings: ChunkingSettings
) -> list[list[int]]:
    """Fold chunks below `min_tokens` into their neighbour (Contract 5 §2).

    A 12-token chunk retrieves badly and reads as a fragment under a citation, so it is
    merged forward — or backward, when it is the last one.
    """
    if not packed:
        return packed

    merged: list[list[int]] = []
    carry: list[int] = []
    for chunk in packed:
        combined = carry + [i for i in chunk if i not in carry]
        if sum(units[i].tokens for i in combined) < settings.min_tokens:
            carry = combined
            continue
        merged.append(combined)
        carry = []

    if carry:
        if merged:
            last = merged[-1]
            merged[-1] = last + [i for i in carry if i not in last]
        else:
            merged.append(carry)
    return merged


def _path_for(units: list[_Unit], group: list[int]) -> str | None:
    """The heading path to show under a citation of this chunk.

    Normally the chunk opens with its own heading, because packing forces a boundary there,
    and the first unit's path is the answer. The exception is a chunk that begins in matter
    before any heading — a paper's title block, a preamble — where the first unit has no path
    at all. Falling through to the first heading the chunk *does* contain labels that passage
    with the section it is mostly made of, rather than with nothing.
    """
    for index in group:
        if units[index].heading_path:
            return units[index].heading_path
    return None


def chunk_document(
    document: ExtractedDocument, settings: ChunkingSettings | None = None
) -> list[ChunkCandidate]:
    """Split an extracted document into ordered, overlapping, citable chunks.

    Returns an empty list only when the document has no text worth indexing; the caller turns
    that into a permanent failure with copy the user can act on.
    """
    settings = settings or ChunkingSettings()
    units = _build_units(document, settings)
    if not units:
        return []

    packed = _merge_short(units, _pack(units, settings), settings)

    candidates: list[ChunkCandidate] = []
    seen: set[tuple[int, int]] = set()
    for group in packed:
        if not group:
            continue
        start = min(units[i].char_start for i in group)
        end = max(units[i].char_end for i in group)
        if end <= start or (start, end) in seen:
            continue
        seen.add((start, end))

        text = document.text[start:end]
        page_start, page_end = page_range(document.pages, start, end)
        heading_path = _path_for(units, group) if document.headings_reliable else None

        candidates.append(
            ChunkCandidate(
                ordinal=len(candidates),
                text=text,
                token_count=count_tokens(text),
                char_start=start,
                char_end=end,
                page_start=page_start,
                page_end=page_end,
                heading_path=heading_path,
            )
        )
    return candidates
