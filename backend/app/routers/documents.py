"""The documents HTTP API (Contract 6).

All routes require `Authorization: Bearer <token>`. Every one of them is scoped to the
caller's own rows, and a document belonging to someone else returns 404, never 403.

Embeddings are never serialized to the client -- not in any route, ever. The response
schemas make that structural rather than a rule someone has to remember: `ChunkOut` has
no embedding field to populate.
"""

import uuid
from typing import Optional

from fastapi import APIRouter, File, Query, Response, UploadFile, status

from app.config import get_settings
from app.deps import CurrentUser, DbDep
from app.errors import PayloadTooLargeError, UnsupportedMediaTypeError, ValidationError
from app.mime import SNIFF_BYTES, sniff_mime_type
from app.models import Chunk
from app.pagination import clamp_limit
from app.retrieval.citations import preview_of
from app.schemas import (
    ChunkDetailResponse,
    ChunkListResponse,
    ChunkOut,
    ChunkSummaryOut,
    DocumentListResponse,
    DocumentOut,
)
from app.services import documents as documents_service

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _chunk_summary(chunk: Chunk) -> ChunkSummaryOut:
    return ChunkSummaryOut(
        id=str(chunk.id),
        ordinal=chunk.ordinal,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        heading_path=chunk.heading_path,
        preview=preview_of(chunk.text),
    )


def _chunk_detail(chunk: Chunk) -> ChunkOut:
    return ChunkOut(
        id=str(chunk.id),
        ordinal=chunk.ordinal,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        heading_path=chunk.heading_path,
        preview=preview_of(chunk.text),
        text=chunk.text,
        token_count=chunk.token_count,
    )


def _parse_uuid(value: str, message: str) -> uuid.UUID:
    """A malformed id is a 404, not a 422.

    The alternative tells an attacker which of their guesses were at least well-formed,
    which is the same information leak that 403-instead-of-404 would be.
    """
    from app.errors import NotFoundError

    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise NotFoundError(message)


async def _read_capped(upload: UploadFile) -> bytes:
    """Read the upload, aborting the moment it exceeds the cap.

    The cap is enforced DURING the read, not after it. Reading a hostile 2 GB body into
    memory and then measuring it is how a 413 becomes an outage.
    """
    settings = get_settings()
    limit = settings.max_upload_bytes
    parts = []
    total = 0
    while True:
        block = await upload.read(settings.upload_read_chunk_bytes)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise PayloadTooLargeError()
        parts.append(block)
    return b"".join(parts)


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentOut)
async def upload_document(
    user: CurrentUser,
    db: DbDep,
    file: UploadFile = File(...),
) -> DocumentOut:
    """202 with the document at `status: "pending"`.

    Returned BEFORE any parsing, embedding or page counting happens, so `page_count` and
    `chunk_count` are null here, always (Contract 6 §1). The worker picks the job up from
    the queue; this request does no ingestion work of its own.
    """
    content = await _read_capped(file)

    if not content:
        raise ValidationError("That file is empty.")

    # MIME is decided by SNIFFING the bytes. The declared Content-Type and the filename
    # are both caller-controlled; the extension is consulted only to tell Markdown from
    # plain text, which is cosmetic.
    filename = (file.filename or "document").strip() or "document"
    mime_type = sniff_mime_type(content[:SNIFF_BYTES], filename)
    if mime_type is None:
        raise UnsupportedMediaTypeError()

    document = await documents_service.create_document(
        db,
        user_id=user.id,
        filename=filename,
        mime_type=mime_type,
        content=content,
    )
    return DocumentOut.model_validate(document)


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    user: CurrentUser,
    db: DbDep,
    limit: Optional[int] = Query(default=None, ge=1),
    cursor: Optional[str] = Query(default=None),
) -> DocumentListResponse:
    settings = get_settings()
    rows, next_cursor = await documents_service.list_documents(
        db,
        user_id=user.id,
        limit=clamp_limit(
            limit, default=settings.default_page_limit, maximum=settings.max_page_limit
        ),
        cursor=cursor,
    )
    return DocumentListResponse(
        documents=[DocumentOut.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: str, user: CurrentUser, db: DbDep) -> DocumentOut:
    document = await documents_service.get_owned_document(
        db,
        user_id=user.id,
        document_id=_parse_uuid(document_id, "That document could not be found."),
    )
    return DocumentOut.model_validate(document)


@router.get("/{document_id}/chunks", response_model=ChunkListResponse)
async def list_chunks(
    document_id: str,
    user: CurrentUser,
    db: DbDep,
    limit: Optional[int] = Query(default=None, ge=1),
    cursor: Optional[str] = Query(default=None),
) -> ChunkListResponse:
    settings = get_settings()
    rows, next_cursor = await documents_service.list_chunks(
        db,
        user_id=user.id,
        document_id=_parse_uuid(document_id, "That document could not be found."),
        limit=clamp_limit(
            limit, default=settings.default_page_limit, maximum=settings.max_page_limit
        ),
        cursor=cursor,
    )
    return ChunkListResponse(
        chunks=[_chunk_summary(row) for row in rows], next_cursor=next_cursor
    )


@router.get("/{document_id}/chunks/{chunk_id}", response_model=ChunkDetailResponse)
async def get_chunk(
    document_id: str, chunk_id: str, user: CurrentUser, db: DbDep
) -> ChunkDetailResponse:
    """The passage plus its immediate neighbours, so the citation viewer can show it in
    context without fetching the whole document."""
    chunk, previous, following = await documents_service.get_chunk_with_neighbours(
        db,
        user_id=user.id,
        document_id=_parse_uuid(document_id, "That document could not be found."),
        chunk_id=_parse_uuid(chunk_id, "That passage could not be found."),
    )
    return ChunkDetailResponse(
        chunk=_chunk_detail(chunk),
        prev=_chunk_detail(previous) if previous is not None else None,
        next=_chunk_detail(following) if following is not None else None,
    )


@router.post(
    "/{document_id}/reindex",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentOut,
)
async def reindex_document(
    document_id: str, user: CurrentUser, db: DbDep
) -> DocumentOut:
    """202 with the document reset to `pending`; 409 if it is currently `processing`."""
    document = await documents_service.reindex_document(
        db,
        user_id=user.id,
        document_id=_parse_uuid(document_id, "That document could not be found."),
    )
    return DocumentOut.model_validate(document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: str, user: CurrentUser, db: DbDep) -> Response:
    await documents_service.delete_document(
        db,
        user_id=user.id,
        document_id=_parse_uuid(document_id, "That document could not be found."),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
