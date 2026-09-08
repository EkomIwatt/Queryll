"""SQLAlchemy models for ONLY the tables this instance touches.

These map `db/init.sql` (Contract 2) exactly. They are never used to create a schema --
`create_all` is not called anywhere in this project. Instance 1 declares its own models
for the tables it touches; the duplication is deliberate and bounded (CLAUDE.md,
Decomposition rationale), and it is safe precisely because neither instance owns the DDL.

COLUMN OWNERSHIP (Contract 3 §1) is not expressible in SQLAlchemy, so it is written
down here instead. This instance may write:

  documents      : id, user_id, filename, mime_type, size_bytes, content, created_at
                   -- INSERT only
  ingestion_jobs : row creation only
  chunks         : nothing -- read-only
  conversations  : everything
  messages       : everything

The single ratified exception is re-index (Contract 3 §1 / Contract 6 §5), where this
instance sets documents.status / progress / error_message and deletes that document's
chunks. That lives in exactly one function, and nowhere else.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings

DOCUMENT_STATUSES = ("pending", "processing", "ready", "failed")
JOB_STATES = ("queued", "running", "done", "failed")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Worker-owned columns below. Read freely; write only in the re-index exception.
    status: Mapped[str] = mapped_column(
        Enum(*DOCUMENT_STATUSES, name="document_status", create_type=False),
        nullable=False,
    )
    progress: Mapped[float] = mapped_column(Float, nullable=False)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    indexed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Chunk(Base):
    """Written exclusively by Instance 1. READ-ONLY here.

    `embedding` is mapped so retrieval can order by it. It is never serialized to a
    client -- not in any route, ever (Contract 6 §3).
    """

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "ordinal"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Deferred: an ORM load of a Chunk never fetches the vector. Every route that
    # returns chunks returns them without it, so not loading 1024 floats per row is
    # both the cheaper and the safer default -- retrieval selects the column explicitly
    # through Core and is unaffected.
    embedding: Mapped[List[float]] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=False, deferred=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class IngestionJob(Base):
    """Row creation is this instance's; every state column belongs to Instance 1.

    Mapped read-only here so a stuck-job admin view can exist later without a second
    schema declaration (Contract 3 §1).
    """

    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        Enum(*JOB_STATES, name="job_state", create_type=False), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="messages_role_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
