"""Plain-text and Markdown extraction.

For these formats the canonical text is simply the decoded file, with line endings
normalized. Nothing is rewritten, so `char_start`/`char_end` land on offsets a human can
reproduce with `content.decode()[start:end]` — which is what makes a chunking bug
reproducible from the stored bytes alone.

Markdown keeps its markup inside chunk text on purpose: `## 3.2 Sampling` reads correctly in
the citation viewer, and stripping it would put the extracted text out of step with the file
the user uploaded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from queryll_worker.errors import PermanentIngestError
from queryll_worker.parsing.base import Block, BlockKind, ExtractedDocument

_DECODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

_ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_SETEXT_H1_RE = re.compile(r"^ {0,3}=+[ \t]*$")
_SETEXT_H2_RE = re.compile(r"^ {0,3}-{2,}[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LIST_ITEM_RE = re.compile(r"^ {0,3}(?:[-*+][ \t]+|\d{1,9}[.)][ \t]+)")
_TABLE_ROW_RE = re.compile(r"^ {0,3}\|")
_THEMATIC_BREAK_RE = re.compile(
    r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})$"
)


def decode_text(content: bytes) -> str:
    """Decode uploaded bytes into the canonical string.

    Raises:
        PermanentIngestError: if the bytes are not text at all.
    """
    if b"\x00" in content[:8192]:
        raise PermanentIngestError(
            "This file looks like a binary file rather than a text document, so it could "
            "not be read."
        )
    for encoding in _DECODINGS:
        try:
            decoded = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        return decoded.replace("\r\n", "\n").replace("\r", "\n")
    raise PermanentIngestError(  # pragma: no cover - latin-1 decodes any byte string
        "The text encoding of this file could not be recognised, so it could not be read."
    )


@dataclass(frozen=True)
class _Line:
    text: str
    start: int
    end: int  # exclusive, not including the newline


def _split_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    offset = 0
    for raw in text.split("\n"):
        lines.append(_Line(raw, offset, offset + len(raw)))
        offset += len(raw) + 1
    return lines


def _paragraph_block(lines: list[_Line], first: int, last: int) -> Block:
    return Block(
        kind=BlockKind.PARAGRAPH,
        char_start=lines[first].start,
        char_end=lines[last].end,
    )


def extract_markdown(content: bytes) -> ExtractedDocument:
    """Segment Markdown into headings, paragraphs, list items and pre-formatted runs."""
    text = decode_text(content)
    lines = _split_lines(text)
    blocks: list[Block] = []

    index = 0
    total = len(lines)
    pending_start: int | None = None  # first line index of an open paragraph

    def flush(end_index: int) -> None:
        nonlocal pending_start
        if pending_start is not None and end_index >= pending_start:
            blocks.append(_paragraph_block(lines, pending_start, end_index))
        pending_start = None

    # YAML front matter is metadata, not prose: no block covers it, so nothing cites it.
    if total and lines[0].text.strip() == "---":
        for probe in range(1, total):
            if lines[probe].text.strip() in {"---", "..."}:
                index = probe + 1
                break

    while index < total:
        line = lines[index]
        stripped = line.text.strip()

        if not stripped:
            flush(index - 1)
            index += 1
            continue

        fence = _FENCE_RE.match(line.text)
        if fence:
            flush(index - 1)
            marker = fence.group(1)[0]
            close = index + 1
            while close < total and not lines[close].text.strip().startswith(marker * 3):
                close += 1
            end_line = min(close, total - 1)
            blocks.append(
                Block(
                    kind=BlockKind.PREFORMATTED,
                    char_start=line.start,
                    char_end=lines[end_line].end,
                )
            )
            index = end_line + 1
            continue

        atx = _ATX_RE.match(line.text)
        if atx:
            flush(index - 1)
            label = atx.group(2).strip()
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    char_start=line.start,
                    char_end=line.end,
                    level=len(atx.group(1)),
                    heading_text=label or None,
                )
            )
            index += 1
            continue

        # Setext: the underline turns the *previous* paragraph line into a heading.
        if pending_start is not None and index > 0:
            is_h1 = bool(_SETEXT_H1_RE.match(line.text))
            is_h2 = bool(_SETEXT_H2_RE.match(line.text))
            if is_h1 or is_h2:
                previous = lines[index - 1]
                if pending_start < index - 1:
                    blocks.append(_paragraph_block(lines, pending_start, index - 2))
                blocks.append(
                    Block(
                        kind=BlockKind.HEADING,
                        char_start=previous.start,
                        char_end=line.end,
                        level=1 if is_h1 else 2,
                        heading_text=previous.text.strip() or None,
                    )
                )
                pending_start = None
                index += 1
                continue

        if _THEMATIC_BREAK_RE.match(line.text):
            flush(index - 1)
            index += 1
            continue

        if _TABLE_ROW_RE.match(line.text):
            flush(index - 1)
            close = index
            while close + 1 < total and _TABLE_ROW_RE.match(lines[close + 1].text):
                close += 1
            blocks.append(
                Block(
                    kind=BlockKind.PREFORMATTED,
                    char_start=line.start,
                    char_end=lines[close].end,
                )
            )
            index = close + 1
            continue

        # Each list item is its own block, so a long list splits between items rather than
        # inside one.
        if _LIST_ITEM_RE.match(line.text):
            flush(index - 1)
            close = index
            while (
                close + 1 < total
                and lines[close + 1].text.strip()
                and not _LIST_ITEM_RE.match(lines[close + 1].text)
                and not _ATX_RE.match(lines[close + 1].text)
                and not _FENCE_RE.match(lines[close + 1].text)
            ):
                close += 1
            blocks.append(
                Block(
                    kind=BlockKind.PARAGRAPH,
                    char_start=line.start,
                    char_end=lines[close].end,
                )
            )
            index = close + 1
            continue

        if pending_start is None:
            pending_start = index
        index += 1

    flush(total - 1)

    if not blocks:
        raise PermanentIngestError(
            "This document contains no readable text, so there was nothing to index."
        )

    return ExtractedDocument(text=text, blocks=tuple(blocks), pages=(), page_count=None)


def extract_plain_text(content: bytes) -> ExtractedDocument:
    """Segment plain text into blank-line-separated paragraphs.

    No heading detection: there is no reliable signal for one in a `.txt` file, and Contract 5
    §3 would rather have `heading_path = NULL` than a confidently wrong path printed under a
    citation.
    """
    text = decode_text(content)
    lines = _split_lines(text)
    blocks: list[Block] = []

    pending_start: int | None = None
    for index, line in enumerate(lines):
        if line.text.strip():
            if pending_start is None:
                pending_start = index
        elif pending_start is not None:
            blocks.append(_paragraph_block(lines, pending_start, index - 1))
            pending_start = None
    if pending_start is not None:
        blocks.append(_paragraph_block(lines, pending_start, len(lines) - 1))

    if not blocks:
        raise PermanentIngestError(
            "This document contains no readable text, so there was nothing to index."
        )

    return ExtractedDocument(text=text, blocks=tuple(blocks), pages=(), page_count=None)
