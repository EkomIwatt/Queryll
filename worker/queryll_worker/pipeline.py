"""One job, start to finish: bytes -> text -> chunks -> vectors -> rows.

The shape of this module is dictated by two things that pull in opposite directions.
Contract 3 §5 wants the chunk write to be a single atomic replace, so a job that runs twice
leaves the same database as a job that ran once. Contract 3 §7 wants progress to be visible
*during* the run, which means committing while the run is still in flight. So progress and
page count commit in their own short transactions, the chunks and the final status commit in
one, and nothing in between is half-written: until that last transaction lands, the document
is `processing` with no chunks, which is exactly what it is.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from queryll_worker import queue
from queryll_worker.chunking import ChunkCandidate, chunk_document
from queryll_worker.config import MAX_INPUTS_PER_REQUEST, Settings
from queryll_worker.db import session_scope
from queryll_worker.embeddings.base import Embedder
from queryll_worker.errors import (
    GENERIC_FAILURE_MESSAGE,
    PermanentIngestError,
    TransientIngestError,
)
from queryll_worker.logging_setup import kv
from queryll_worker.parsing import ExtractedDocument, extract

logger = logging.getLogger(__name__)

#: Aim for roughly this many progress updates across a document, so the bar moves often
#: enough to be believable without turning every ingest into a hundred tiny API calls.
_PROGRESS_STEPS = 8
_MIN_BATCH = 16


@dataclass(frozen=True)
class JobOutcome:
    """What happened to one job. Returned for logging and for tests to assert on."""

    status: str  # "ready" | "failed" | "requeued" | "vanished"
    chunk_count: int = 0
    page_count: int | None = None
    detail: str | None = None


def batch_size_for(total: int, configured: int) -> int:
    """Choose an embedding batch size for a document of `total` chunks.

    Never above the Contract 4 ceiling of 128 inputs per request, and never so large that a
    forty-second ingest reports nothing until it is over.
    """
    ceiling = min(configured, MAX_INPUTS_PER_REQUEST)
    if total <= 0:
        return ceiling
    stepped = max(_MIN_BATCH, math.ceil(total / _PROGRESS_STEPS))
    return max(1, min(ceiling, stepped))


async def run_job(
    job: queue.ClaimedJob,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
) -> JobOutcome:
    """Ingest one claimed document, translating every failure into the right end state.

    `EmbeddingContractError` is deliberately *not* caught: a wrong-shaped vector is not a bad
    document, it is the two-sided embedding contract being broken, and the runner escalates it
    rather than quietly failing the document that happened to be next in the queue.
    """
    try:
        return await _ingest(
            job, session_factory=session_factory, settings=settings, embedder=embedder
        )
    except PermanentIngestError as exc:
        async with session_scope(session_factory) as session:
            await queue.fail_document(
                session,
                job.id,
                job.document_id,
                user_message=exc.user_message,
                detail=exc.detail,
            )
        return JobOutcome(status="failed", detail=exc.detail or exc.user_message)
    except TransientIngestError as exc:
        return await _handle_transient(
            job, session_factory, settings, exc.user_message, exc.detail
        )
    except asyncio.CancelledError:
        # Shutdown mid-run. Leave the job `running`: the 15-minute reclaim window picks it up,
        # and idempotent ingestion makes re-running it safe.
        logger.warning("job cancelled mid-run %s", kv(document=str(job.document_id)))
        raise
    except Exception as exc:  # an unexpected bug should not poison the queue forever
        logger.exception("unexpected ingestion failure %s", kv(document=str(job.document_id)))
        return await _handle_transient(
            job,
            session_factory,
            settings,
            GENERIC_FAILURE_MESSAGE,
            f"unexpected {type(exc).__name__}",
        )


async def _handle_transient(
    job: queue.ClaimedJob,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_message: str,
    detail: str | None,
) -> JobOutcome:
    """Contract 3 §6: retry until `attempts >= 3`, then fail the document for good."""
    exhausted = job.attempts >= settings.max_attempts
    async with session_scope(session_factory) as session:
        if exhausted:
            await queue.fail_document(
                session,
                job.id,
                job.document_id,
                user_message=user_message,
                detail=detail,
            )
        else:
            await queue.requeue_job(
                session, job.id, job.document_id, detail=detail
            )
    return JobOutcome(status="failed" if exhausted else "requeued", detail=detail)


async def _ingest(
    job: queue.ClaimedJob,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
) -> JobOutcome:
    async with session_scope(session_factory) as session:
        document = await queue.load_document(session, job.document_id)
        if document is None:
            # Deleted between the claim and now. `ON DELETE CASCADE` took the job row with it,
            # so there is nothing left to update.
            logger.info("document vanished before ingestion %s", kv(document=str(job.document_id)))
            return JobOutcome(status="vanished")
        await queue.begin_processing(session, job.document_id)

    if document.size_bytes > settings.max_file_bytes:
        raise PermanentIngestError(
            "This file is larger than the 20 MB limit, so it could not be indexed."
        )

    extracted, candidates = await asyncio.to_thread(
        _parse_and_chunk, document, settings
    )
    if not candidates:
        raise PermanentIngestError(
            "This document contains no readable text, so there was nothing to index."
        )

    async with session_scope(session_factory) as session:
        await queue.record_page_count(session, job.document_id, extracted.page_count)

    vectors = await _embed_all(
        candidates,
        embedder=embedder,
        settings=settings,
        session_factory=session_factory,
        document_id=job.document_id,
    )

    async with session_scope(session_factory) as session:
        written = await queue.commit_chunks(
            session,
            job.id,
            job.document_id,
            candidates,
            vectors,
            extracted.page_count,
        )
    if not written:
        logger.info(
            "document vanished before chunks were written %s",
            kv(document=str(job.document_id)),
        )
        return JobOutcome(status="vanished")

    logger.info(
        "document indexed %s",
        kv(
            document=str(job.document_id),
            chunks=len(candidates),
            pages=extracted.page_count,
            model=embedder.model,
        ),
    )
    return JobOutcome(
        status="ready", chunk_count=len(candidates), page_count=extracted.page_count
    )


def _parse_and_chunk(
    document: queue.DocumentInput, settings: Settings
) -> tuple[ExtractedDocument, list[ChunkCandidate]]:
    """Parsing and chunking are pure, CPU-bound and slow; they run off the event loop."""
    extracted = extract(
        document.content, document.mime_type, max_pages=settings.max_pages
    )
    return extracted, chunk_document(extracted, settings.chunking)


async def _embed_all(
    candidates: list[ChunkCandidate],
    *,
    embedder: Embedder,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    document_id: uuid.UUID,
) -> list[list[float]]:
    """Embed every chunk, publishing progress after each batch (Contract 3 §7)."""
    size = batch_size_for(len(candidates), settings.embedding_batch_size)
    vectors: list[list[float]] = []

    for start in range(0, len(candidates), size):
        batch = candidates[start : start + size]
        vectors.extend(await embedder.embed_documents([chunk.text for chunk in batch]))
        async with session_scope(session_factory) as session:
            await queue.record_progress(
                session, document_id, len(vectors), len(candidates)
            )

    return vectors
