"""PDF extraction: geometry in, one canonical string plus structure out.

The output of this module is what every citation in the product is ultimately made of, so it
is built to be honest rather than clever. Running headers and footers are removed, columns
are read in the right order, paragraphs are rejoined across line and page breaks, and page
numbers are tracked per line so a chunk straddling a page break carries both of them.

`ExtractedDocument.text` here is a *reconstruction*, not a byte range of the file — that is
unavoidable for PDF. What is guaranteed is that it is reconstructed deterministically from
the bytes alone (Contract 5 §4), so any chunking bug can be reproduced from the stored
`documents.content` and nothing else.
"""

from __future__ import annotations

import io
import logging
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from queryll_worker.errors import PermanentIngestError
from queryll_worker.logging_setup import kv
from queryll_worker.parsing.base import Block, BlockKind, ExtractedDocument, PageSpan
from queryll_worker.parsing.pdf_layout import (
    HeadingModel,
    Line,
    build_heading_model,
    find_running_lines,
    page_lines,
)

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"

#: A line is treated as ending its paragraph if it stops before this share of its column.
_FULL_LINE_SHARE = 0.80
#: A first line indented by more than this share of the column starts a new paragraph.
_INDENT_SHARE = 0.02
#: A vertical gap larger than this multiple of the line height starts a new paragraph.
_PARAGRAPH_GAP_RATIO = 0.75
#: Below this many extracted characters a PDF is treated as having no text at all.
_MIN_MEANINGFUL_CHARS = 20


@dataclass(frozen=True)
class _ColumnMetrics:
    left: float
    right: float

    @property
    def width(self) -> float:
        return max(1.0, self.right - self.left)


def _require_pdf(content: bytes) -> None:
    if not content:
        raise PermanentIngestError(
            "This file is empty, so there was nothing to index."
        )
    # An extension that lies about the content is a real upload, not a hypothetical.
    if not content.lstrip()[:1024].startswith(PDF_MAGIC):
        raise PermanentIngestError(
            "This file is not a readable PDF, even though it was uploaded as one."
        )


def _open_pdf(content: bytes) -> Any:
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from pdfminer.pdfparser import PDFSyntaxError

    try:
        return pdfplumber.open(io.BytesIO(content))
    except PDFPasswordIncorrect as exc:
        raise PermanentIngestError(
            "This PDF is password-protected, so its text could not be read.",
            detail="pdf is encrypted",
        ) from exc
    except PDFSyntaxError as exc:
        raise PermanentIngestError(
            "This PDF is damaged and could not be opened.",
            detail="pdf syntax error",
        ) from exc
    except Exception as exc:  # pdfminer raises a wide variety of parse errors
        raise PermanentIngestError(
            "This PDF could not be opened. The file may be damaged.",
            detail=f"{type(exc).__name__}",
        ) from exc


def _collect_lines(pdf: Any, *, max_pages: int) -> tuple[list[Line], int, bool]:
    """Read every page into ordered lines. Returns (lines, page_count, saw_images)."""
    page_count = len(pdf.pages)
    if page_count == 0:
        raise PermanentIngestError(
            "This PDF has no pages, so there was nothing to index."
        )
    if page_count > max_pages:
        raise PermanentIngestError(
            f"This PDF has {page_count} pages. Queryll can index documents of up to "
            f"{max_pages} pages."
        )

    lines: list[Line] = []
    saw_images = False
    for index, page in enumerate(pdf.pages, start=1):
        try:
            words = page.extract_words(
                extra_attrs=["size", "fontname"],
                keep_blank_chars=False,
                use_text_flow=False,
            )
            saw_images = saw_images or bool(page.images)
        except Exception as exc:  # a single corrupt page should not lose the document
            logger.warning("page extraction failed %s", kv(page=index, kind=type(exc).__name__))
            continue
        finally:
            page.flush_cache()

        if not words:
            continue
        lines.extend(
            page_lines(
                words,
                page=index,
                page_width=float(page.width),
                page_height=float(page.height),
            )
        )
    return lines, page_count, saw_images


def _column_metrics(lines: Sequence[Line]) -> dict[tuple[int, int], _ColumnMetrics]:
    """Left and right text edges for each (page, column), used to judge line fullness."""
    grouped: dict[tuple[int, int], list[Line]] = {}
    for line in lines:
        grouped.setdefault((line.page, line.column), []).append(line)
    return {
        key: _ColumnMetrics(
            left=min(line.x0 for line in group),
            right=max(line.x1 for line in group),
        )
        for key, group in grouped.items()
    }


def _median_line_height(lines: Sequence[Line]) -> float:
    heights = [line.bottom - line.top for line in lines if line.bottom > line.top]
    return statistics.median(heights) if heights else 12.0


def _starts_new_paragraph(
    previous: Line,
    current: Line,
    *,
    metrics: dict[tuple[int, int], _ColumnMetrics],
    line_height: float,
) -> bool:
    """Decide whether `current` begins a new paragraph rather than continuing `previous`."""
    previous_column = metrics[(previous.page, previous.column)]
    current_column = metrics[(current.page, current.column)]

    previous_ended_full = (
        previous.x1 >= previous_column.left + previous_column.width * _FULL_LINE_SHARE
    )
    current_is_indented = (
        current.x0 > current_column.left + current_column.width * _INDENT_SHARE
    )

    if (previous.page, previous.column) != (current.page, current.column):
        # Across a column or page break the only evidence left is whether the previous line
        # ran to the margin and this one starts at it.
        return not (previous_ended_full and not current_is_indented)

    gap = current.top - previous.bottom
    if gap > line_height * _PARAGRAPH_GAP_RATIO:
        return True
    if current_is_indented:
        return True
    return not previous_ended_full


def _join(previous_text: str, next_text: str) -> tuple[str, str]:
    """Join two lines of one paragraph, repairing a hyphen split across the line break.

    Returns `(separator, text_to_append)`; the previous line's trailing hyphen is removed by
    the caller when the separator comes back empty.
    """
    if (
        previous_text.endswith("-")
        and not previous_text.endswith("--")
        and next_text[:1].islower()
    ):
        return "", next_text
    return " ", next_text


@dataclass
class _Builder:
    """Accumulates the canonical text, its page spans and its blocks in one pass."""

    parts: list[str]
    offset: int
    page_marks: list[tuple[int, int]]
    blocks: list[Block]
    current_page: int | None = None

    def _emit(self, text: str) -> None:
        self.parts.append(text)
        self.offset += len(text)

    def add_group(self, group: Sequence[Line], level: int | None) -> None:
        if not group:
            return
        if self.parts:
            self._emit("\n\n")

        start = self.offset

        for index, line in enumerate(group):
            if line.page != self.current_page:
                self.page_marks.append((line.page, self.offset))
                self.current_page = line.page
            if index == 0:
                self._emit(line.text)
                continue
            separator, text = _join(self.parts[-1], line.text)
            if separator == "":
                self.parts[-1] = self.parts[-1][:-1]
                self.offset -= 1
            else:
                self._emit(separator)
            self._emit(text)

        end = self.offset
        if end <= start:
            return
        # `heading_text` stays None: the chunker derives the label from the block's own slice
        # of the canonical text, so the path printed under a citation is exactly the text the
        # document contains — including any de-hyphenation done above.
        self.blocks.append(
            Block(
                kind=BlockKind.HEADING if level else BlockKind.PARAGRAPH,
                char_start=start,
                char_end=end,
                level=level,
            )
        )


def extract_pdf(content: bytes, *, max_pages: int) -> ExtractedDocument:
    """Extract a PDF into text, page spans and structural blocks.

    Raises:
        PermanentIngestError: encrypted, damaged, over the page limit, or image-only. None of
            these are worth three attempts, and each carries copy a user can act on.
    """
    _require_pdf(content)

    pdf = _open_pdf(content)
    try:
        lines, page_count, saw_images = _collect_lines(pdf, max_pages=max_pages)
    finally:
        pdf.close()

    total_chars = sum(len(line.text.strip()) for line in lines)
    if total_chars < _MIN_MEANINGFUL_CHARS:
        if saw_images:
            raise PermanentIngestError(
                "This PDF appears to be a scan, so it contains images rather than text that "
                "can be searched. Queryll does not read scanned documents.",
                detail="image-only pdf",
            )
        raise PermanentIngestError(
            "This PDF contains no readable text, so there was nothing to index.",
            detail="no extractable text",
        )

    running = find_running_lines(lines, page_count)
    if running:
        logger.info(
            "stripped running headers/footers %s",
            kv(lines=len(running), pages=page_count),
        )
    body = [line for index, line in enumerate(lines) if index not in running]
    if not body:
        raise PermanentIngestError(
            "This PDF contains no readable text, so there was nothing to index.",
            detail="only running headers survived extraction",
        )

    model = build_heading_model(body)
    metrics = _column_metrics(body)
    line_height = _median_line_height(body)

    levels = [model.level_for(line) for line in body]

    groups: list[list[Line]] = [[body[0]]]
    group_levels: list[int | None] = [levels[0]]
    for index in range(1, len(body)):
        previous, current = body[index - 1], body[index]
        previous_level, current_level = levels[index - 1], levels[index]

        if current_level is not None and current_level == previous_level:
            # A heading that wrapped onto a second line is one heading, not two. Splitting it
            # would leave `heading_path` showing only the tail of the title.
            same_column = (previous.page, previous.column) == (current.page, current.column)
            adjacent = current.top - previous.bottom <= line_height * 1.2
            if same_column and adjacent:
                groups[-1].append(current)
                continue

        if (
            current_level is not None
            or previous_level is not None
            or _starts_new_paragraph(
                previous, current, metrics=metrics, line_height=line_height
            )
        ):
            groups.append([current])
            group_levels.append(current_level)
        else:
            groups[-1].append(current)

    builder = _Builder(parts=[], offset=0, page_marks=[], blocks=[])
    for group, level in zip(groups, group_levels):
        builder.add_group(group, level)

    text = "".join(builder.parts)
    pages = _page_spans(builder.page_marks, len(text))

    return ExtractedDocument(
        text=text,
        blocks=tuple(builder.blocks),
        pages=pages,
        page_count=page_count,
        headings_reliable=model.reliable,
    )


def _page_spans(marks: Sequence[tuple[int, int]], total: int) -> tuple[PageSpan, ...]:
    """Turn `(page, first offset)` markers into contiguous, non-overlapping spans.

    Contiguity matters: an offset that lands in the `\\n\\n` between two blocks still has to
    resolve to a page, or a chunk boundary would produce a NULL page number in the middle of
    a paged document.
    """
    if not marks:
        return ()
    spans: list[PageSpan] = []
    for index, (page, start) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else total
        if end > start:
            spans.append(PageSpan(number=page, char_start=start, char_end=end))
    return tuple(spans)
