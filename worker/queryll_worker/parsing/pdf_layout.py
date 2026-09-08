"""Turning a PDF page into an ordered list of text lines.

This is the part of the worker that decides whether citations are worth reading, and all
three of its problems are geometry.

1. **Running headers and footers.** A journal's running title and page number appear on every
   page at the same height. Left in, they land in every chunk, drag every embedding toward the
   same boilerplate, and read as noise inside a citation. They are found by *repetition at a
   stable vertical position*, with digit runs normalized away so "Page 3 of 12" and
   "Page 4 of 12" count as the same line.

   This happens **before** columns are worked out, and the order matters: a centred running
   title crosses the gutter, so leaving it in changes what the page looks like to the column
   detector as well as what ends up in a chunk.

2. **Reading order in multi-column layouts.** Sorting text by vertical position alone
   interleaves the columns of a two-column paper into alternating half-sentences. Columns are
   found by looking for a vertical gutter, and a genuinely full-width line — a title, a wide
   caption — closes the band above it so it is not cut in half.

3. **Which lines are headings.** Font size and weight relative to the document's body text.
   Deliberately conservative: if the signal looks unreliable the whole document falls back to
   `heading_path = NULL`, because a wrong heading is printed under a citation as if it were
   fact.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: A gutter must be at least this fraction of the page width, and at least this many points.
_MIN_GUTTER_RATIO = 0.025
_MIN_GUTTER_POINTS = 8.0
#: A gutter's centre must fall inside this fraction of the page's text extent.
_GUTTER_CENTRE_RANGE = (0.25, 0.75)
#: How close to a margin a row must start or end to count as anchored to it.
_ANCHOR_TOLERANCE = 0.03
#: A gutter may still be crossed by this share of rows — a title, a wide caption, a rule.
_MAX_GUTTER_COVERAGE = 0.2
#: Each column must hold at least this many rows of its own.
_MIN_ROWS_PER_COLUMN = 3
#: Pages with fewer rows than this are not worth looking for columns in.
_MIN_ROWS_FOR_COLUMNS = 6

#: Vertical bands, as a fraction of page height, in which a running header/footer can live.
_HEADER_BAND = 0.12
_FOOTER_BAND = 0.12
#: Repetition below this many pages is coincidence, not a running header.
_MIN_PAGES_FOR_STRIPPING = 3
#: A line must repeat on at least this share of pages to be stripped.
_REPEAT_SHARE = 0.5
#: Vertical position is bucketed to this many slots per page before comparing.
_POSITION_BUCKETS = 50

#: The column index given to a full-width line, so it never pollutes a real column's measure.
FULL_WIDTH_COLUMN = -1

_BOLD_MARKERS = ("bold", "black", "heavy", "semibold", "demibold")
_DIGIT_RUN_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")

Word = dict[str, Any]


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
    #: Which column of its page the line came from, left to right. `FULL_WIDTH_COLUMN` for a
    #: line that spans them all.
    column: int = 0

    @property
    def width(self) -> float:
        return self.x1 - self.x0


@dataclass
class Row:
    """Words sharing a baseline, before anything is known about columns."""

    words: list[Word]
    page: int
    page_width: float
    page_height: float
    x0: float = field(init=False)
    x1: float = field(init=False)
    top: float = field(init=False)
    bottom: float = field(init=False)

    def __post_init__(self) -> None:
        self.words.sort(key=lambda w: float(w["x0"]))
        self.x0 = min(float(w["x0"]) for w in self.words)
        self.x1 = max(float(w["x1"]) for w in self.words)
        self.top = min(float(w["top"]) for w in self.words)
        self.bottom = max(float(w["bottom"]) for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(str(word["text"]) for word in self.words).strip()


def _word_size(word: Word) -> float:
    try:
        return float(word.get("size") or 0.0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0.0


def _word_is_bold(word: Word) -> bool:
    fontname = str(word.get("fontname") or "").lower()
    return any(marker in fontname for marker in _BOLD_MARKERS)


def page_rows(
    words: Sequence[Word], *, page: int, page_width: float, page_height: float
) -> list[Row]:
    """Group one page's words into rows by vertical position.

    Sorted by `top` then `x0`, so the result does not depend on the order pdfplumber happened
    to emit characters in — determinism starts here.
    """
    if not words:
        return []
    heights = [float(w["bottom"]) - float(w["top"]) for w in words]
    tolerance = max(1.5, statistics.median(heights) * 0.5)

    ordered = sorted(words, key=lambda w: (round(float(w["top"]), 2), float(w["x0"])))
    groups: list[list[Word]] = [[ordered[0]]]
    row_top = float(ordered[0]["top"])
    for word in ordered[1:]:
        if abs(float(word["top"]) - row_top) <= tolerance:
            groups[-1].append(word)
        else:
            groups.append([word])
            row_top = float(word["top"])

    return [
        Row(words=group, page=page, page_width=page_width, page_height=page_height)
        for group in groups
    ]


def _normalize_for_repetition(text: str) -> str:
    """Collapse a line to the form in which running headers are recognisable.

    Digit runs become `#` so incrementing page numbers and dates compare equal.
    """
    return _DIGIT_RUN_RE.sub("#", _WS_RE.sub(" ", text).strip().lower())


def find_running_rows(rows: Sequence[Row], page_count: int) -> set[int]:
    """Identify running headers and footers, returned as indices into `rows`.

    A row is boilerplate when its normalized text appears in the same vertical slot of the
    header or footer band on at least half the pages. Documents shorter than three pages are
    left alone: on a two-page document "it repeats on every page" is not evidence.
    """
    if page_count < _MIN_PAGES_FOR_STRIPPING:
        return set()

    pages_seen: dict[tuple[str, int], set[int]] = {}
    positions: dict[tuple[str, int], list[int]] = {}

    for index, row in enumerate(rows):
        if row.page_height <= 0:
            continue
        in_header = row.top / row.page_height <= _HEADER_BAND
        in_footer = row.bottom / row.page_height >= 1.0 - _FOOTER_BAND
        if not (in_header or in_footer):
            continue
        key = (
            _normalize_for_repetition(row.text),
            int(row.top / row.page_height * _POSITION_BUCKETS),
        )
        if not key[0]:
            continue
        pages_seen.setdefault(key, set()).add(row.page)
        positions.setdefault(key, []).append(index)

    threshold = max(_MIN_PAGES_FOR_STRIPPING, int(page_count * _REPEAT_SHARE))
    running: set[int] = set()
    for key, pages in pages_seen.items():
        if len(pages) >= threshold:
            running.update(positions[key])
    return running


def _row_bins(row: Row, left: float, bins: int) -> set[int]:
    """The one-point x bins this row actually puts ink in."""
    covered: set[int] = set()
    for word in row.words:
        first = max(0, int(float(word["x0"]) - left))
        last = min(bins - 1, int(float(word["x1"]) - left))
        covered.update(range(first, last + 1))
    return covered


def find_gutter(rows: Sequence[Row], page_width: float) -> float | None:
    """Find the x coordinate of the vertical gutter between columns, or None.

    Coverage is counted **per row, not per word**, and the gutter is the widest *low*-coverage
    valley rather than the widest empty run. That distinction is the trick: a full-width title
    or a wide caption puts ink straight through the gutter, and a strict "must be empty" test
    would conclude the page has one column and interleave the two.
    """
    if len(rows) < _MIN_ROWS_FOR_COLUMNS:
        return None

    left = min(row.x0 for row in rows)
    right = max(row.x1 for row in rows)
    extent = right - left
    if extent <= 0:
        return None

    bins = max(1, int(extent) + 1)
    coverage = [0] * bins
    for row in rows:
        for index in _row_bins(row, left, bins):
            coverage[index] += 1

    min_width = max(_MIN_GUTTER_POINTS, page_width * _MIN_GUTTER_RATIO)
    ceiling = len(rows) * _MAX_GUTTER_COVERAGE
    best: tuple[int, float] | None = None  # (width, centre_x)

    run_start: int | None = None
    for index in range(bins + 1):
        quiet = index < bins and coverage[index] <= ceiling
        if quiet and run_start is None:
            run_start = index
        elif not quiet and run_start is not None:
            width = index - run_start
            centre = left + run_start + width / 2.0
            position = (centre - left) / extent
            if (
                width >= min_width
                and _GUTTER_CENTRE_RANGE[0] <= position <= _GUTTER_CENTRE_RANGE[1]
                and (best is None or width > best[0])
            ):
                best = (width, centre)
            run_start = None

    if best is None:
        return None

    centre_x = best[1]
    # Both sides must hold real text, or this is a wide indent rather than a gutter.
    #
    # Counted per *word*, not per row's extent. A Row groups words by vertical position, so on
    # a dense two-column page almost every row holds words from BOTH columns: its x0 sits in
    # the left column and its x1 in the right. Asking whether a row lies *wholly* on one side
    # therefore answers "neither" for exactly the pages this function exists to detect — the
    # better-formed the layout, the more certainly it was rejected. On the four-page fixture
    # that left pages 2 and 3 with 2/1 and 1/1 qualifying rows against a minimum of 3, so the
    # gutter was discarded, the page was read as one column, and the two column streams were
    # interleaved line by line into the chunk text. Page 1 survived only because its right
    # column is partly empty.
    #
    # A page whose right column is genuinely empty (the last page of a paper) still fails this
    # test, which is correct: it really is single-column, and reading it as one is right.
    on_left = sum(
        1 for row in rows if any(float(word["x1"]) <= centre_x for word in row.words)
    )
    on_right = sum(
        1 for row in rows if any(float(word["x0"]) >= centre_x for word in row.words)
    )
    if min(on_left, on_right) < _MIN_ROWS_PER_COLUMN:
        return None
    return centre_x


def _line_from(words: Sequence[Word], row: Row, column: int) -> Line | None:
    ordered = sorted(words, key=lambda w: float(w["x0"]))
    text = " ".join(str(word["text"]) for word in ordered).strip()
    if not text:
        return None
    char_counts = [max(1, len(str(word["text"]))) for word in ordered]
    sizes: list[float] = []
    for word, count in zip(ordered, char_counts, strict=False):
        sizes.extend([_word_size(word)] * count)
    bold_chars = sum(
        count for word, count in zip(ordered, char_counts, strict=False) if _word_is_bold(word)
    )
    return Line(
        text=text,
        page=row.page,
        x0=min(float(w["x0"]) for w in ordered),
        x1=max(float(w["x1"]) for w in ordered),
        top=min(float(w["top"]) for w in ordered),
        bottom=max(float(w["bottom"]) for w in ordered),
        size=round(statistics.median(sizes), 2) if sizes else 0.0,
        bold=bold_chars * 2 > sum(char_counts),
        page_width=row.page_width,
        page_height=row.page_height,
        column=column,
    )


def _split_row(
    row: Row, gutter: float, text_left: float, text_right: float
) -> tuple[list[Word], list[Word]] | None:
    """Split one row at the gutter, or None if the row is genuinely full width.

    Two kinds of row cross the gutter and must be told apart, and the tell is what the row is
    *anchored* to.

    A body line that merely overflows its column by a few points starts hard against the left
    column's margin and stops well short of the right column's. It is an ordinary line, and
    treating it as full width would split the page into two bands and interleave every column
    below it — so its crossing word is assigned to whichever side its midpoint falls on.

    Anything else that crosses is full width: a banner title touches both margins, a centred
    title or author line touches neither. Both close the band above them and are emitted
    whole, because cutting a title in half is worse than any ordering mistake.

    A row with text in only one column is not full width either: it is an ordinary line whose
    opposite number happens to sit at a slightly different height.
    """
    crosses = any(float(w["x0"]) < gutter < float(w["x1"]) for w in row.words)
    if crosses:
        margin = max(1.0, text_right - text_left) * _ANCHOR_TOLERANCE
        starts_at_left = row.x0 <= text_left + margin
        ends_at_right = row.x1 >= text_right - margin
        overflowing_column = starts_at_left and not ends_at_right
        if not overflowing_column:
            return None

    left: list[Word] = []
    right: list[Word] = []
    for word in row.words:
        midpoint = (float(word["x0"]) + float(word["x1"])) / 2.0
        (left if midpoint <= gutter else right).append(word)
    return left, right


def order_rows(rows: Sequence[Row]) -> list[Line]:
    """Order one page's rows into lines, reading each column top to bottom in turn.

    Emitting a band as "all of the left column, then all of the right column" is what turns a
    two-column paper into readable prose instead of alternating half-sentences.
    """
    if not rows:
        return []

    page_width = rows[0].page_width
    gutter = find_gutter(rows, page_width)
    if gutter is None:
        return [
            line
            for line in (_line_from(row.words, row, 0) for row in rows)
            if line is not None
        ]

    text_left = min(row.x0 for row in rows)
    text_right = max(row.x1 for row in rows)
    lines: list[Line] = []
    band: list[tuple[Row, list[Word], list[Word]]] = []

    def flush() -> None:
        for column in (0, 1):
            for row, left, right in band:
                words = left if column == 0 else right
                if not words:
                    continue
                line = _line_from(words, row, column)
                if line is not None:
                    lines.append(line)
        band.clear()

    for row in rows:
        split = _split_row(row, gutter, text_left, text_right)
        if split is None:
            flush()
            line = _line_from(row.words, row, FULL_WIDTH_COLUMN)
            if line is not None:
                lines.append(line)
        else:
            band.append((row, split[0], split[1]))
    flush()
    return lines


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
    emphatic = line.bold and line.size >= body_size
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

    if len(candidates) / len(lines) > 0.25:
        # Either the whole document is set in a display face or the heuristic has misfired.
        return HeadingModel(body_size=body, levels=(), reliable=False)

    sizes = sorted({round(line.size, 1) for line in candidates}, reverse=True)
    return HeadingModel(body_size=body, levels=tuple(sizes[:6]), reliable=True)


def iter_words(lines: Iterable[Line]) -> list[str]:  # pragma: no cover - debugging aid
    return [line.text for line in lines]
