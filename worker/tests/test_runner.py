"""Startup guards, the poll loop, and what happens when Contract 4 breaks."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import text

from queryll_worker.config import ChunkingSettings, ConfigError, Settings
from queryll_worker.embeddings import FakeEmbedder
from queryll_worker.errors import EmbeddingContractError
from queryll_worker.models import SCHEMA_EMBEDDING_DIM
from queryll_worker.runner import (
    EmbeddingContractFailure,
    ShutdownSignal,
    run_forever,
    validate_runtime,
)
from tests.conftest import fixture_bytes


class BrokenEmbedder(FakeEmbedder):
    """An embedder whose vectors violate Contract 4 — the failure this project fears most."""

    async def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        raise EmbeddingContractError("expected 1024 dims, got 768")


# --- startup guards ---------------------------------------------------------------------


def test_a_dimension_that_disagrees_with_the_schema_is_refused(settings: Settings) -> None:
    """`vector(1024)` is frozen in the DDL; changing the dimension is an escalation."""
    with pytest.raises(ConfigError, match="ESCALATION"):
        validate_runtime(replace(settings, embedding_dim=768))


def test_the_ratified_dimension_starts_cleanly(settings: Settings) -> None:
    validate_runtime(replace(settings, embedding_dim=SCHEMA_EMBEDDING_DIM))


def test_the_fake_provider_warns_loudly(settings: Settings, caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level("WARNING"):
        validate_runtime(replace(settings, embedding_provider="fake"))
    assert "never run this in production" in caplog.text.lower()


def test_chunking_settings_reject_impossible_overlap() -> None:
    with pytest.raises(ConfigError):
        ChunkingSettings(target_tokens=512, overlap_tokens=512)


def test_settings_reject_a_non_asyncpg_url() -> None:
    with pytest.raises(ConfigError, match="asyncpg"):
        Settings(
            database_url="postgresql://queryll:queryll@localhost:5433/queryll",
            voyage_api_key="k",
        )


def test_settings_reject_an_oversized_batch() -> None:
    with pytest.raises(ConfigError, match="EMBEDDING_BATCH_SIZE"):
        Settings(
            database_url="postgresql+asyncpg://x/y",
            voyage_api_key="k",
            embedding_batch_size=1000,
        )


# --- the loop ----------------------------------------------------------------------------


async def test_the_loop_drains_the_queue_then_stops(
    session_factory, enqueue, settings: Settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    document_id, _ = await enqueue(
        fixture_bytes("ingestion_notes.md"),
        filename="notes.md",
        mime_type="text/markdown",
    )
    shutdown = ShutdownSignal()

    import queryll_worker.runner as runner_module

    monkeypatch.setattr(runner_module, "build_embedder", lambda _s: FakeEmbedder())
    monkeypatch.setattr(runner_module, "create_session_factory", lambda _e: session_factory)

    original = runner_module.run_job

    async def stop_after_one(job, **kwargs):  # type: ignore[no-untyped-def]
        outcome = await original(job, **kwargs)
        shutdown.request()
        return outcome

    monkeypatch.setattr(runner_module, "run_job", stop_after_one)

    exit_code = await run_forever(settings, shutdown)
    assert exit_code == 0

    async with session_factory() as session:
        result = await session.execute(
            text("SELECT status::text FROM documents WHERE id = :id"), {"id": document_id}
        )
    assert result.scalar() == "ready"


async def test_an_idle_queue_does_not_spin(
    session_factory, settings: Settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """With nothing queued the loop sleeps and exits cleanly on the shutdown flag."""
    import queryll_worker.runner as runner_module

    monkeypatch.setattr(runner_module, "build_embedder", lambda _s: FakeEmbedder())
    monkeypatch.setattr(runner_module, "create_session_factory", lambda _e: session_factory)

    shutdown = ShutdownSignal()
    sleeps: list[float] = []
    original_sleep = runner_module.asyncio.sleep

    async def counting_sleep(delay: float) -> None:
        sleeps.append(delay)
        shutdown.request()
        await original_sleep(0)

    monkeypatch.setattr(runner_module.asyncio, "sleep", counting_sleep)

    assert await run_forever(settings, shutdown) == 0
    assert sleeps == [settings.poll_interval_seconds]


async def test_a_contract_4_violation_halts_the_worker_and_frees_the_job(
    session_factory, enqueue, settings: Settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A wrong-shaped vector is not a bad document — it is the contract being broken.

    Grinding on would fail every document in the queue one at a time with no explanation. A
    non-zero exit is loud, and the job goes back on the queue untouched so nothing is lost.
    """
    document_id, job_id = await enqueue(
        fixture_bytes("ingestion_notes.md"),
        filename="notes.md",
        mime_type="text/markdown",
    )

    import queryll_worker.runner as runner_module

    monkeypatch.setattr(runner_module, "build_embedder", lambda _s: BrokenEmbedder())
    monkeypatch.setattr(runner_module, "create_session_factory", lambda _e: session_factory)

    exit_code = await run_forever(settings, ShutdownSignal())
    assert exit_code == 1

    async with session_factory() as session:
        job = await session.execute(
            text(
                "SELECT state::text AS state, last_error FROM ingestion_jobs WHERE id = :id"
            ),
            {"id": job_id},
        )
        job_row = job.mappings().one()
        document = await session.execute(
            text("SELECT status::text FROM documents WHERE id = :id"), {"id": document_id}
        )

    assert job_row["state"] == "queued"
    assert "embedding contract" in job_row["last_error"]
    # The document is waiting, not failed: nothing was wrong with it.
    assert document.scalar() == "pending"


def test_the_contract_failure_is_its_own_exception_type() -> None:
    assert issubclass(EmbeddingContractFailure, RuntimeError)
