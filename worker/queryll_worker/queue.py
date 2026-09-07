"""The Postgres job queue and every write this process makes (Contract 3).

There is no broker here. A job is a row, claimed atomically with `FOR UPDATE SKIP LOCKED`,
and that is the whole reason a document survives the API process restarting mid-upload: the
work is durable state, not an in-memory task.

Every function in this module writes only worker-owned columns (Contract 3 §1). The one
exception in the contract — the re-index reset — belongs to Instance 2 and is deliberately
absent here.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from queryll_worker.chunking import ChunkCandidate
from queryll_worker.logging_setup import kv
from queryll_worker.models import Chunk, Document, DocumentStatus, IngestionJob, JobState

logger = logging.getLogger(__name__)

#: Contract 3 §3, verbatim. The 15-minute window is how a job orphaned by a killed process
#: comes back, and it is why ingestion has to be idempotent (§5). Do not parameterise the
#: interval: this statement is contract text.
CLAIM_SQL = text(
    """
    UPDATE ingestion_jobs SET state = 'running', attempts = attempts + 1,
           locked_at = now(), locked_by = :worker_id, updated_at = now()
    WHERE id = (
      SELECT id FROM ingestion_jobs
      WHERE state = 'queued'
         OR (state = 'running' AND locked_at < now() - interval '15 minutes')
      ORDER BY created_at
      FOR UPDATE SKIP LOCKED
      LIMIT 1
    )
    RETURNING *;
    """
)


@dataclass(frozen=True)
class ClaimedJob:
    """A job this worker now owns, with the document it points at."""

    id: uuid.UUID
    document_id: uuid.UUID
    attempts: int


@dataclass(frozen=True)
class DocumentInput:
    """The parts of a `documents` row the worker needs in order to ingest it."""

    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    content: bytes


async def claim_next_job(session: AsyncSession, worker_id: str) -> ClaimedJob | None:
    """Claim one job, or return None when the queue is empty.

    Two workers running concurrently can never claim the same job: `SKIP LOCKED` makes the
    second one step over the row the first has locked rather than block on it.
    """
    result = await session.execute(CLAIM_SQL, {"worker_id": worker_id})
    row = result.mappings().first()
    if row is None:
        return None
    return ClaimedJob(
        id=row["id"], document_id=row["document_id"], attempts=int(row["attempts"])
    )


async def load_document(
    session: AsyncSession, document_id: uuid.UUID
) -> DocumentInput | None:
    """Read the document's bytes and metadata, or None if it has since been deleted."""
    result = await session.execute(
        select(
            Document.id,
            Document.filename,
            Document.mime_type,
            Document.size_bytes,
            Document.content,
        ).where(Document.id == document_id)
    )
    row = result.first()
    if row is None:
        return None
    return DocumentInput(
        id=row.id,
        filename=row.filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        content=bytes(row.content),
    )


async def begin_processing(session: AsyncSession, document_id: uuid.UUID) -> None:
    """`pending -> processing`. Progress restarts at 0 and any old error is cleared.

    Contract 3 §7: parsing and chunking happen before any chunk count is known, so a run
    genuinely sits at 0.0 for a moment — Instance 3 renders that as an indeterminate state
    rather than a stalled bar.
    """
    await session.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(
            status=DocumentStatus.PROCESSING,
            progress=0.0,
            error_message=None,
            indexed_at=None,
        )
    )


async def record_page_count(
    session: AsyncSession, document_id: uuid.UUID, page_count: int | None
) -> None:
    """Publish the page count as soon as parsing knows it, ahead of any embedding."""
    if page_count is None:
        return
    await session.execute(
        update(Document).where(Document.id == document_id).values(page_count=page_count)
    )


async def record_progress(
    session: AsyncSession, document_id: uuid.UUID, embedded: int, total: int
) -> None:
    """Write `embedded / total`, clamped to [0.0, 0.99] while processing (Contract 3 §7).

    Only 1.0 means ready, and it is written in the same transaction as `status = 'ready'`, so
    the UI can never see a finished bar above an unfinished document.
    """
    fraction = 0.0 if total <= 0 else embedded / total
    clamped = max(0.0, min(0.99, fraction))
    await session.execute(
        update(Document).where(Document.id == document_id).values(progress=clamped)
    )


async def commit_chunks(
    session: AsyncSession,
    job_id: uuid.UUID,
    document_id: uuid.UUID,
    candidates: Sequence[ChunkCandidate],
    vectors: Sequence[Sequence[float]],
    page_count: int | None,
) -> bool:
    """Replace this document's chunks and mark it ready — all in one transaction.

    This is Contract 3 §5. The delete and the insert share a transaction, so a job that runs
    twice (crash reclaim, retry) leaves exactly the database a job that ran once would: same
    ordinals, same offsets, same text. Only the chunk *ids* differ, which is why citations are
    resolved at answer time and never cached across a re-index.

    Returns False if the document was deleted underneath us, in which case nothing is written.
    """
    if len(candidates) != len(vectors):
        raise ValueError("every chunk must have exactly one vector")

    exists = await session.execute(
        select(Document.id).where(Document.id == document_id).with_for_update()
    )
    if exists.first() is None:
        return False

    await session.execute(delete(Chunk).where(Chunk.document_id == document_id))

    if candidates:
        await session.execute(
            insert(Chunk),
            [
                {
                    "document_id": document_id,
                    "ordinal": candidate.ordinal,
                    "text": candidate.text,
                    "token_count": candidate.token_count,
                    "page_start": candidate.page_start,
                    "page_end": candidate.page_end,
                    "char_start": candidate.char_start,
                    "char_end": candidate.char_end,
                    "heading_path": candidate.heading_path,
                    "embedding": list(vector),
                }
                for candidate, vector in zip(candidates, vectors)
            ],
        )

    await session.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(
            status=DocumentStatus.READY,
            progress=1.0,
            chunk_count=len(candidates),
            page_count=page_count,
            error_message=None,
            indexed_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(
            state=JobState.DONE,
            last_error=None,
            locked_at=None,
            updated_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    return True


async def fail_document(
    session: AsyncSession,
    job_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    user_message: str,
    detail: str | None = None,
) -> None:
    """Terminal failure: `documents.status = 'failed'` with copy the user will actually read.

    Contract 3 §4: a failed document *must* carry a non-null, human-readable
    `error_message`, because Instance 3 renders that string verbatim. It is user-facing copy,
    not a traceback — and Contract 9 forbids it carrying a stack trace, SQL, a provider
    payload or any fragment of a key.
    """
    await session.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(status=DocumentStatus.FAILED, error_message=user_message, progress=0.0)
    )
    await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(
            state=JobState.FAILED,
            last_error=(detail or user_message)[:1000],
            locked_at=None,
            updated_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    logger.warning("document failed %s", kv(document=str(document_id), reason=detail))


async def requeue_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    detail: str | None = None,
    reset_document: bool = True,
) -> None:
    """Hand the job back to the queue for another attempt.

    The document returns to `pending` so the library shows it as waiting rather than stuck
    half-processed — a legal transition, since `processing -> pending` only ever happens here
    and on re-index, and progress resets to 0.0 with it (Contract 3 §7).
    """
    if reset_document:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status=DocumentStatus.PENDING, progress=0.0)
        )
    await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(
            state=JobState.QUEUED,
            locked_at=None,
            locked_by=None,
            last_error=(detail or "")[:1000] or None,
            updated_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    logger.info("job requeued %s", kv(document=str(document_id), reason=detail))
