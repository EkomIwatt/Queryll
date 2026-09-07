"""Shared test fixtures.

**These tests run against real Postgres with pgvector.** There is no in-memory shortcut in
this project: SQLite cannot represent a `vector` column, so anything touching SQL runs against
the container from the repo-root `docker-compose.yml` (host port 5433).

The worker's tests create and use their **own database** on that server —
`queryll_worker_test` by default — rather than the shared `queryll` development database. The
container is shared infrastructure ratified on `main` and the API instance develops against it
too; a test suite that truncated its tables would be reaching outside its lane. The schema is
applied from the frozen `db/init.sql`, read, never edited.

Tests that need SQL are marked `postgres` and skip with a clear message when the container is
not running, so the parsing, chunking and embedding tests — which are the numerous, fast ones
— still run anywhere.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from queryll_worker.config import ChunkingSettings, Settings
from queryll_worker.db import create_engine, create_session_factory

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = REPO_ROOT / "db" / "init.sql"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL",
    "postgresql+asyncpg://queryll:queryll@localhost:5433/queryll",
)
TEST_DB_NAME = os.environ.get("TEST_DATABASE_NAME", "queryll_worker_test")
TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    f"postgresql+asyncpg://queryll:queryll@localhost:5433/{TEST_DB_NAME}",
)

#: Tables the worker touches, truncated between tests. `users` is Instance 2's table, but a
#: `documents` row needs an owner, so the fixtures seed one.
_TABLES = "chunks, ingestion_jobs, documents, users"


def fixture_path(name: str) -> Path:
    path = FIXTURES / name
    if not path.exists():  # pragma: no cover - regenerate and commit
        pytest.fail(f"missing fixture {name}; run `python tools/make_fixtures.py`")
    return path


def fixture_bytes(name: str) -> bytes:
    return fixture_path(name).read_bytes()


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings for tests: the fake embedder, and the frozen chunking targets."""
    return Settings(
        database_url=TEST_URL,
        voyage_api_key="",
        embedding_provider="fake",
        worker_id="test-worker",
        poll_interval_seconds=0.01,
        chunking=ChunkingSettings(),
    )


async def _database_is_available() -> str | None:
    """Prepare `queryll_worker_test` and return None, or a reason to skip."""
    engine = create_engine(ADMIN_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            existing = await connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            )
            if existing.first() is None:
                # CREATE DATABASE cannot run inside a transaction block.
                raw = await connection.get_raw_connection()
                await raw.driver_connection.execute(
                    f'CREATE DATABASE "{TEST_DB_NAME}"'
                )
    except Exception as exc:  # noqa: BLE001 - any failure means "no database"
        return f"pgvector database not reachable at {ADMIN_URL.rsplit('@', 1)[-1]}: {exc}"
    finally:
        await engine.dispose()

    engine = create_engine(TEST_URL)
    try:
        async with engine.begin() as connection:
            present = await connection.execute(
                text("SELECT to_regclass('public.chunks')")
            )
            if present.scalar() is None:
                await connection.execute(text(INIT_SQL.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        return f"could not apply db/init.sql to {TEST_DB_NAME}: {exc}"
    finally:
        await engine.dispose()
    return None


@pytest.fixture(scope="session")
def postgres_skip_reason() -> str | None:
    return asyncio.run(_database_is_available())


@pytest_asyncio.fixture
async def engine(postgres_skip_reason: str | None) -> AsyncIterator[object]:
    if postgres_skip_reason:
        pytest.skip(postgres_skip_reason)
    engine = create_engine(TEST_URL)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # type: ignore[no-untyped-def]
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    yield create_session_factory(engine)


@pytest_asyncio.fixture
async def user_id(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """Seed the owning user. `users` belongs to Instance 2; this is fixture data, not a write
    the worker ever performs in production."""
    identifier = uuid.uuid4()
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, password_hash, display_name) "
                "VALUES (:id, :email, 'x', 'Test User')"
            ),
            {"id": identifier, "email": f"{identifier}@example.test"},
        )
        await session.commit()
    return identifier


@pytest.fixture
def enqueue(session_factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Insert a document and its job the way Instance 2 does: one transaction (Contract 3 §2)."""

    async def _enqueue(
        content: bytes,
        *,
        filename: str = "fixture.txt",
        mime_type: str = "text/plain",
        status: str = "pending",
        with_job: bool = True,
    ) -> tuple[uuid.UUID, uuid.UUID | None]:
        document_id = uuid.uuid4()
        job_id = uuid.uuid4() if with_job else None
        async with session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO documents "
                    "(id, user_id, filename, mime_type, size_bytes, content, status) "
                    "VALUES (:id, :user_id, :filename, :mime, :size, :content, "
                    "CAST(:status AS document_status))"
                ),
                {
                    "id": document_id,
                    "user_id": user_id,
                    "filename": filename,
                    "mime": mime_type,
                    "size": len(content),
                    "content": content,
                    "status": status,
                },
            )
            if job_id is not None:
                await session.execute(
                    text(
                        "INSERT INTO ingestion_jobs (id, document_id) VALUES (:id, :doc)"
                    ),
                    {"id": job_id, "doc": document_id},
                )
            await session.commit()
        return document_id, job_id

    return _enqueue


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(items: Iterator[pytest.Item]) -> None:
    """Anything asking for a database connection is a `postgres` test."""
    for item in items:
        if {"engine", "session_factory", "enqueue", "user_id"} & set(
            getattr(item, "fixturenames", ())
        ):
            item.add_marker(pytest.mark.postgres)
