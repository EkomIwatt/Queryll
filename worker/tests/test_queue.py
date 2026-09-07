"""The job protocol against real Postgres (Contract 3).

Claiming is the one piece of this worker that cannot be verified by reading the code. Two
workers really are started against one queued job here, with the first transaction held open,
because `FOR UPDATE SKIP LOCKED` behaving the way the contract says is the difference between
a durable queue and a duplicate-chunk generator.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from queryll_worker import queue
from queryll_worker.chunking import ChunkCandidate
from queryll_worker.db import session_scope
from queryll_worker.embeddings import fake_vector


def _candidate(ordinal: int, body: str) -> ChunkCandidate:
    return ChunkCandidate(
        ordinal=ordinal,
        text=body,
        token_count=len(body.split()),
        char_start=ordinal * 100,
        char_end=ordinal * 100 + len(body),
        page_start=ordinal + 1,
        page_end=ordinal + 1,
        heading_path="1. Section" if ordinal else None,
    )


async def _job_row(session_factory, job_id: uuid.UUID) -> dict:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT * FROM ingestion_jobs WHERE id = :id"), {"id": job_id}
        )
        return dict(result.mappings().one())


async def _document_row(session_factory, document_id: uuid.UUID) -> dict:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT status::text AS status, progress, chunk_count, page_count, "
                "error_message, indexed_at FROM documents WHERE id = :id"
            ),
            {"id": document_id},
        )
        return dict(result.mappings().one())


# --- claiming -----------------------------------------------------------------------------


async def test_an_empty_queue_claims_nothing(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_scope(session_factory) as session:
        assert await queue.claim_next_job(session, "worker-a") is None


async def test_claiming_marks_the_job_running_and_counts_the_attempt(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(b"body text")

    async with session_scope(session_factory) as session:
        job = await queue.claim_next_job(session, "worker-a")

    assert job is not None
    assert job.document_id == document_id
    assert job.attempts == 1

    row = await _job_row(session_factory, job_id)
    assert row["state"] == "running"
    assert row["locked_by"] == "worker-a"
    assert row["locked_at"] is not None


async def test_two_workers_never_claim_the_same_job(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    """`SKIP LOCKED` makes the second worker step over the locked row, not block on it."""
    await enqueue(b"body text")

    async with session_factory() as first, session_factory() as second:
        claimed_by_first = await queue.claim_next_job(first, "worker-a")
        # The first transaction is still open and holding the row lock.
        claimed_by_second = await queue.claim_next_job(second, "worker-b")
        await first.commit()
        await second.rollback()

    assert claimed_by_first is not None
    assert claimed_by_second is None


async def test_a_second_worker_claims_a_second_job(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    await enqueue(b"first")
    await enqueue(b"second")

    async with session_factory() as first, session_factory() as second:
        claimed_by_first = await queue.claim_next_job(first, "worker-a")
        claimed_by_second = await queue.claim_next_job(second, "worker-b")
        await first.commit()
        await second.commit()

    assert claimed_by_first is not None and claimed_by_second is not None
    assert claimed_by_first.document_id != claimed_by_second.document_id


async def test_jobs_are_claimed_oldest_first(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    older, older_job = await enqueue(b"older")
    await enqueue(b"newer")
    async with session_factory() as session:
        await session.execute(
            text("UPDATE ingestion_jobs SET created_at = now() - interval '1 hour' "
                 "WHERE id = :id"),
            {"id": older_job},
        )
        await session.commit()

    async with session_scope(session_factory) as session:
        job = await queue.claim_next_job(session, "worker-a")
    assert job is not None and job.document_id == older


async def test_a_job_orphaned_by_a_killed_worker_is_reclaimed(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    """The fifteen-minute window is how a crashed run comes back (Contract 3 §3)."""
    _document_id, job_id = await enqueue(b"body text")
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE ingestion_jobs SET state = 'running', attempts = 1, "
                "locked_at = now() - interval '20 minutes', locked_by = 'dead-worker' "
                "WHERE id = :id"
            ),
            {"id": job_id},
        )
        await session.commit()

    async with session_scope(session_factory) as session:
        job = await queue.claim_next_job(session, "worker-b")

    assert job is not None
    assert job.attempts == 2
    assert (await _job_row(session_factory, job_id))["locked_by"] == "worker-b"


async def test_a_job_still_within_the_window_is_left_alone(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    _document_id, job_id = await enqueue(b"body text")
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE ingestion_jobs SET state = 'running', "
                "locked_at = now() - interval '1 minute' WHERE id = :id"
            ),
            {"id": job_id},
        )
        await session.commit()

    async with session_scope(session_factory) as session:
        assert await queue.claim_next_job(session, "worker-b") is None


async def test_finished_jobs_are_never_reclaimed(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    _document_id, job_id = await enqueue(b"body text")
    async with session_factory() as session:
        await session.execute(
            text("UPDATE ingestion_jobs SET state = 'done' WHERE id = :id"),
            {"id": job_id},
        )
        await session.commit()

    async with session_scope(session_factory) as session:
        assert await queue.claim_next_job(session, "worker-a") is None


# --- lifecycle writes ----------------------------------------------------------------------


async def test_begin_processing_clears_the_previous_error(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    document_id, _ = await enqueue(b"body", status="failed")
    async with session_factory() as session:
        await session.execute(
            text("UPDATE documents SET error_message = 'old failure' WHERE id = :id"),
            {"id": document_id},
        )
        await session.commit()

    async with session_scope(session_factory) as session:
        await queue.begin_processing(session, document_id)

    row = await _document_row(session_factory, document_id)
    assert row["status"] == "processing"
    assert row["progress"] == pytest.approx(0.0)
    assert row["error_message"] is None


async def test_progress_is_clamped_below_one_while_processing(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    """Only `ready` may show a finished bar (Contract 3 §7)."""
    document_id, _ = await enqueue(b"body")
    async with session_scope(session_factory) as session:
        await queue.record_progress(session, document_id, 10, 10)
    assert (await _document_row(session_factory, document_id))["progress"] == pytest.approx(
        0.99
    )


async def test_progress_of_a_partial_run(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    document_id, _ = await enqueue(b"body")
    async with session_scope(session_factory) as session:
        await queue.record_progress(session, document_id, 1, 4)
    assert (await _document_row(session_factory, document_id))["progress"] == pytest.approx(
        0.25
    )


async def test_committing_chunks_marks_the_document_ready(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(b"body")
    candidates = [_candidate(0, "first passage"), _candidate(1, "second passage")]
    vectors = [fake_vector(c.text) for c in candidates]

    async with session_scope(session_factory) as session:
        assert await queue.commit_chunks(
            session, job_id, document_id, candidates, vectors, 7
        )

    row = await _document_row(session_factory, document_id)
    assert row["status"] == "ready"
    assert row["progress"] == pytest.approx(1.0)
    assert row["chunk_count"] == 2
    assert row["page_count"] == 7
    assert row["indexed_at"] is not None
    assert (await _job_row(session_factory, job_id))["state"] == "done"


async def test_rerunning_a_job_replaces_chunks_rather_than_duplicating_them(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    """Contract 3 §5: a job that runs twice leaves the database a job that ran once would."""
    document_id, job_id = await enqueue(b"body")
    candidates = [_candidate(0, "first passage"), _candidate(1, "second passage")]
    vectors = [fake_vector(c.text) for c in candidates]

    for _ in range(2):
        async with session_scope(session_factory) as session:
            await queue.commit_chunks(
                session, job_id, document_id, candidates, vectors, 7
            )

    async with session_factory() as session:
        result = await session.execute(
            text("SELECT ordinal, text FROM chunks WHERE document_id = :id ORDER BY ordinal"),
            {"id": document_id},
        )
        rows = result.mappings().all()

    assert [row["ordinal"] for row in rows] == [0, 1]
    assert [row["text"] for row in rows] == ["first passage", "second passage"]


async def test_chunk_ids_change_on_a_rerun(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    """Which is why Contract 8 resolves citations at answer time and never caches an id."""
    document_id, job_id = await enqueue(b"body")
    candidates = [_candidate(0, "first passage")]
    vectors = [fake_vector(candidates[0].text)]

    ids: list[set[uuid.UUID]] = []
    for _ in range(2):
        async with session_scope(session_factory) as session:
            await queue.commit_chunks(
                session, job_id, document_id, candidates, vectors, 1
            )
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT id FROM chunks WHERE document_id = :id"), {"id": document_id}
            )
            ids.append({row[0] for row in result})

    assert ids[0] != ids[1]


async def test_committing_chunks_for_a_deleted_document_writes_nothing(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(b"body")
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM documents WHERE id = :id"), {"id": document_id}
        )
        await session.commit()

    async with session_scope(session_factory) as session:
        written = await queue.commit_chunks(
            session, job_id, document_id, [_candidate(0, "x")], [fake_vector("x")], 1
        )
    assert written is False


async def test_a_mismatched_vector_count_is_refused(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(b"body")
    async with session_factory() as session, pytest.raises(ValueError):
        await queue.commit_chunks(
            session, job_id, document_id, [_candidate(0, "x")], [], 1
        )


async def test_deleting_a_document_cascades(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(b"body")
    async with session_scope(session_factory) as session:
        await queue.commit_chunks(
            session, job_id, document_id, [_candidate(0, "x")], [fake_vector("x")], 1
        )
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM documents WHERE id = :id"), {"id": document_id}
        )
        await session.commit()
        remaining = await session.execute(
            text(
                "SELECT (SELECT count(*) FROM chunks WHERE document_id = :id) "
                "+ (SELECT count(*) FROM ingestion_jobs WHERE document_id = :id)"
            ),
            {"id": document_id},
        )
    assert remaining.scalar() == 0


async def test_failing_a_document_stores_copy_a_user_can_read(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(b"body")
    message = "This PDF is password-protected, so its text could not be read."

    async with session_scope(session_factory) as session:
        await queue.fail_document(
            session, job_id, document_id, user_message=message, detail="pdf is encrypted"
        )

    row = await _document_row(session_factory, document_id)
    assert row["status"] == "failed"
    assert row["error_message"] == message
    job = await _job_row(session_factory, job_id)
    assert job["state"] == "failed"
    # The internal detail stays on the job row, where only an admin view can see it.
    assert job["last_error"] == "pdf is encrypted"


async def test_requeueing_returns_the_document_to_pending(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(b"body")
    async with session_scope(session_factory) as session:
        await queue.begin_processing(session, document_id)
    async with session_scope(session_factory) as session:
        await queue.requeue_job(session, job_id, document_id, detail="voyage 503")

    row = await _document_row(session_factory, document_id)
    assert row["status"] == "pending"
    assert row["progress"] == pytest.approx(0.0)
    job = await _job_row(session_factory, job_id)
    assert job["state"] == "queued"
    assert job["locked_at"] is None and job["locked_by"] is None


# --- the vector column itself ---------------------------------------------------------------


async def test_vectors_round_trip_and_rank_by_cosine_distance(
    session_factory, enqueue
) -> None:  # type: ignore[no-untyped-def]
    """Cosine, always `<=>`. A mismatched operator would silently drop the HNSW index."""
    document_id, job_id = await enqueue(b"body")
    candidates = [_candidate(i, f"passage number {i}") for i in range(3)]
    vectors = [fake_vector(c.text) for c in candidates]

    async with session_scope(session_factory) as session:
        await queue.commit_chunks(
            session, job_id, document_id, candidates, vectors, 3
        )

    probe = fake_vector("passage number 1")
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT text, 1 - (embedding <=> CAST(:probe AS vector)) AS similarity "
                "FROM chunks WHERE document_id = :id "
                "ORDER BY embedding <=> CAST(:probe AS vector) LIMIT 1"
            ),
            {"probe": str(list(probe)), "id": document_id},
        )
        row = result.mappings().one()

    assert row["text"] == "passage number 1"
    assert row["similarity"] == pytest.approx(1.0, abs=1e-4)


async def test_stored_vectors_keep_their_width(session_factory, enqueue) -> None:  # type: ignore[no-untyped-def]
    document_id, job_id = await enqueue(b"body")
    async with session_scope(session_factory) as session:
        await queue.commit_chunks(
            session, job_id, document_id, [_candidate(0, "x")], [fake_vector("x")], 1
        )
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT vector_dims(embedding) FROM chunks WHERE document_id = :id"),
            {"id": document_id},
        )
    assert result.scalar() == 1024
