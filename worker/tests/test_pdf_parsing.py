"""PDF extraction against a genuinely awkward fixture.

`paper_two_column.pdf` is two-column, has a running header that starts on page two the way a
real paper's does, page-number footers, headings set by size and weight rather than by any
tag, a paragraph that runs across a page break, a hyphenated word split across a line break, a
footnote and a table. A clean single-column PDF would pass every test here while proving
nothing, which is why it is only the control.
"""

from __future__ import annotations

import pytest

from queryll_worker.errors import PermanentIngestError
from queryll_worker.parsing import extract, extract_pdf
from queryll_worker.parsing.base import BlockKind
from queryll_worker.parsing.pdf import _collect_rows, _open_pdf
from queryll_worker.parsing.pdf_layout import find_gutter, find_running_rows
from tests.conftest import fixture_bytes

RUNNING_HEADER = "Retrieval Quality in Grounded Question Answering"


@pytest.fixture(scope="module")
def paper():  # type: ignore[no-untyped-def]
    return extract_pdf(fixture_bytes("paper_two_column.pdf"), max_pages=500)


# --- structure ---------------------------------------------------------------------------


def test_page_count_is_reported(paper) -> None:  # type: ignore[no-untyped-def]
    assert paper.page_count == 4


def test_page_spans_are_contiguous_and_cover_the_text(paper) -> None:  # type: ignore[no-untyped-def]
    """A gap would mean an offset in it resolved to no page at all."""
    assert paper.pages
    assert paper.pages[0].char_start == 0
    for earlier, later in zip(paper.pages, paper.pages[1:], strict=False):
        assert earlier.char_end == later.char_start
        assert earlier.number <= later.number
    assert paper.pages[-1].char_end == len(paper.text)


def test_blocks_slice_back_to_the_canonical_text(paper) -> None:  # type: ignore[no-untyped-def]
    for block in paper.blocks:
        assert paper.text[block.char_start : block.char_end] == paper.slice(block)


def test_blocks_are_ordered_and_do_not_overlap(paper) -> None:  # type: ignore[no-untyped-def]
    for earlier, later in zip(paper.blocks, paper.blocks[1:], strict=False):
        assert earlier.char_end <= later.char_start


# --- the three problems this module exists to solve ---------------------------------------


def test_running_headers_and_footers_are_stripped(paper) -> None:  # type: ignore[no-untyped-def]
    """Left in, they land in every chunk and read as noise inside a citation."""
    body = paper.text
    # The title appears once, as the title. It must not appear once per page.
    assert body.count(RUNNING_HEADER) <= 1
    for page in range(1, 5):
        assert f"Page {page} of 4" not in body


def test_columns_are_read_in_the_right_order(paper) -> None:  # type: ignore[no-untyped-def]
    """Sorting by vertical position alone would interleave the two columns into nonsense."""
    positions = [
        paper.text.index(heading)
        for heading in (
            "1. Introduction",
            "2. Background",
            "2.1 Chunking",
            "2.2 Embeddings",
            "3. Methods",
            "3.1 Corpus",
            "3.2 Sampling",
            "4. Results",
            "5. Discussion",
        )
    ]
    assert positions == sorted(positions)


def test_sentences_survive_the_column_and_page_breaks_intact(paper) -> None:  # type: ignore[no-untyped-def]
    for sentence in (
        "The sampling frame was drawn from 4,318 publicly available support articles",
        "Stripping running headers and footers before embedding improved mean reciprocal",
        "Chunks of roughly 512 tokens with 64 tokens of overlap outperformed both",
    ):
        assert sentence in " ".join(paper.text.split())


def test_the_gutter_is_found_on_the_dense_two_column_pages() -> None:
    """`test_columns_are_read_in_the_right_order` samples page 1, and page 1 is the easy page.

    Pages 2 and 3 are solid two-column body text, and that is where detection actually gets
    hard. A Row groups words by vertical position, so on a dense page almost every row holds
    words from *both* columns: its x0 is in the left column and its x1 in the right. A check
    asking whether a row lies *wholly* on one side answers "neither" for exactly the pages
    this function exists to detect, the gutter is discarded, the page is read as one column,
    and the two streams interleave line by line into text like

        "The evaluation harness was contrasted with an The retrieval layer was contrasted
         with an ablation with ablation with overlap removed entirely."

    Nothing else in this suite notices, because every structural invariant survives it: the
    garbled text is deterministic and still slices back to its own offsets. So this asserts
    the layout property directly rather than sampling prose, which is what let the bug hide.

    Page 4 is excluded deliberately -- its right column is genuinely empty, so returning None
    there is correct and reading it as a single column is right.
    """
    pdf = _open_pdf(fixture_bytes("paper_two_column.pdf"))
    rows, page_count, _ = _collect_rows(pdf, max_pages=500)
    running = find_running_rows(rows, page_count)
    kept = [row for index, row in enumerate(rows) if index not in running]
    widths = {number: float(page.width) for number, page in enumerate(pdf.pages, start=1)}

    for page_number in (1, 2, 3):
        page_rows_ = [row for row in kept if row.page == page_number]
        gutter = find_gutter(page_rows_, widths[page_number])
        assert gutter is not None, (
            f"page {page_number} is two-column but no gutter was found, so its columns "
            f"interleave"
        )
        assert 0.25 < (gutter / widths[page_number]) < 0.75


def test_a_word_hyphenated_across_a_line_break_is_rejoined(paper) -> None:  # type: ignore[no-untyped-def]
    assert "reproducibility" in paper.text
    assert "reproduci-" not in paper.text


def test_headings_are_detected_with_a_usable_path(paper) -> None:  # type: ignore[no-untyped-def]
    assert paper.headings_reliable
    headings = [
        " ".join(paper.slice(block).split())
        for block in paper.blocks
        if block.kind is BlockKind.HEADING
    ]
    assert "3. Methods" in headings
    assert "3.2 Sampling" in headings


def test_heading_levels_distinguish_sections_from_subsections(paper) -> None:  # type: ignore[no-untyped-def]
    levels = {
        " ".join(paper.slice(block).split()): block.level
        for block in paper.blocks
        if block.kind is BlockKind.HEADING
    }
    assert levels["3. Methods"] < levels["3.2 Sampling"]


def test_a_short_document_keeps_its_repeated_lines() -> None:
    """Repetition across one or two pages is coincidence, not a running header."""
    document = extract_pdf(fixture_bytes("release_notes.pdf"), max_pages=500)
    assert "Release notes" in document.text


# --- the ugly real cases ------------------------------------------------------------------


def test_an_encrypted_pdf_fails_permanently_with_readable_copy() -> None:
    with pytest.raises(PermanentIngestError) as error:
        extract_pdf(fixture_bytes("encrypted.pdf"), max_pages=500)
    message = error.value.user_message
    assert "password-protected" in message
    assert message.endswith(".") and "Traceback" not in message


def test_a_scanned_pdf_says_so_rather_than_failing_vaguely() -> None:
    """OCR is out of scope, so the message has to explain the limitation, not hide it."""
    with pytest.raises(PermanentIngestError) as error:
        extract_pdf(fixture_bytes("scanned_no_text.pdf"), max_pages=500)
    assert "scan" in error.value.user_message.lower()


def test_a_file_that_lies_about_being_a_pdf_fails_permanently() -> None:
    with pytest.raises(PermanentIngestError) as error:
        extract(b"PK\x03\x04 this is a zip", "application/pdf", max_pages=500)
    assert "not a readable PDF" in error.value.user_message


def test_a_zero_byte_file_fails_permanently() -> None:
    with pytest.raises(PermanentIngestError) as error:
        extract_pdf(b"", max_pages=500)
    assert "empty" in error.value.user_message.lower()


def test_a_damaged_pdf_fails_permanently() -> None:
    with pytest.raises(PermanentIngestError):
        extract_pdf(b"%PDF-1.7\nthis is not really a pdf body", max_pages=500)


def test_the_page_limit_is_enforced_with_both_numbers() -> None:
    with pytest.raises(PermanentIngestError) as error:
        extract_pdf(fixture_bytes("paper_two_column.pdf"), max_pages=2)
    message = error.value.user_message
    assert "4 pages" in message and "2 pages" in message


def test_extraction_is_deterministic() -> None:
    content = fixture_bytes("paper_two_column.pdf")
    first = extract_pdf(content, max_pages=500)
    second = extract_pdf(content, max_pages=500)
    assert first.text == second.text
    assert first.blocks == second.blocks
    assert first.pages == second.pages


def test_the_document_title_is_not_the_root_of_every_heading_path(paper) -> None:  # type: ignore[no-untyped-def]
    """Contract 5's example is "3. Methods > 3.2 Sampling", not "<Whole Title> > 3. Methods".

    A paper's title is set in the largest type on the page, so font-based detection correctly
    calls it a heading — and then every citation in the document repeats information the
    filename already carries.
    """
    from queryll_worker.chunking import chunk_document
    from queryll_worker.config import ChunkingSettings

    paths = [
        chunk.heading_path
        for chunk in chunk_document(paper, ChunkingSettings())
        if chunk.heading_path
    ]
    assert paths, "the paper should produce heading paths at all"
    assert not any(path.startswith(RUNNING_HEADER) for path in paths)
    assert "3. Methods > 3.2 Sampling" in paths


def test_a_first_heading_that_is_a_real_section_is_kept() -> None:
    """The demotion only fires on an unambiguous title, never on a leading section heading."""
    from queryll_worker.parsing.pdf import _demote_document_title

    # One largest heading, first, with deeper levels below it: a title.
    assert _demote_document_title([1, 2, 3, 2]) == [None, 2, 3, 2]
    # Two headings share the largest size, so neither is a title.
    assert _demote_document_title([1, 2, 1, 2]) == [1, 2, 1, 2]
    # Nothing beneath it: a one-level document, not a title page.
    assert _demote_document_title([1, None, None]) == [1, None, None]
    # The document does not open with a heading at all.
    assert _demote_document_title([None, 1, 2]) == [None, 1, 2]
