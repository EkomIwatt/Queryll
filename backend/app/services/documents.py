"""Document lifecycle operations (Contracts 3 and 6).

This module is where Contract 3's column ownership is actually enforced, so it is worth
being explicit about what it may and may not write:

* `create_document` inserts the `documents` row AND its `ingestion_jobs` row in the SAME
  transaction (Contract 3 §2). A committed document without a job is an unreachable
  state, not a race to paper over -- so it is made unreachable by the transaction rather
  than repaired later by a sweeper.

* `reindex_document` is THE ONE RATIFIED EXCEPTION (Contract 3 §1) in which this instance
  writes worker-owned columns. It is legal only from `ready` or `failed`, never from
  `processing`, and everything it does happens in one transaction.

* Nothing else in this codebase writes `documents.status`, `progress`, `page_count`,
  `chunk_count`, `error_message` or `indexed_at`, and nothing writes `chunks` at all.
  If you find yourself wanting to nudge a status to unstick something, you have found an
  escalation, not a fix.

Ownership failures return 404, never 403 (Contract 9). Ownership must not be discoverable
by probing ids, so "someone else's document" and "no such document" are indistinguishable
from outside.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, NotFoundError
from app.models import Chunk, Document, IngestionJob
from app.pagination import decode_cursor, encode_cursor

# Contract 3 §4: nothing leaves `ready` except by re-index or deletion, and a re-index is
# legal only from a terminal state -- never while the worker holds the document.
REINDEXABLE_STATUSES = ("ready", "failed")


async def create_document(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    filename: str,
    mime_type: str,
    content: bytes,
) -> Document:
    """Insert the document and enqueue its job in one transaction (Contract 3 §2).

    The row is created at `status='pending'`, `progress=0.0`, with `page_count` and
    `chunk_count` NULL: nothing has been parsed yet, and Contract 6 §1 requires the 202
    to be returned before any parsing, embedding or page counting happens.
    """
    now = datetime.now(timezone.utc)
    document = Document(
        id=uuid.uuid4(),
        user_id=user_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(content),
        content=content,
        status="pending",
        progress=0.0,
        page_count=None,
        chunk_count=None,
        error_message=None,
        created_at=now,
        indexed_at=None,
    )
    job = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        state="queued",
        attempts=0,
        created_at=now,
        updated_at=now,
    )
    db.add(document)
    db.add(job)
    await db.commit()
    await db.refresh(document)
    return document


async def get_owned_document(
    db: AsyncSession, *, user_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    document = (
        await db.execute(
            select(Document).where(
                Document.id == document_id, Document.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFoundError("That document could not be found.")
    return document


async def list_documents(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int,
    cursor: Optional[str] = None,
) -> Tuple[List[Document], Optional[str]]:
    """Newest first, keyset-paginated. Only the caller's own documents, ever."""
    statement = select(Document).where(Document.user_id == user_id)

    position = decode_cursor(cursor)
    if position is not None:
        after_created = _parse_cursor_timestamp(position.get("created_at"))
        after_id = _parse_cursor_uuid(position.get("id"))
        if after_created is not None and after_id is not None:
            # Strict keyset: (created_at, id) < (cursor_created_at, cursor_id) DESC.
            statement = statement.where(
                (Document.created_at < after_created)
                | (
                    (Document.created_at == after_created)
                    & (Document.id < after_id)
                )
            )

    statement = statement.order_by(
        Document.created_at.desc(), Document.id.desc()
    ).limit(limit + 1)

    rows = list((await db.execute(statement)).scalars().all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(
            {"created_at": last.created_at.isoformat(), "id": str(last.id)}
        )
    return rows, next_cursor


async def reindex_document(
    db: AsyncSession, *, user_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    """THE ONE RATIFIED EXCEPTION to Contract 3 §1 column ownership.

    In a single transaction: reset the document to `pending` with `progress = 0.0` and
    `error_message = NULL`, delete that document's chunks, and insert a fresh job row.
    Legal only when the current status is `ready` or `failed` -- never `processing`,
    because that would race a worker that currently holds the document and is about to
    write chunks for it.

    Chunk ids change on a re-index, which is why citations are resolved at answer time
    and never cached across one (Contract 3 §5).
    """
    document = await get_owned_document(db, user_id=user_id, document_id=document_id)
    if document.status not in REINDEXABLE_STATUSES:
        raise ConflictError(
            "That document is still being processed. Try again when it has finished."
        )

    now = datetime.now(timezone.utc)
    await db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    await db.execute(
        update(Document)
        .where(Document.id == document.id)
        .values(
            status="pending",
            progress=0.0,
            error_message=None,
            chunk_count=None,
            indexed_at=None,
        )
    )
    db.add(
        IngestionJob(
            id=uuid.uuid4(),
            document_id=document.id,
            state="queued",
            attempts=0,
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()

    await db.refresh(document)
    return document


async def delete_document(
    db: AsyncSession, *, user_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Cascades to chunks. Refused while `processing` (Contract 6 §6).

    A delete racing a running worker is refused rather than resolved: the worker would
    otherwise be mid-transaction writing chunks for a document that no longer exists.

    Past conversation messages that cite this document are deliberately NOT touched.
    Their citations become dangling, and Contract 8 §3 governs how the UI renders them --
    a historical answer keeps showing what it was based on.
    """
    document = await get_owned_document(db, user_id=user_id, document_id=document_id)
    if document.status == "processing":
        raise ConflictError(
            "That document is being processed right now and cannot be deleted yet."
        )
    await db.execute(delete(Document).where(Document.id == document.id))
    await db.commit()


# ---------------------------------------------------------------------------
# Chunks -- read-only here; every row was written by Instance 1.
# ---------------------------------------------------------------------------


async def list_chunks(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    limit: int,
    cursor: Optional[str] = None,
) -> Tuple[List[Chunk], Optional[str]]:
    await get_owned_document(db, user_id=user_id, document_id=document_id)

    statement = select(Chunk).where(Chunk.document_id == document_id)
    position = decode_cursor(cursor)
    if position is not None and isinstance(position.get("ordinal"), int):
        statement = statement.where(Chunk.ordinal > position["ordinal"])

    statement = statement.order_by(Chunk.ordinal.asc()).limit(limit + 1)
    rows = list((await db.execute(statement)).scalars().all())

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor({"ordinal": rows[-1].ordinal})
    return rows, next_cursor


async def get_chunk_with_neighbours(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
    chunk_id: uuid.UUID,
) -> Tuple[Chunk, Optional[Chunk], Optional[Chunk]]:
    """The passage IN CONTEXT: the chunk plus its immediate neighbours.

    Neighbours are found by ordinal, which is contiguous and 0-based within a document.
    The chunk itself is still addressed by id -- `chunk.id` is the citation primary key,
    and resolving a citation by ordinal or page would be resolving it by display metadata.
    """
    await get_owned_document(db, user_id=user_id, document_id=document_id)

    chunk = (
        await db.execute(
            select(Chunk).where(Chunk.id == chunk_id, Chunk.document_id == document_id)
        )
    ).scalar_one_or_none()
    if chunk is None:
        raise NotFoundError("That passage could not be found.")

    previous = (
        await db.execute(
            select(Chunk)
            .where(
                Chunk.document_id == document_id, Chunk.ordinal < chunk.ordinal
            )
            .order_by(Chunk.ordinal.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    following = (
        await db.execute(
            select(Chunk)
            .where(
                Chunk.document_id == document_id, Chunk.ordinal > chunk.ordinal
            )
            .order_by(Chunk.ordinal.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return chunk, previous, following


async def accessible_document_ids(
    db: AsyncSession, *, user_id: uuid.UUID, document_ids: Sequence[uuid.UUID]
) -> List[uuid.UUID]:
    """Filter a client-supplied id list down to documents the caller actually owns.

    Retrieval is scoped in SQL regardless, so this is belt-and-braces -- but it means an
    `ask` naming another user's document is answered from the caller's own documents
    rather than silently including a scope the caller cannot see.
    """
    if not document_ids:
        return []
    rows = (
        await db.execute(
            select(Document.id).where(
                Document.user_id == user_id, Document.id.in_(list(document_ids))
            )
        )
    ).scalars()
    return list(rows)


def _parse_cursor_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_cursor_uuid(value: Any) -> Optional[uuid.UUID]:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def as_dict(document: Document) -> Dict[str, Any]:
    """Only for logging. `content` is never included -- file bytes are never logged."""
    return {
        "id": str(document.id),
        "status": document.status,
        "mime_type": document.mime_type,
        "size_bytes": document.size_bytes,
    }
