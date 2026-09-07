"""The shape every parser produces.

There is exactly one canonical string per document — `ExtractedDocument.text` — and every
offset in the system indexes into it. `chunks.char_start` / `char_end` are offsets into this
string, and Contract 5 §5 requires `text[c.char_start:c.char_end] == c.text` for every chunk.
The chunker guarantees that by only ever producing *spans* of this string, never rebuilt or
re-joined text.
"""

from __future__ import annotations

import bisect
import enum
from dataclasses import dataclass


class BlockKind(str, enum.Enum):
    """What a structural unit is, which decides how the chunker is allowed to split it."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    #: Fenced code, tables and other pre-formatted runs: split at their edges, never inside.
    PREFORMATTED = "preformatted"


@dataclass(frozen=True)
class Block:
    """A structural unit of the document, as a span of `ExtractedDocument.text`."""

    kind: BlockKind
    char_start: int
    char_end: int
    #: Heading depth, 1-based. `None` for anything that is not a heading.
    level: int | None = None
    #: The heading's label with its markup removed — what goes into `heading_path`.
    heading_text: str | None = None


@dataclass(frozen=True)
class PageSpan:
    """The half-open character range of `text` that came from one 1-based page."""

    number: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class ExtractedDocument:
    """The parsed document: one canonical string plus the structure found in it."""

    text: str
    blocks: tuple[Block, ...]
    #: Empty for formats without pages (`.txt`, `.md`) — those chunks carry NULL page numbers.
    pages: tuple[PageSpan, ...] = ()
    page_count: int | None = None
    #: True when heading detection was judged reliable. When False the chunker emits
    #: `heading_path = NULL` for every chunk: a wrong heading is worse than no heading.
    headings_reliable: bool = True

    def slice(self, block: Block) -> str:
        return self.text[block.char_start : block.char_end]


def page_at(pages: tuple[PageSpan, ...], offset: int) -> int | None:
    """The 1-based page number containing `offset`, or None for a page-less document.

    Page spans are contiguous, non-overlapping and monotonic in `char_start` (the extractor
    walks pages in order), so this is a binary search.
    """
    if not pages:
        return None
    starts = [p.char_start for p in pages]
    idx = bisect.bisect_right(starts, offset) - 1
    if idx < 0:
        return pages[0].number
    return pages[idx].number


def page_range(pages: tuple[PageSpan, ...], start: int, end: int) -> tuple[int | None, int | None]:
    """The inclusive 1-based page range a `[start, end)` span covers (Contract 5 §5).

    A chunk that straddles a page break carries both numbers; a chunk inside one page carries
    the same number twice.
    """
    if not pages:
        return None, None
    first = page_at(pages, start)
    last = page_at(pages, max(start, end - 1))
    if first is None or last is None:
        return None, None
    return (first, last) if first <= last else (last, first)
