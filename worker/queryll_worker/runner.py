"""The poll loop.

One process, one job at a time, claiming rows from Postgres. It is allowed to be killed at
any moment: a job it was part-way through stays `running`, the fifteen-minute reclaim window
in Contract 3 §3 hands it back, and idempotent ingestion (§5) makes the re-run land on the
same database the first run would have. That is the whole payoff for choosing a durable queue
over `BackgroundTasks`, and it is why nothing here tries to be clever about cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import dataclass

from queryll_worker import queue
from queryll_worker.config import ConfigError, Settings
from queryll_worker.db import (
    check_connectivity,
    create_engine,
    create_session_factory,
    session_scope,
)
from queryll_worker.embeddings import build_embedder
from queryll_worker.errors import EmbeddingContractError
from queryll_worker.logging_setup import kv
from queryll_worker.models import SCHEMA_EMBEDDING_DIM
from queryll_worker.pipeline import run_job

logger = logging.getLogger(__name__)


class EmbeddingContractFailure(RuntimeError):
    """Raised out of the loop when Contract 4 is violated. The process exits non-zero.

    Deliberately fatal. A wrong-shaped vector means this worker's embedding configuration
    disagrees with the schema — and, almost certainly, with the API process. Grinding on and
    failing every document one by one would fill the library with unexplained failures; a
    crash-loop is loud, and loud is the point.
    """


@dataclass
class ShutdownSignal:
    """Cooperative stop flag, set by SIGTERM/SIGINT and checked between jobs."""

    requested: bool = False

    def request(self) -> None:
        self.requested = True


def validate_runtime(settings: Settings) -> None:
    """Refuse to start on a configuration that cannot possibly work.

    The dimension check is the cheap half of Contract 4: if `EMBEDDING_DIM` disagrees with
    `vector(1024)` in the ratified DDL, every insert would fail at the database anyway — far
    better to say so at boot than one claimed job later.
    """
    if settings.embedding_dim != SCHEMA_EMBEDDING_DIM:
        raise ConfigError(
            f"EMBEDDING_DIM is {settings.embedding_dim} but db/init.sql declares "
            f"vector({SCHEMA_EMBEDDING_DIM}). Changing the dimension is a schema change, "
            "which is an ESCALATION, not a config edit."
        )
    if settings.embedding_provider == "fake":
        logger.warning(
            "EMBEDDING_PROVIDER=fake — vectors are deterministic noise and retrieval over "
            "them is meaningless. Never run this in production."
        )


def install_signal_handlers(shutdown: ShutdownSignal) -> None:
    """Ask the loop to stop after the current job.

    `loop.add_signal_handler` is POSIX-only, so this falls back to `signal.signal`, which is
    what makes the worker developable on Windows and deployable on Render's Linux containers
    without a second code path.
    """
    def _handle(signum: int, _frame: object) -> None:
        logger.info("shutdown requested %s", kv(signal=signum))
        shutdown.request()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        signal_number = getattr(signal, name, None)
        if signal_number is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal_number, _handle)


async def run_forever(settings: Settings, shutdown: ShutdownSignal | None = None) -> int:
    """Claim and process jobs until asked to stop. Returns the process exit code."""
    shutdown = shutdown or ShutdownSignal()
    validate_runtime(settings)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    embedder = build_embedder(settings)

    await check_connectivity(engine)
    logger.info(
        "worker started %s",
        kv(
            worker=settings.worker_id,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            provider=settings.embedding_provider,
        ),
    )

    exit_code = 0
    last_idle_log = 0.0
    try:
        while not shutdown.requested:
            async with session_scope(session_factory) as session:
                job = await queue.claim_next_job(session, settings.worker_id)

            if job is None:
                now = time.monotonic()
                if now - last_idle_log >= settings.idle_log_interval_seconds:
                    logger.info("queue empty %s", kv(worker=settings.worker_id))
                    last_idle_log = now
                await asyncio.sleep(settings.poll_interval_seconds)
                continue

            last_idle_log = 0.0
            logger.info(
                "job claimed %s",
                kv(document=str(job.document_id), attempt=job.attempts),
            )
            try:
                outcome = await run_job(
                    job,
                    session_factory=session_factory,
                    settings=settings,
                    embedder=embedder,
                )
            except EmbeddingContractError as exc:
                logger.critical(
                    "CONTRACT 4 VIOLATION — refusing to store vectors %s",
                    kv(document=str(job.document_id), reason=str(exc)),
                )
                async with session_scope(session_factory) as session:
                    await queue.requeue_job(
                        session,
                        job.id,
                        job.document_id,
                        detail="embedding contract violation; worker halted",
                    )
                raise EmbeddingContractFailure(str(exc)) from exc

            logger.info(
                "job finished %s",
                kv(document=str(job.document_id), outcome=outcome.status),
            )
    except EmbeddingContractFailure:
        exit_code = 1
    finally:
        await embedder.aclose()
        await engine.dispose()
        logger.info("worker stopped %s", kv(worker=settings.worker_id, exit_code=exit_code))

    return exit_code
