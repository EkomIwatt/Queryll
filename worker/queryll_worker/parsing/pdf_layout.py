"""Turning a PDF page into an ordered list of text lines.

This is the part of the worker that decides whether citations are worth reading. Three
problems have to be solved before any chunking can happen, and all three are geometry:

1. **Reading order in multi-column layouts.** Sorting text by vertical position alone
   interleaves the columns of a two-column paper into nonsense. Columns are found by looking
   for a vertical whitespace gutter, and a page-spanning line (a title, a full-width figure
   caption) splits the page into bands so a heading above a two-column body does not hide the
   gutter underneath it.

2. **Running headers and footers.** A journal's running title and page number appear on every
   page at the same height. Left in, they land in every chunk, drag every embedding toward the
   same boilerplate, and read as noise inside a citation. They are found by *repetition at a
   stable vertical position*, with digit runs normalized away so "Page 3 of 12" and
   "Page 4 of 12" count as the same line.

3. **Which lines are headings.** Font size and weight relative to the document's body text.
   Deliberately conservative: if the signal looks unreliable the whole document falls back to
   `heading_path = NULL`, because a wrong heading is printed under a citation as if it were
   fact.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: Fraction of the text width a line must cover to be treated as spanning every column.
_FULL_WIDTH_RATIO = 0.72
#: A gutter must be at least this fraction of the page width, and at least this many points.
_MIN_GUTTER_RATIO = 0.025
_MIN_GUTTER_POINTS = 8.0
#: A gutter's centre must fall inside this fraction of the band's horizontal extent.
_GUTTER_CENTRE_RANGE = (0.25, 0.75)
#: Each side of a gutter must hold at least this fraction of the band's words.
_MIN_SIDE_SHARE = 0.15
#: Bands smaller than this are not worth looking for columns in.
_MIN_ROWS_FOR_COLUMNS = 4

#: Vertical bands, as a fraction of page height, in which a running header/footer can live.
_HEADER_BAND = 0.12
_FOOTER_BAND = 0.12
#: Repetition below this many pages is coincidence, not a running header.
_MIN_PAGES_FOR_STRIPPING = 3
#: A line must repeat on at least this share of pages to be stripped.
_REPEAT_SHARE = 0.5
#: Vertical position is bucketed to this many slots per page before comparing.
_POSITION_BUCKETS = 50

_BOLD_MARKERS = ("bold", "black", "heavy", "semibold", "demibold")
_DIGIT_RUN_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Line:
    """One rendered line of text, with the geometry needed to classify it."""

    text: str
    page: int  # 1-based
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool
    page_width: float
    page_height: float
    #: Which column of its page the line came from, left to right. 0 for single-column text.
    column: int = 0

    @property
    def width(self) -> float:
        return self.x1 - self.x0


def _word_size(word: dict[str, Any]) -> float:
    try:
        return float(word.get("size") or 0.0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0.0


def _word_is_bold(word: dict[str, Any]) -> bool:
    fontname = str(word.get("fontname") or "").lower()
    return any(marker in fontname for marker in _BOLD_MARKERS)


def _cluster_rows(words: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group words into rows by vertical position.

    Sorted by `top` then `x0` so the result is independent of the order pdfplumber happened
    to emit characters in — determinism starts here.
    """
    if not words:
        return []
    heights = [float(w["bottom"]) - float(w["top"]) for w in words]
    tolerance = max(1.5, statistics.median(heights) * 0.5)

    ordered = sorted(words, key=lambda w: (round(float(w["top"]), 2), float(w["x0"])))
    rows: list[list[dict[str, Any]]] = [[ordered[0]]]
    row_top = float(ordered[0]["top"])
    for word in ordered[1:]:
        if abs(float(word["top"]) - row_top) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
            row_top = float(word["top"])
    for row in rows:
        row.sort(key=lambda w: float(w["x0"]))
    return rows


def _find_gutter(words: Sequence[dict[str, Any]], page_width: float) -> float | None:
    """Find the x coordinate of a vertical whitespace gutter, or None if there isn't one.

    Occupancy is projected onto the x axis in one-point bins; a gutter is the widest empty
    run that is wide enough, central enough, and has substantial text on both sides.
    """
    if len(words) < 8:
        return None

    left = min(float(w["x0"]) for w in words)
    right = max(float(w["x1"]) for w in words)
    extent = right - left
    if extent <= 0:
        return None

    bins = max(1, int(extent) + 1)
    occupied = bytearray(bins)
    for word in words:
        start = max(0, int(float(word["x0"]) - left))
        end = min(bins, int(float(word["x1"]) - left) + 1)
        for index in range(start, end):
            occupied[index] = 1

    min_width = max(_MIN_GUTTER_POINTS, page_width * _MIN_GUTTER_RATIO)
    best: tuple[float, float] | None = None  # (width, centre_x)

    run_start: int | None = None
    for index in range(bins + 1):
        filled = index >= bins or occupied[index]
        if not filled and run_start is None:
            run_start = index
        elif filled and run_start is not None:
            run_width = index - run_start
            centre = left + run_start + run_width / 2.0
            position = (centre - left) / extent
            if (
                run_width >= min_width
                and _GUTTER_CENTRE_RANGE[0] <= position <= _GUTTER_CENTRE_RANGE[1]
                and (best is None or run_width > best[0])
            ):
                best = (float(run_width), centre)
            run_start = None

    if best is None:
        return None

    centre_x = best[1]
    left_count = sum(1 for w in words if float(w["x1"]) <= centre_x)
    right_count = len(words) - left_count
    minimum = len(words) * _MIN_SIDE_SHARE
    if left_count < minimum or right_count < minimum:
        return None
    return centre_x


def _rows_to_lines(
    rows: Iterable[list[dict[str, Any]]],
    *,
    page: int,
    page_width: float,
    page_height: float,
    column: int = 0,
) -> list[Line]:
    lines: list[Line] = []
    for row in rows:
        if not row:
            continue
        text = " ".join(str(word["text"]) for word in row).strip()
        if not text:
            continue
        char_counts = [max(1, len(str(word["text"]))) for word in row]
        sizes: list[float] = []
        for word, count in zip(row, char_counts):
            sizes.extend([_word_size(word)] * count)
        bold_chars = sum(
            count for word, count in zip(row, char_counts) if _word_is_bold(word)
        )
        lines.append(
            Line(
                text=text,
                page=page,
                x0=min(float(w["x0"]) for w in row),
                x1=max(float(w["x1"]) for w in row),
                top=min(float(w["top"]) for w in row),
                bottom=max(float(w["bottom"]) for w in row),
                size=round(statistics.median(sizes), 2) if sizes else 0.0,
                bold=bold_chars * 2 > sum(char_counts),
                page_width=page_width,
                page_height=page_height,
                column=column,
            )
        )
    return lines


def page_lines(
    words: Sequence[dict[str, Any]],
    *,
    page: int,
    page_width: float,
    page_height: float,
) -> list[Line]:
    """Order one page's words into lines, respecting columns.

    Bands are separated by page-spanning rows, so a full-width title above a two-column body
    does not mask the gutter beneath it.
    """
    rows = _cluster_rows(words)
    if not rows:
        return []

    left = min(float(w["x0"]) for w in words)
    right = max(float(w["x1"]) for w in words)
    text_width = max(1.0, right - left)

    bands: list[list[list[dict[str, Any]]]] = []
    for row in rows:
        row_width = max(float(w["x1"]) for w in row) - min(float(w["x0"]) for w in row)
        spanning = row_width > text_width * _FULL_WIDTH_RATIO
        if spanning or not bands:
            bands.append([row])
            if spanning:
                bands.append([])  # start a fresh band after a spanning row
        else:
            bands[-1].append(row)
    bands = [band for band in bands if band]

    lines: list[Line] = []
    for band in bands:
        band_words = [word for row in band for word in row]
        gutter = (
            _find_gutter(band_words, page_width)
            if len(band) >= _MIN_ROWS_FOR_COLUMNS
            else None
        )
        if gutter is None:
            lines.extend(
                _rows_to_lines(
                    band, page=page, page_width=page_width, page_height=page_height
                )
            )
            continue

        left_words: list[dict[str, Any]] = []
        right_words: list[dict[str, Any]] = []
        for word in band_words:
            start, end = float(word["x0"]), float(word["x1"])
            # A word straddling the gutter is assigned by its midpoint rather than dropped.
            side = left_words if (start + end) / 2.0 <= gutter else right_words
            side.append(word)
        for index, column_words in enumerate((left_words, right_words)):
            lines.extend(
                _rows_to_lines(
                    _cluster_rows(column_words),
                    page=page,
                    page_width=page_width,
                    page_height=page_height,
                    column=index,
                )
            )
    return lines


def _normalize_for_repetition(text: str) -> str:
    """Collapse a line to the form in which running headers are recognisable.

    Digit runs become `#` so incrementing page numbers and dates compare equal.
    """
    return _DIGIT_RUN_RE.sub("#", _WS_RE.sub(" ", text).strip().lower())


def find_running_lines(lines: Sequence[Line], page_count: int) -> set[int]:
    """Identify running headers and footers, returned as indices into `lines`.

    A line is running boilerplate when its normalized text appears in the same vertical slot
    of the header or footer band on at least half the pages. Documents shorter than three
    pages are left alone: on a two-page document "repeats on every page" is not evidence.
    """
    if page_count < _MIN_PAGES_FOR_STRIPPING:
        return set()

    buckets: dict[tuple[str, int], set[int]] = {}
    candidates: dict[tuple[str, int], list[int]] = {}

    for index, line in enumerate(lines):
        if line.page_height <= 0:
            continue
        relative_top = line.top / line.page_height
        relative_bottom = line.bottom / line.page_height
        in_header = relative_top <= _HEADER_BAND
        in_footer = relative_bottom >= 1.0 - _FOOTER_BAND
        if not (in_header or in_footer):
            continue
        slot = int(relative_top * _POSITION_BUCKETS)
        key = (_normalize_for_repetition(line.text), slot)
        if not key[0]:
            continue
        buckets.setdefault(key, set()).add(line.page)
        candidates.setdefault(key, []).append(index)

    threshold = max(_MIN_PAGES_FOR_STRIPPING, int(page_count * _REPEAT_SHARE))
    running: set[int] = set()
    for key, pages in buckets.items():
        if len(pages) >= threshold:
            running.update(candidates[key])
    return running


def body_font_size(lines: Sequence[Line]) -> float:
    """The document's body text size: the character-weighted median line size."""
    sizes: list[float] = []
    for line in lines:
        sizes.extend([line.size] * max(1, len(line.text)))
    return statistics.median(sizes) if sizes else 0.0


@dataclass(frozen=True)
class HeadingModel:
    """How to tell a heading from body text in one particular document."""

    body_size: float
    #: Distinct heading sizes, largest first; a line's index here becomes its heading level.
    levels: tuple[float, ...]
    reliable: bool

    def level_for(self, line: Line) -> int | None:
        if not self.reliable or not _looks_like_heading(line, self.body_size):
            return None
        for index, size in enumerate(self.levels):
            if abs(line.size - size) < 0.26:
                return min(index + 1, 6)
        return min(len(self.levels) + 1, 6)


def _looks_like_heading(line: Line, body_size: float) -> bool:
    text = line.text.strip()
    if not text or len(text) > 120:
        return False
    if text.endswith((".", ";", ",")):
        return False
    bigger = line.size >= body_size * 1.15
    emphatic = line.bold and line.size >= body_size * 1.0
    return bigger or emphatic


def build_heading_model(lines: Sequence[Line]) -> HeadingModel:
    """Decide whether this document's headings can be detected at all, and at what sizes.

    Detection is switched off entirely — every chunk then gets `heading_path = NULL` — when
    the signal is weak: no size or weight variation to speak of, or so many lines qualifying
    that "heading" has stopped meaning anything.
    """
    body = body_font_size(lines)
    if body <= 0 or not lines:
        return HeadingModel(body_size=body, levels=(), reliable=False)

    candidates = [line for line in lines if _looks_like_heading(line, body)]
    if not candidates:
        return HeadingModel(body_size=body, levels=(), reliable=False)

    share = len(candidates) / len(lines)
    if share > 0.25:
        # Either the whole document is set in a display face or the heuristic has misfired.
        return HeadingModel(body_size=body, levels=(), reliable=False)

    sizes = sorted({round(line.size, 1) for line in candidates}, reverse=True)
    return HeadingModel(body_size=body, levels=tuple(sizes[:6]), reliable=True)
