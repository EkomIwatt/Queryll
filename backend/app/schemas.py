"""Pydantic v2 schemas -- the exact wire shapes of Contracts 1, 6, 7, 8 and 9.

Instance 3 hand-wrote its TypeScript types from the same contract text without ever
seeing this file. These models are therefore the producer side of a text contract, not
an implementation detail: a field renamed here is a broken frontend at merge.

Two project-wide conventions are enforced structurally rather than by convention:

  * Timestamps are ISO-8601 UTC with a trailing `Z` -- `UtcTimestamp` below, not the
    `+00:00` that `datetime.isoformat()` produces by default.
  * Ids cross the wire as strings, so a `model_dump()` is always JSON-clean and an SSE
    frame can never carry a `UUID` object into `json.dumps`.
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, PlainSerializer

DocumentStatus = str  # "pending" | "processing" | "ready" | "failed"


def _to_iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_str_id(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


UtcTimestamp = Annotated[datetime, PlainSerializer(_to_iso_z, return_type=str)]
StrId = Annotated[str, BeforeValidator(_to_str_id)]


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Contract 9 -- the error envelope
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    error: str


# ---------------------------------------------------------------------------
# Contract 1 -- authentication
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(Base):
    id: StrId
    email: str
    display_name: str
    created_at: UtcTimestamp


class AuthResponse(BaseModel):
    access_token: str
    user: UserOut


# ---------------------------------------------------------------------------
# Contract 6 -- documents
# ---------------------------------------------------------------------------


class DocumentOut(Base):
    id: StrId
    filename: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    progress: float
    page_count: Optional[int] = None
    chunk_count: Optional[int] = None
    # Non-null iff status == "failed". Rendered verbatim by the UI, so it is written by
    # the worker as user-facing copy -- never a traceback.
    error_message: Optional[str] = None
    created_at: UtcTimestamp
    indexed_at: Optional[UtcTimestamp] = None


class DocumentListResponse(BaseModel):
    documents: List[DocumentOut]
    next_cursor: Optional[str] = None


class ChunkSummaryOut(Base):
    id: StrId
    ordinal: int
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    heading_path: Optional[str] = None
    preview: str  # first 200 characters of the chunk text


class ChunkOut(ChunkSummaryOut):
    text: str
    token_count: int


class ChunkListResponse(BaseModel):
    chunks: List[ChunkSummaryOut]
    next_cursor: Optional[str] = None


class ChunkDetailResponse(BaseModel):
    """The citation viewer needs the passage IN CONTEXT, so neighbours come with it."""

    chunk: ChunkOut
    prev: Optional[ChunkOut] = None
    next: Optional[ChunkOut] = None


# ---------------------------------------------------------------------------
# Contract 8 -- the Citation object
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    index: int  # 1-based; the [n] in the answer text. Per-answer, not global.
    chunk_id: StrId  # the stable handle -- resolve passages by this, never by index
    document_id: StrId
    filename: str
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    heading_path: Optional[str] = None
    similarity: float  # 0..1, rounded to 3dp
    snippet: str  # <= 300 chars, for the inline hover card only


# ---------------------------------------------------------------------------
# Contract 7 -- conversations, messages and the streamed answer
# ---------------------------------------------------------------------------


class ConversationCreateRequest(BaseModel):
    title: Optional[str] = None


class ConversationOut(Base):
    id: StrId
    title: Optional[str] = None
    created_at: UtcTimestamp


class ConversationListResponse(BaseModel):
    conversations: List[ConversationOut]


class MessageOut(Base):
    id: StrId
    role: str  # "user" | "assistant"
    content: str
    citations: List[Citation] = []
    created_at: UtcTimestamp


class ConversationDetailResponse(BaseModel):
    conversation: ConversationOut
    messages: List[MessageOut]


class AskRequest(BaseModel):
    question: str
    # null means "search all of the caller's documents".
    document_ids: Optional[List[StrId]] = None


# -- SSE frame payloads (Contract 7 §3). Every `data` is a single-line JSON object.


class RetrievalEvent(BaseModel):
    sources: List[Citation]
    insufficient_context: bool


class TokenEvent(BaseModel):
    text: str


class DoneEvent(BaseModel):
    message_id: StrId
    citations_used: List[int]  # 1-based indices actually referenced in the final text


class StreamErrorEvent(BaseModel):
    error: str
    code: str


def error_envelope(message: str) -> Dict[str, str]:
    return {"error": message}
