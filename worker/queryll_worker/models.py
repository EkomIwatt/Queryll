"""SQLAlchemy models for the three tables this worker touches.

`db/init.sql` (Contract 2) is the single source of truth for the schema. There is no Alembic
in this project and nothing here ever runs `create_all` against a real database — these
classes map the ratified DDL, they do not define it. A schema change is an ESCALATION.

Tables deliberately absent: `users`, `conversations`, `messages`. The worker never reads or
writes them, so it does not declare them. `documents.user_id` is likewise omitted: ownership
is Instance 2's concern and the worker has no business filtering on it.

Column ownership (Contract 3 §1) is annotated on every column below, because "reading another
instance's column is fine; writing it is a contract violation even when it would obviously
work" is the kind of rule that only survives if it is written where the code is.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, REAL, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: The `vector(N)` width frozen into `db/init.sql`. `Settings.embedding_dim` must equal this;
#: the worker refuses to start otherwise, because a mismatch is unstorable by construction.
SCHEMA_EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


class DocumentStatus(str, enum.Enum):
    """`document_status` in the DDL. Legal transitions are Contract 3 §4."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class JobState(str, enum.Enum):
    """`job_state` in the DDL."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _pg_enum(python_enum: type[enum.Enum], name: str) -> ENUM:
    """Map an existing Postgres enum type. `create_type=False`: the DDL already made it."""
    return ENUM(
        python_enum,
        name=name,
        create_type=False,
        values_callable=lambda e: [member.value for member in e],
    )


class Document(Base):
    """A user's uploaded file, and its ingestion lifecycle."""

    __tablename__ = "documents"

    # --- written by Instance 2 on insert; read-only here -------------------------------
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # --- written by Instance 1 (this process) -------------------------------------------
    status: Mapped[DocumentStatus] = mapped_column(
        _pg_enum(DocumentStatus, "document_status"), nullable=False
    )
    progress: Mapped[float] = mapped_column(REAL, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Chunk(Base):
    """One retrievable, citable passage. Every column is written by this process.

    `chunk.id` is the citation primary key for the whole product, and it is regenerated on
    every re-index — which is exactly why Contract 8 resolves citations at answer time and
    never caches a chunk id across an ingestion run.
    """

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "ordinal"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(SCHEMA_EMBEDDING_DIM), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IngestionJob(Base):
    """The durable queue row.

    Created by Instance 2 in the same transaction as its `documents` row (Contract 3 §2);
    every state column below is written only by this process. Claimed with the exact
    `FOR UPDATE SKIP LOCKED` statement in Contract 3 §3 — see `queue.py`.
    """

    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[JobState] = mapped_column(
        _pg_enum(JobState, "job_state"), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    locked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
