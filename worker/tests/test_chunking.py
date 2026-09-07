"""Chunking — the invariants that make citations trustworthy (Contract 5).

Two properties here are load-bearing for the whole product and are tested over every fixture
rather than over one hand-written string:

* **The offset invariant.** `text[c.char_start:c.char_end] == c.text` for every chunk. This is
  what the citation viewer relies on when it opens a passage, and it is exactly what an
  off-by-one in the overlap logic destroys.
* **Determinism.** The same bytes produce the same chunk list — in one process, twice, and in
  a freshly started interpreter. That is what makes a re-index safe to run at any time and a
  chunking bug reproducible from the stored bytes alone.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from queryll_worker.chunking import chunk_document, count_tokens
from queryll_worker.config import ChunkingSettings
from queryll_worker.parsing import extract, extract_markdown, extract_plain_text
from tests.conftest import fixture_bytes

SETTINGS = ChunkingSettings()

PROSE = " ".join(
    f"Sentence number {index} carries a distinct clause about retrieval quality."
    for index in range(120)
)


def _documents() -> list[tuple[str, object]]:
    """Every fixture we have, so the invariants are checked against real inputs."""
    return [
        ("markdown", extract_markdown(fixture_bytes("ingestion_notes.md"))),
        ("plain", extract_plain_text(PROSE.encode("utf-8"))),
        (
            "two-column pdf",
            extract(
                fixture_bytes("paper_two_column.pdf"), "application/pdf", max_pages=500
            ),
        ),
        (
            "single-column pdf",
            extract(fixture_bytes("release_notes.pdf"), "application/pdf", max_pages=500),
        ),
    ]


@pytest.mark.parametrize("name,document", _documents(), ids=lambda value: str(value)[:20])
def test_every_chunk_slices_back_to_its_own_text(name: str, document) -> None:  # type: ignore[no-untyped-def]
    """The invariant Contract 5 §5 promises the citation UI."""
    chunks = chunk_document(document, SETTINGS)
    assert chunks, f"{name} produced no chunks"
    for chunk in chunks:
        assert document.text[chunk.char_start : chunk.char_end] == chunk.text


@pytest.mark.parametrize("name,document", _documents(), ids=lambda value: str(value)[:20])
def test_chunks_are_ordered_and_ordinals_are_dense(name: str, document) -> None:  # type: ignore[no-untyped-def]
    chunks = chunk_document(document, SETTINGS)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    starts = [chunk.char_start for chunk in chunks]
    assert starts == sorted(starts)


@pytest.mark.parametrize("name,document", _documents(), ids=lambda value: str(value)[:20])
def test_chunks_never_start_or_end_on_whitespace(name: str, document) -> None:  # type: ignore[no-untyped-def]
    """A chunk opening with a blank line reads as broken under a citation."""
    for chunk in chunk_document(document, SETTINGS):
        assert chunk.text == chunk.text.strip()


@pytest.mark.parametrize("name,document", _documents(), ids=lambda value: str(value)[:20])
def test_token_counts_match_the_stored_text(name: str, document) -> None:  # type: ignore[no-untyped-def]
    for chunk in chunk_document(document, SETTINGS):
        assert chunk.token_count == count_tokens(chunk.text)


def test_chunks_land_near_the_target_size() -> None:
    document = extract_plain_text(PROSE.encode("utf-8"))
    chunks = chunk_document(document, SETTINGS)
    assert len(chunks) > 1

    # Every chunk but the last should be a substantial fraction of the target, and none should
    # blow far past it: packing chooses whole sentences, so a little overshoot is expected.
    for chunk in chunks[:-1]:
        assert SETTINGS.min_tokens <= chunk.token_count
        assert chunk.token_count <= SETTINGS.target_tokens * 1.4


def test_short_chunks_are_merged_rather_than_stored_alone() -> None:
    """Contract 5 §2: below 32 tokens a chunk is merged forward."""
    source = "\n\n".join(["Tiny.", "Also tiny.", "Still tiny."])
    document = extract_plain_text(source.encode("utf-8"))
    chunks = chunk_document(document, SETTINGS)
    assert len(chunks) == 1
    assert "Tiny." in chunks[0].text and "Still tiny." in chunks[0].text


def test_consecutive_chunks_overlap() -> None:
    document = extract_plain_text(PROSE.encode("utf-8"))
    chunks = chunk_document(document, SETTINGS)
    overlapping = [
        later.char_start < earlier.char_end
        for earlier, later in zip(chunks, chunks[1:])
    ]
    assert any(overlapping), "no chunk overlapped its predecessor"


def test_overlap_never_repeats_a_whole_chunk() -> None:
    """A chunk that is a superset of its predecessor would mean the packer failed to advance."""
    document = extract_plain_text(PROSE.encode("utf-8"))
    chunks = chunk_document(document, SETTINGS)
    for earlier, later in zip(chunks, chunks[1:]):
        assert later.char_start > earlier.char_start
        assert later.char_end > earlier.char_end


def test_a_sentence_longer_than_a_chunk_is_split_at_word_boundaries() -> None:
    giant = "word " * 4000
    document = extract_plain_text(giant.strip().encode("utf-8"))
    chunks = chunk_document(document, SETTINGS)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.startswith("word")
        assert chunk.text.endswith("word")


def test_headings_start_new_chunks_and_populate_the_heading_path() -> None:
    source = (
        "# Guide\n\n"
        "Opening paragraph about the guide.\n\n"
        "## Setup\n\n"
        "Install the dependencies first.\n\n"
        "### Database\n\n"
        "Start Postgres with pgvector before running anything else.\n"
    )
    document = extract_markdown(source.encode("utf-8"))
    chunks = chunk_document(document, ChunkingSettings(min_tokens=0, target_tokens=64))
    paths = [chunk.heading_path for chunk in chunks]
    assert "Guide > Setup > Database" in paths
    assert paths[0] == "Guide"


def test_heading_path_is_null_when_detection_was_unreliable() -> None:
    """A wrong heading is worse than no heading, so the whole document falls back."""
    document = extract_markdown(b"# Title\n\nSome body text here.\n")
    unreliable = type(document)(
        text=document.text,
        blocks=document.blocks,
        pages=document.pages,
        page_count=document.page_count,
        headings_reliable=False,
    )
    assert all(chunk.heading_path is None for chunk in chunk_document(unreliable, SETTINGS))


def test_text_documents_carry_no_page_numbers() -> None:
    document = extract_plain_text(PROSE.encode("utf-8"))
    for chunk in chunk_document(document, SETTINGS):
        assert chunk.page_start is None and chunk.page_end is None


def test_chunking_is_deterministic_within_one_process() -> None:
    document = extract(
        fixture_bytes("paper_two_column.pdf"), "application/pdf", max_pages=500
    )
    first = chunk_document(document, SETTINGS)
    second = chunk_document(document, SETTINGS)
    assert first == second


def test_chunking_is_deterministic_across_processes() -> None:
    """The same bytes, a brand-new interpreter, the same chunks (Contract 5 §4).

    Run in a subprocess on purpose: hash randomisation, dict ordering and module-level caches
    are exactly the things that make "deterministic" quietly untrue within one process.
    """
    root = Path(__file__).resolve().parents[1]
    script = (
        "import json,sys;"
        "sys.path.insert(0, r'{root}');"
        "from queryll_worker.parsing import extract;"
        "from queryll_worker.chunking import chunk_document;"
        "from queryll_worker.config import ChunkingSettings;"
        "content=open(r'{fixture}','rb').read();"
        "doc=extract(content,'application/pdf',max_pages=500);"
        "print(json.dumps([[c.ordinal,c.char_start,c.char_end,c.token_count,"
        "c.page_start,c.page_end,c.heading_path,c.text] "
        "for c in chunk_document(doc, ChunkingSettings())]))"
    ).format(root=root, fixture=root / "tests" / "fixtures" / "paper_two_column.pdf")

    runs = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": "random", "PATH": ""},
        )
        runs.append(json.loads(completed.stdout))

    document = extract(
        fixture_bytes("paper_two_column.pdf"), "application/pdf", max_pages=500
    )
    in_process = [
        [
            chunk.ordinal,
            chunk.char_start,
            chunk.char_end,
            chunk.token_count,
            chunk.page_start,
            chunk.page_end,
            chunk.heading_path,
            chunk.text,
        ]
        for chunk in chunk_document(document, SETTINGS)
    ]
    assert runs[0] == runs[1] == in_process


def test_empty_document_yields_no_chunks() -> None:
    document = extract_plain_text(b"Text.")
    empty = type(document)(text="", blocks=(), pages=(), page_count=None)
    assert chunk_document(empty, SETTINGS) == []
