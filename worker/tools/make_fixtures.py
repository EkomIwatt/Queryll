"""Generate the PDF fixtures the parser tests run against.

A clean single-column PDF proves almost nothing, so the main fixture here is deliberately
awkward: two columns, a running header that starts on page two the way a real paper's does,
page-number footers, headings set by size and weight rather than by any tag, a paragraph that
runs across a page break, a word hyphenated across a line break, a footnote, and a table.

The PDFs are committed so the test suite has no build step. Re-run this only when a fixture
needs to change:

    python tools/make_fixtures.py

Everything here is deterministic — the same source produces byte-identical geometry — so a
regenerated fixture differs only where the content was actually edited.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

PAGE_WIDTH, PAGE_HEIGHT = LETTER
MARGIN = 54.0
GUTTER = 24.0
COLUMN_WIDTH = (PAGE_WIDTH - 2 * MARGIN - GUTTER) / 2
BODY_TOP = PAGE_HEIGHT - MARGIN - 18.0
BODY_BOTTOM = MARGIN + 24.0

BODY_FONT = "Times-Roman"
BOLD_FONT = "Times-Bold"
BODY_SIZE = 9.5
LEADING = 12.0

RUNNING_HEADER = "Retrieval Quality in Grounded Question Answering"


@dataclass
class Item:
    """A thing to lay out in the column flow."""

    kind: str  # "heading" | "body" | "footnote" | "table" | "prewrapped"
    text: str
    level: int = 0


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if pdfmetrics.stringWidth(candidate, font, size) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _style(item: Item) -> tuple[str, float, float]:
    """Font, size and leading for an item — the only signal heading detection gets."""
    if item.kind == "heading":
        return (BOLD_FONT, 13.0 if item.level == 1 else 11.0, 17.0)
    if item.kind == "footnote":
        return (BODY_FONT, 7.5, 9.5)
    if item.kind == "table":
        return ("Courier", 8.0, 10.0)
    return (BODY_FONT, BODY_SIZE, LEADING)


def _draw_furniture(pdf: canvas.Canvas, page: int, total: int) -> None:
    """Running header (from page two, as a real paper does) and a page-number footer."""
    if page > 1:
        pdf.setFont(BODY_FONT, 8.0)
        pdf.drawCentredString(PAGE_WIDTH / 2, PAGE_HEIGHT - MARGIN + 8.0, RUNNING_HEADER)
    pdf.setFont(BODY_FONT, 8.0)
    pdf.drawCentredString(PAGE_WIDTH / 2, MARGIN - 12.0, f"Page {page} of {total}")


def _render(items: list[Item], path: Path) -> None:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=LETTER)

    page = 1
    column = 0
    y = BODY_TOP

    # The title block spans both columns, which is what forces the layout code to find the
    # gutter *below* a full-width line rather than giving up on the page.
    pdf.setFont(BOLD_FONT, 16.0)
    pdf.drawCentredString(PAGE_WIDTH / 2, y, RUNNING_HEADER)
    y -= 22.0
    pdf.setFont(BODY_FONT, 10.0)
    pdf.drawCentredString(PAGE_WIDTH / 2, y, "A. Reviewer, B. Editor and C. Maintainer")
    y -= 28.0
    title_bottom = y

    def column_x() -> float:
        return MARGIN + column * (COLUMN_WIDTH + GUTTER)

    def new_column() -> None:
        nonlocal column, y, page
        if column == 0:
            column = 1
            y = title_bottom if page == 1 else BODY_TOP
        else:
            _draw_furniture(pdf, page, 4)
            pdf.showPage()
            page += 1
            column = 0
            y = BODY_TOP

    for item in items:
        font, size, leading = _style(item)
        lines = (
            item.text.split("\n")
            if item.kind in {"table", "prewrapped"}
            else _wrap(item.text, font, size, COLUMN_WIDTH)
        )
        if item.kind == "heading":
            y -= 6.0
        for line in lines:
            width = pdfmetrics.stringWidth(line, font, size)
            assert width <= COLUMN_WIDTH, (
                f"{item.kind} line overflows the column by "
                f"{width - COLUMN_WIDTH:.1f}pt: {line!r}"
            )
            if y < BODY_BOTTOM:
                new_column()
            pdf.setFont(font, size)
            pdf.drawString(column_x(), y, line)
            y -= leading
        y -= 4.0

    _draw_furniture(pdf, page, 4)
    pdf.showPage()
    pdf.save()
    path.write_bytes(buffer.getvalue())
    print(f"wrote {path.name} ({path.stat().st_size} bytes)")


LOREM = (
    "Grounded question answering systems are judged less by the fluency of their prose than "
    "by whether the passage behind each claim genuinely supports it. A system that answers "
    "well from the wrong passage is harder to trust than one that declines to answer at all, "
    "because the failure is invisible to the reader."
)

ITEMS: list[Item] = [
    Item("heading", "1. Introduction", level=1),
    Item("body", LOREM),
    Item(
        "body",
        "We evaluate three families of retrieval configuration against a corpus of technical "
        "documentation, and report the effect of each on citation accuracy. Our interest is "
        "practical rather than theoretical: every configuration we test is one a small team "
        "could deploy without a dedicated vector database.",
    ),
    Item("heading", "2. Background", level=1),
    Item(
        "body",
        "Dense retrieval over document fragments has become the default architecture for "
        "question answering over private corpora. The literature has converged on a broad "
        "shape, but the parameters that matter most in practice are rarely reported.",
    ),
    Item("heading", "2.1 Chunking", level=2),
    Item(
        "body",
        "Chunks of roughly 512 tokens with 64 tokens of overlap outperformed both 256-token "
        "and 1024-token configurations on answer coverage. Smaller chunks fragmented "
        "multi-sentence explanations; larger chunks diluted the query signal with unrelated "
        "material from adjacent sections.",
    ),
    # Hand-broken so a word is hyphenated across a line break, which the extractor has to
    # rejoin. Every line must fit inside COLUMN_WIDTH — `_render` asserts it, because a line
    # that overflows its column crosses the gutter and stops being a two-column page at all.
    Item(
        "prewrapped",
        "Splitting on document structure rather than on a fixed\n"
        "character count produced chunks with better reproduci-\n"
        "bility across re-indexing runs, which matters because\n"
        "the chunk identifiers are regenerated on every run.",
    ),
    Item("heading", "2.2 Embeddings", level=2),
    Item(
        "body",
        "All embeddings were produced at 1024 dimensions and stored without further "
        "normalization. The document and query sides of the pipeline must use the same model "
        "and the same dimension; where they diverged in a pilot run, retrieval degraded "
        "silently rather than failing, and no automated test detected the regression.",
    ),
    Item("heading", "3. Methods", level=1),
    Item("heading", "3.1 Corpus", level=2),
    Item(
        "body",
        "The sampling frame was drawn from 4,318 publicly available support articles "
        "collected between March and August 2024. Articles shorter than two hundred words "
        "were excluded, as were duplicates identified by exact text match.",
    ),
    Item("heading", "3.2 Sampling", level=2),
    Item(
        "body",
        "From the sampling frame we drew a stratified random sample of six hundred articles, "
        "balanced across the eleven product areas represented in the corpus. Two annotators "
        "independently marked the passage answering each question, and disagreements were "
        "resolved by discussion.",
    ),
    Item("heading", "4. Results", level=1),
    Item(
        "body",
        "Stripping running headers and footers before embedding improved mean reciprocal "
        "rank from 0.61 to 0.74. The effect was largest on documents with long running "
        "titles, where the repeated text accounted for a substantial share of every short "
        "chunk.",
    ),
    Item(
        "table",
        "Configuration        MRR    Hit@3\n"
        "baseline             0.61   0.72\n"
        "headers stripped     0.74   0.86\n"
        "structure-aware      0.79   0.90",
    ),
    Item(
        "body",
        "Structure-aware splitting added a further four points of mean reciprocal rank over "
        "header stripping alone. We attribute the improvement to the reduced incidence of "
        "chunks that begin or end mid-sentence, which annotators consistently rated as "
        "harder to read when shown as a citation.",
    ),
    Item("heading", "5. Discussion", level=1),
    Item(
        "body",
        "The configurations that helped most were also the cheapest to implement. None of "
        "them required a change of model, and none of them required more than a few hundred "
        "lines of parsing code. We take this as evidence that retrieval quality in small "
        "systems is more often lost in preprocessing than in the choice of embedding.",
    ),
    Item(
        "body",
        "A limitation of this work is that all documents were born-digital. Scanned "
        "documents, which require optical character recognition before any of this applies, "
        "were excluded from the corpus entirely.",
    ),
    Item(
        "footnote",
        "1. Annotation guidelines and the full question set are available on request.",
    ),
]


def _filler() -> list[Item]:
    """Deterministic body text, so the fixture is four pages rather than one.

    A one-page PDF cannot exercise a running header that starts on page two, page-number
    footers, or a paragraph that carries across a page break — which are three of the four
    things this fixture exists for. The prose is generated rather than written because its
    content is irrelevant; only its bulk and its shape matter.
    """
    subjects = (
        "the retrieval layer", "the annotation protocol", "the evaluation harness",
        "the ranking model", "the chunking pass", "the citation viewer",
        "the ingestion worker", "the embedding cache",
    )
    verbs = (
        "was measured against", "was compared with", "was tuned alongside",
        "was validated using", "was contrasted with", "was calibrated against",
    )
    objects = (
        "a held-out set of two hundred questions",
        "the annotations produced in the second round",
        "a baseline that retrieved whole documents",
        "the configuration reported in the previous section",
        "an ablation with overlap removed entirely",
        "a lexical retriever using BM25 scoring",
    )
    tails = (
        "The difference was small but consistent across product areas.",
        "We report the mean over five runs; the variance was negligible.",
        "The effect did not survive correction for document length.",
        "Annotators preferred the shorter passages in every product area.",
        "No configuration recovered an answer that chunking had split in half.",
    )

    items: list[Item] = [Item("heading", "6. Extended results", level=1)]
    index = 0
    for section in range(1, 5):
        items.append(Item("heading", f"6.{section} Additional comparisons", level=2))
        for _ in range(6):
            sentences = []
            for _ in range(4):
                sentences.append(
                    f"{subjects[index % len(subjects)].capitalize()} "
                    f"{verbs[index % len(verbs)]} {objects[index % len(objects)]}. "
                    f"{tails[index % len(tails)]}"
                )
                index += 1
            items.append(Item("body", " ".join(sentences)))
    return items


ITEMS.extend(_filler())

SIMPLE_MARKDOWN = """---
title: Ingestion notes
---

# Ingestion notes

A short document used to exercise Markdown structure handling.

## Chunking

Chunks are built from spans of the extracted text, never from rebuilt strings. This is what
makes the offset invariant hold across overlap.

- Headings start a new chunk.
- Sentences are the smallest unit the packer moves.
- Pre-formatted blocks are never split internally.

## Embeddings

Every vector is checked for dimension and for unit norm before it is stored.

```python
def check(vector):
    assert len(vector) == 1024
```

Setext heading
--------------

The final section exists only to prove that setext headings are recognised too.
"""


def _write_encrypted(path: Path) -> None:
    """A password-protected PDF: a permanent failure with copy the user can act on."""
    from pypdf import PdfReader, PdfWriter

    source = FIXTURES / "paper_two_column.pdf"
    writer = PdfWriter()
    for page in PdfReader(str(source)).pages:
        writer.add_page(page)
    writer.encrypt("queryll-fixture")
    with path.open("wb") as handle:
        writer.write(handle)
    print(f"wrote {path.name} ({path.stat().st_size} bytes)")


def _write_scanned(path: Path) -> None:
    """An image-only PDF: what a scan actually looks like to a parser that does not do OCR.

    A raster image, not vector rectangles. The distinction matters: the extractor tells "this
    is a scan, and Queryll does not read scans" apart from "this file has no text in it" by
    asking whether the page carried any images, and only a real embedded raster exercises that
    path the way a scanner's output would.
    """
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    page = Image.new("L", (850, 1100), color=235)
    draw = ImageDraw.Draw(page)
    for index, offset in enumerate(range(80, 900, 26)):
        # Grey bars standing in for lines of scanned text, ragged like a real scan.
        draw.rectangle(
            [90, offset, 90 + 620 - (index % 5) * 55, offset + 11], fill=90
        )
    raster = io.BytesIO()
    page.save(raster, format="PNG")
    raster.seek(0)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=LETTER)
    for _ in range(2):
        pdf.drawImage(
            ImageReader(raster), 0, 0, width=PAGE_WIDTH, height=PAGE_HEIGHT
        )
        pdf.showPage()
    pdf.save()
    path.write_bytes(buffer.getvalue())
    print(f"wrote {path.name} ({path.stat().st_size} bytes)")


def _write_single_column(path: Path) -> None:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=LETTER)
    y = PAGE_HEIGHT - MARGIN
    pdf.setFont(BOLD_FONT, 14.0)
    pdf.drawString(MARGIN, y, "Release notes")
    y -= 22.0
    for paragraph in (
        "Version 2.1 adds structure-aware chunking and header stripping.",
        "Version 2.0 moved ingestion into a separate worker process so that a long "
        "embedding job could no longer block an HTTP request.",
    ):
        pdf.setFont(BODY_FONT, 11.0)
        for line in _wrap(paragraph, BODY_FONT, 11.0, PAGE_WIDTH - 2 * MARGIN):
            pdf.drawString(MARGIN, y, line)
            y -= 14.0
        y -= 8.0
    pdf.showPage()
    pdf.save()
    path.write_bytes(buffer.getvalue())
    print(f"wrote {path.name} ({path.stat().st_size} bytes)")


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    _render(ITEMS, FIXTURES / "paper_two_column.pdf")
    _write_single_column(FIXTURES / "release_notes.pdf")
    _write_scanned(FIXTURES / "scanned_no_text.pdf")
    _write_encrypted(FIXTURES / "encrypted.pdf")
    (FIXTURES / "ingestion_notes.md").write_text(SIMPLE_MARKDOWN, encoding="utf-8")
    print("wrote ingestion_notes.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
