"""End to end, against real Postgres and the deterministic fake embedder.

These are the tests that check the two contracts actually meet: a document goes
`pending → processing → ready` with progress visible along the way (Contract 3 §4, §7), the
chunk rows carry offsets that slice back to the extracted text (Contract 5 §5), and a job that
runs twice leaves exactly the database a job that ran once would (Contract 3 §5).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Sequence

import pytest
from sqlalchemy import text

from queryll_worker import queue
from queryll_worker.config import Settings
from queryll_worker.db import session_scope
from queryll_worker.embeddings import FakeEmbedder, fake_vector
from queryll_worker.errors import TransientIngestError
from queryll_worker.parsing import extract
from queryll_worker.pipeline import batch_size_for, run_job
from tests.conftest import fixture_bytes

MARKDOWN = fixture_bytes("ingestion_notes.md")


class RecordingEmbedder(FakeEmbedder):
    """A fake embedder that snapshots `documents.progress` before each batch."""

    def __init__(self, session_factory, document_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self._session_factory = session_factory
        self._document_id = document_id
        self.progress_snapshots: list[float] = []

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        async with self._session_factory() as session:
            result = await session.execute(
                text("SELECT progress, status::text FROM documents WHERE id = :id"),
                {"id": self._document_id},
            )
            row = result.mappings().one()
        assert row["status"] == "processing"
        self.progress_snapshots.append(float(row["progress"]))
        return await super().embed_documents(texts)


class FailingEmbedder(FakeEmbedder):
    """Voyage is down."""

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise TransientIngestError(detail="voyage unavailable after retries")


async def _claim(session_factory, worker: str = "test-worker"):  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        return await queue.claim_next_job(session, worker)


async def _document(session_factory, document_id: uuid.UUID) -> dict:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT status::text AS status, progress, chunk_count, page_count, "
                "error_message FROM documents WHERE id = :id"
            ),
            {"id": document_id},
        )
        return dict(result.mappings().one())


async def _chunks(session_factory, document_id: uuid.UUID) -> list[dict]:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT ordinal, text, token_count, char_start, char_end, page_start, "
                "page_end, heading_path FROM chunks WHERE document_id = :id ORDER BY ordinal"
            ),
            {"id": document_id},
        )
        return [dict(row) for row in result.mappings()]


# --- the happy path --------------------------------------------------------------------------


async def test_a_markdown_document_reaches_ready(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    document_id, _ = await enqueue(
        MARKDOWN, filename="notes.md", mime_type="text/markdown"
    )
    job = await _claim(session_factory)
    assert job is not None

    outcome = await run_job(
        job,
        session_factory=session_factory,
        settings=settings,
        embedder=FakeEmbedder(),
    )

    assert outcome.status == "ready"
    row = await _document(session_factory, document_id)
    assert row["status"] == "ready"
    assert row["progress"] == pytest.approx(1.0)
    assert row["chunk_count"] == outcome.chunk_count > 0
    assert row["error_message"] is None
    # A Markdown document has no pages, so the UI shows no page chip.
    assert row["page_count"] is None


async def test_chunk_offsets_slice_back_to_the_extracted_text(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """The promise Contract 5 §5 makes to the citation viewer, checked through the database."""
    document_id, _ = await enqueue(
        MARKDOWN, filename="notes.md", mime_type="text/markdown"
    )
    job = await _claim(session_factory)
    await run_job(
        job, session_factory=session_factory, settings=settings, embedder=FakeEmbedder()
    )

    extracted = extract(MARKDOWN, "text/markdown", max_pages=settings.max_pages)
    for row in await _chunks(session_factory, document_id):
        assert extracted.text[row["char_start"] : row["char_end"]] == row["text"]


async def test_a_pdf_records_its_page_count_and_page_ranges(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    document_id, _ = await enqueue(
        fixture_bytes("paper_two_column.pdf"),
        filename="paper.pdf",
        mime_type="application/pdf",
    )
    job = await _claim(session_factory)
    outcome = await run_job(
        job, session_factory=session_factory, settings=settings, embedder=FakeEmbedder()
    )

    assert outcome.status == "ready"
    assert (await _document(session_factory, document_id))["page_count"] == 4
    rows = await _chunks(session_factory, document_id)
    assert rows
    for row in rows:
        assert row["page_start"] is not None and row["page_end"] is not None
        assert 1 <= row["page_start"] <= row["page_end"] <= 4


async def test_progress_is_written_while_the_run_is_still_in_flight(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """A bar that sits at zero and then jumps to done is indistinguishable from a hang."""
    document_id, _ = await enqueue(
        fixture_bytes("paper_two_column.pdf"),
        filename="paper.pdf",
        mime_type="application/pdf",
    )
    job = await _claim(session_factory)
    embedder = RecordingEmbedder(session_factory, document_id)
    tuned = replace(settings, embedding_batch_size=4)

    await run_job(
        job, session_factory=session_factory, settings=tuned, embedder=embedder
    )

    assert len(embedder.calls) > 1, "the document should have taken more than one batch"
    assert embedder.progress_snapshots[0] == pytest.approx(0.0)
    assert embedder.progress_snapshots == sorted(embedder.progress_snapshots)
    assert max(embedder.progress_snapshots) < 1.0
    assert (await _document(session_factory, document_id))["progress"] == pytest.approx(1.0)


async def test_rerunning_the_same_job_is_idempotent(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """The reclaim window is only safe because of this (Contract 3 §5)."""
    document_id, job_id = await enqueue(
        MARKDOWN, filename="notes.md", mime_type="text/markdown"
    )
    job = await _claim(session_factory)
    await run_job(
        job, session_factory=session_factory, settings=settings, embedder=FakeEmbedder()
    )
    first = await _chunks(session_factory, document_id)

    async with session_factory() as session:
        await session.execute(
            text("UPDATE ingestion_jobs SET state = 'queued' WHERE id = :id"),
            {"id": job_id},
        )
        await session.commit()

    second_job = await _claim(session_factory)
    assert second_job is not None
    await run_job(
        second_job,
        session_factory=session_factory,
        settings=settings,
        embedder=FakeEmbedder(),
    )
    second = await _chunks(session_factory, document_id)

    assert first == second
    assert (await _document(session_factory, document_id))["chunk_count"] == len(first)


async def test_stored_vectors_match_the_stored_text(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """A chunk paired with another chunk's vector is invisible until retrieval is wrong."""
    document_id, _ = await enqueue(
        MARKDOWN, filename="notes.md", mime_type="text/markdown"
    )
    job = await _claim(session_factory)
    await run_job(
        job, session_factory=session_factory, settings=settings, embedder=FakeEmbedder()
    )

    rows = await _chunks(session_factory, document_id)
    target = rows[-1]["text"]
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT text, 1 - (embedding <=> CAST(:probe AS vector)) AS similarity "
                "FROM chunks WHERE document_id = :id "
                "ORDER BY embedding <=> CAST(:probe AS vector) LIMIT 1"
            ),
            {"probe": str(list(fake_vector(target))), "id": document_id},
        )
        row = result.mappings().one()
    assert row["text"] == target
    assert row["similarity"] == pytest.approx(1.0, abs=1e-4)


# --- failure paths ---------------------------------------------------------------------------


async def test_a_scanned_pdf_fails_immediately_without_burning_three_attempts(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(
        fixture_bytes("scanned_no_text.pdf"),
        filename="scan.pdf",
        mime_type="application/pdf",
    )
    job = await _claim(session_factory)
    outcome = await run_job(
        job, session_factory=session_factory, settings=settings, embedder=FakeEmbedder()
    )

    assert outcome.status == "failed"
    row = await _document(session_factory, document_id)
    assert row["status"] == "failed"
    assert "scan" in row["error_message"].lower()

    async with session_factory() as session:
        result = await session.execute(
            text("SELECT state::text AS state, attempts FROM ingestion_jobs WHERE id = :id"),
            {"id": job_id},
        )
        job_row = result.mappings().one()
    assert job_row["state"] == "failed"
    assert job_row["attempts"] == 1


async def test_an_encrypted_pdf_fails_with_copy_the_ui_can_render(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    document_id, _ = await enqueue(
        fixture_bytes("encrypted.pdf"), filename="locked.pdf", mime_type="application/pdf"
    )
    job = await _claim(session_factory)
    await run_job(
        job, session_factory=session_factory, settings=settings, embedder=FakeEmbedder()
    )

    message = (await _document(session_factory, document_id))["error_message"]
    assert message == "This PDF is password-protected, so its text could not be read."


async def test_a_failed_document_always_has_an_error_message(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """Contract 3 §4 makes this mandatory: the UI renders the string verbatim."""
    document_id, _ = await enqueue(b"\x00\x00binary", filename="odd.txt")
    job = await _claim(session_factory)
    await run_job(
        job, session_factory=session_factory, settings=settings, embedder=FakeEmbedder()
    )

    row = await _document(session_factory, document_id)
    assert row["status"] == "failed"
    assert row["error_message"] and row["error_message"].endswith(".")


async def test_a_transient_failure_requeues_the_job(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(
        MARKDOWN, filename="notes.md", mime_type="text/markdown"
    )
    job = await _claim(session_factory)
    outcome = await run_job(
        job,
        session_factory=session_factory,
        settings=settings,
        embedder=FailingEmbedder(),
    )

    assert outcome.status == "requeued"
    assert (await _document(session_factory, document_id))["status"] == "pending"
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT state::text AS state, last_error FROM ingestion_jobs WHERE id = :id"),
            {"id": job_id},
        )
        row = result.mappings().one()
    assert row["state"] == "queued"
    assert "voyage" in row["last_error"]


async def test_the_third_attempt_fails_the_document_for_good(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """Contract 3 §6: `attempts >= 3` is terminal."""
    document_id, job_id = await enqueue(
        MARKDOWN, filename="notes.md", mime_type="text/markdown"
    )

    for expected in ("requeued", "requeued", "failed"):
        job = await _claim(session_factory)
        assert job is not None
        outcome = await run_job(
            job,
            session_factory=session_factory,
            settings=settings,
            embedder=FailingEmbedder(),
        )
        assert outcome.status == expected

    row = await _document(session_factory, document_id)
    assert row["status"] == "failed"
    assert row["error_message"]
    # No provider payload, no traceback, nothing a support agent would have to translate.
    assert "voyage" not in row["error_message"].lower()

    async with session_factory() as session:
        result = await session.execute(
            text("SELECT attempts FROM ingestion_jobs WHERE id = :id"), {"id": job_id}
        )
    assert result.scalar() == 3


async def test_a_document_deleted_before_ingestion_is_not_resurrected(
    session_factory, enqueue, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    document_id, _ = await enqueue(
        MARKDOWN, filename="notes.md", mime_type="text/markdown"
    )
    job = await _claim(session_factory)
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM documents WHERE id = :id"), {"id": document_id}
        )
        await session.commit()

    outcome = await run_job(
        job, session_factory=session_factory, settings=settings, embedder=FakeEmbedder()
    )
    assert outcome.status == "vanished"
    assert await _chunks(session_factory, document_id) == []


# --- pure helpers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "total,configured,expected",
    [
        (0, 128, 128),
        (10, 128, 16),
        (200, 128, 25),
        (5000, 128, 128),
        (200, 8, 8),
    ],
)
def test_batch_size_never_exceeds_the_contract_ceiling(
    total: int, configured: int, expected: int
) -> None:
    assert batch_size_for(total, configured) == expected
