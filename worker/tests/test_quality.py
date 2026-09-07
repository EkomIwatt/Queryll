"""Retrieval quality of the chunker, as a regression test.

This is the test that would actually catch a bad chunking change. The others prove the
chunker is *consistent*; this one asks whether the passage that answers a question survives
chunking as one coherent, retrievable piece.

The scorer is lexical rather than semantic, for the reason set out in `queryll_worker.quality`:
no test may call the live Voyage API, and the fake embedder produces noise. A passage split
across two chunks ranks badly under BM25 for the same reason it would rank badly under a real
embedding — the evidence for the answer is no longer in one place.

The thresholds here are floors, not targets. If a chunking change pushes them down, that
change made the product worse and the number says so before a human has to notice it in a
citation.
"""

from __future__ import annotations

import pytest

from queryll_worker.chunking import chunk_document
from queryll_worker.config import ChunkingSettings
from queryll_worker.parsing import extract
from queryll_worker.quality import QualityCase, evaluate
from tests.conftest import fixture_bytes

CASES = (
    QualityCase(
        question="How many support articles were in the sampling frame?",
        expected_phrase=(
            "The sampling frame was drawn from 4,318 publicly available support articles"
        ),
    ),
    QualityCase(
        question="What chunk size and overlap performed best?",
        expected_phrase=(
            "Chunks of roughly 512 tokens with 64 tokens of overlap outperformed both"
        ),
    ),
    QualityCase(
        question="What effect did stripping running headers have on mean reciprocal rank?",
        expected_phrase=(
            "Stripping running headers and footers before embedding improved mean reciprocal"
        ),
    ),
    QualityCase(
        question="How were annotator disagreements resolved?",
        expected_phrase="disagreements were resolved by discussion",
    ),
)


@pytest.fixture(scope="module")
def report():  # type: ignore[no-untyped-def]
    document = extract(
        fixture_bytes("paper_two_column.pdf"), "application/pdf", max_pages=500
    )
    chunks = chunk_document(document, ChunkingSettings())
    return evaluate(chunks, CASES)


def test_every_answer_survives_chunking_in_one_piece(report) -> None:  # type: ignore[no-untyped-def]
    """Coverage is the one metric that must be perfect.

    A miss here means the sentence that answers the question was split across a chunk
    boundary. No amount of retrieval tuning recovers from that — the evidence is simply not
    in any single passage the model will be shown.
    """
    missing = [result.question for result in report.results if result.containing == 0]
    assert not missing, f"chunking split the answer for: {missing}\n{report.format()}"


def test_the_answer_bearing_chunk_ranks_first(report) -> None:  # type: ignore[no-untyped-def]
    assert report.hit_at_1 >= 0.75, report.format()


def test_the_answer_bearing_chunk_is_always_near_the_top(report) -> None:  # type: ignore[no-untyped-def]
    assert report.hit_at_3 == 1.0, report.format()


def test_mean_reciprocal_rank_floor(report) -> None:  # type: ignore[no-untyped-def]
    assert report.mrr >= 0.8, report.format()


def test_the_report_is_stable_across_runs() -> None:
    """Ties break on ordinal, so two runs of the harness cannot disagree."""
    document = extract(
        fixture_bytes("paper_two_column.pdf"), "application/pdf", max_pages=500
    )
    chunks = chunk_document(document, ChunkingSettings())
    assert evaluate(chunks, CASES) == evaluate(chunks, CASES)


def test_a_question_about_nothing_in_the_document_finds_no_passage() -> None:
    """The harness must be able to say "miss", or its passes mean nothing."""
    document = extract(
        fixture_bytes("paper_two_column.pdf"), "application/pdf", max_pages=500
    )
    chunks = chunk_document(document, ChunkingSettings())
    report = evaluate(
        chunks,
        (
            QualityCase(
                question="What is the boiling point of mercury?",
                expected_phrase="the boiling point of mercury is 356.7 degrees",
            ),
        ),
    )
    assert report.coverage == 0.0
    assert report.results[0].rank is None
