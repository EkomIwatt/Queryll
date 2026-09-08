"""Test fixtures.

Two ground rules this suite is built around, both from the contracts:

* **Real Postgres with pgvector, for anything touching SQL.** SQLite cannot represent a
  `vector` column, so there is no in-memory shortcut in this project. Start the database
  with `docker compose up -d db` from the repository root; it listens on 5433, not 5432.

* **No test calls the live Voyage or Anthropic API.** Ever. Queries are embedded with the
  deterministic fake embedder and answers come from a scripted client.

The suite runs against its own database, `queryll_test`, which is dropped and recreated
from `db/init.sql` at the start of every session. That keeps the developer's `queryll`
database untouched, and it means the ratified DDL is exercised on every single run --
if Contract 2 and these models ever drift apart, the suite stops rather than passing
against a schema someone hand-patched months ago.
"""

import os
import pathlib
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, List, Optional

import asyncpg
import pytest

# Environment must be set before anything imports `app.config`, which is cached, and
# before `app.models`, which reads EMBEDDING_DIM at class-definition time.
TEST_DB_NAME = "queryll_test"
_ADMIN_DSN = os.environ.get(
    "QUERYLL_TEST_ADMIN_DSN", "postgresql://queryll:queryll@localhost:5433/postgres"
)
_TEST_DSN = _ADMIN_DSN.rsplit("/", 1)[0] + "/" + TEST_DB_NAME

os.environ["DATABASE_URL"] = _TEST_DSN.replace(
    "postgresql://", "postgresql+asyncpg://", 1
)
os.environ.setdefault("JWT_SECRET", "test-secret-not-used-anywhere-real")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("COOKIE_SAMESITE", "lax")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")
# Neither key is read by any test; they are unset so that a stray real call would fail
# loudly rather than quietly spending someone's quota.
os.environ.pop("VOYAGE_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.answer.claude_client import ScriptedAnswerClient  # noqa: E402
from app.database import dispose_engine, get_sessionmaker  # noqa: E402
from app.embeddings.fake import FakeEmbeddingClient, fake_embedding  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Chunk, Conversation, Document, User  # noqa: E402
from app.security import create_access_token, hash_password  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INIT_SQL = REPO_ROOT / "db" / "init.sql"

# Truncating `users` reaches everything else through ON DELETE CASCADE, so one statement
# resets the world between tests.
TRUNCATE = "TRUNCATE TABLE users RESTART IDENTITY CASCADE"


@pytest.fixture(scope="session", autouse=True)
async def _database() -> AsyncIterator[None]:
    admin = await asyncpg.connect(_ADMIN_DSN)
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            TEST_DB_NAME,
        )
        await admin.execute('DROP DATABASE IF EXISTS "' + TEST_DB_NAME + '"')
        await admin.execute('CREATE DATABASE "' + TEST_DB_NAME + '"')
    finally:
        await admin.close()

    ddl = INIT_SQL.read_text(encoding="utf-8")
    connection = await asyncpg.connect(_TEST_DSN)
    try:
        await connection.execute(ddl)
    finally:
        await connection.close()

    yield

    await dispose_engine()


@pytest.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    async with get_sessionmaker()() as session:
        await session.execute(text(TRUNCATE))
        await session.commit()
    yield


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def embedder() -> FakeEmbeddingClient:
    return FakeEmbeddingClient()


@pytest.fixture
def answer_client() -> ScriptedAnswerClient:
    return ScriptedAnswerClient(["A grounded answer ", "[1]", "."])


@pytest.fixture
async def app(embedder, answer_client):
    application = create_app()
    # The lifespan builds the REAL Voyage and Anthropic clients, so it is not run here;
    # the two clients it would have created are injected directly instead.
    application.state.embedding_client = embedder
    application.state.answer_client = answer_client
    return application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


async def make_user(db: AsyncSession, email: Optional[str] = None) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email or (uuid.uuid4().hex + "@example.com"),
        password_hash=hash_password("correct horse battery"),
        display_name="Test User",
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def make_document(
    db: AsyncSession,
    user: User,
    *,
    filename: str = "notes.txt",
    status: str = "ready",
    mime_type: str = "text/plain",
    content: bytes = b"seed",
) -> Document:
    document = Document(
        id=uuid.uuid4(),
        user_id=user.id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(content),
        content=content,
        status=status,
        progress=1.0 if status == "ready" else 0.0,
        page_count=None,
        chunk_count=None,
        error_message=(
            "This PDF is password-protected and could not be read."
            if status == "failed"
            else None
        ),
        created_at=datetime.now(timezone.utc),
        indexed_at=datetime.now(timezone.utc) if status == "ready" else None,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    return document


async def make_chunks(
    db: AsyncSession,
    document: Document,
    texts: List[str],
    *,
    pages: Optional[List[Optional[int]]] = None,
    heading_path: Optional[str] = None,
) -> List[Chunk]:
    """Seed chunk rows with fixture vectors from the SAME fake embedder Instance 1 uses.

    The worker never has to run for these tests: retrieval is exercised against rows
    seeded here, exactly as the decomposition intended.
    """
    rows: List[Chunk] = []
    offset = 0
    for ordinal, body in enumerate(texts):
        page = pages[ordinal] if pages and ordinal < len(pages) else None
        chunk = Chunk(
            id=uuid.uuid4(),
            document_id=document.id,
            ordinal=ordinal,
            text=body,
            token_count=max(1, len(body.split())),
            page_start=page,
            page_end=page,
            char_start=offset,
            char_end=offset + len(body),
            heading_path=heading_path,
            embedding=fake_embedding(body),
            created_at=datetime.now(timezone.utc),
        )
        offset += len(body) + 1
        db.add(chunk)
        rows.append(chunk)
    await db.commit()
    return rows


async def make_conversation(db: AsyncSession, user: User, title=None) -> Conversation:
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=user.id,
        title=title,
        created_at=datetime.now(timezone.utc),
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


def auth_headers(user: User) -> dict:
    return {"Authorization": "Bearer " + create_access_token(user.id)}
