"""Conversations and the streamed answer (Contract 7).

`POST /api/conversations/{id}/ask` is the centrepiece. Read the ordering in `ask()`
carefully before changing it: everything that can fail with an HTTP status happens
before `StreamingResponse` is constructed, because once the headers are out the status
line is spent and every later failure has to go out as an in-stream `error` event.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select

from app.answer import service as answer_service
from app.database import get_sessionmaker
from app.deps import AnswerDep, CurrentUser, DbDep, EmbeddingDep
from app.errors import AnswerUnavailableError, NotFoundError, ValidationError
from app.models import Conversation, Message
from app.retrieval.service import retrieve
from app.schemas import (
    AskRequest,
    Citation,
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationOut,
    MessageOut,
)
from app.services import documents as documents_service
from app.sse import STREAM_HEADERS

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

MAX_QUESTION_CHARS = 4000


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise NotFoundError("That conversation could not be found.")


async def _get_owned_conversation(
    db: DbDep, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise NotFoundError("That conversation could not be found.")
    return conversation


def _message_out(message: Message) -> MessageOut:
    return MessageOut(
        id=str(message.id),
        role=message.role,
        content=message.content,
        citations=[Citation.model_validate(c) for c in (message.citations or [])],
        created_at=message.created_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ConversationOut)
async def create_conversation(
    payload: ConversationCreateRequest, user: CurrentUser, db: DbDep
) -> ConversationOut:
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=user.id,
        title=(payload.title or None),
        created_at=datetime.now(timezone.utc),
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return ConversationOut.model_validate(conversation)


@router.get("", response_model=ConversationListResponse)
async def list_conversations(user: CurrentUser, db: DbDep) -> ConversationListResponse:
    rows = (
        (
            await db.execute(
                select(Conversation)
                .where(Conversation.user_id == user.id)
                .order_by(Conversation.created_at.desc(), Conversation.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return ConversationListResponse(
        conversations=[ConversationOut.model_validate(row) for row in rows]
    )


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: str, user: CurrentUser, db: DbDep
) -> ConversationDetailResponse:
    conversation = await _get_owned_conversation(
        db, user.id, _parse_uuid(conversation_id)
    )
    rows = (
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return ConversationDetailResponse(
        conversation=ConversationOut.model_validate(conversation),
        messages=[_message_out(row) for row in rows],
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str, user: CurrentUser, db: DbDep
) -> None:
    conversation = await _get_owned_conversation(
        db, user.id, _parse_uuid(conversation_id)
    )
    await db.execute(delete(Conversation).where(Conversation.id == conversation.id))
    await db.commit()


@router.post("/{conversation_id}/ask")
async def ask(
    conversation_id: str,
    payload: AskRequest,
    user: CurrentUser,
    db: DbDep,
    embedder: EmbeddingDep,
    answer_client: AnswerDep,
) -> StreamingResponse:
    """Retrieve, then stream a grounded, cited answer.

    Everything up to the `StreamingResponse` runs in the request scope on purpose:

      * a Voyage outage becomes 503 with an error envelope, not an `error` frame three
        events into a 200 the UI has already begun rendering;
      * a missing Anthropic key becomes 502, and only when the model would actually have
        been called -- the `insufficient_context` path needs no model and must keep
        working without one;
      * the user's message is persisted before the stream opens (Contract 7 §8).
    """
    conversation = await _get_owned_conversation(
        db, user.id, _parse_uuid(conversation_id)
    )

    question = (payload.question or "").strip()
    if not question:
        raise ValidationError("Ask a question to get an answer.")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValidationError("That question is too long. Try a shorter one.")

    # Scope. `null` means all of the caller's documents. A supplied list is narrowed to
    # documents the caller actually owns -- retrieval is scoped in SQL regardless, but
    # narrowing here means another user's id in the list changes nothing rather than
    # quietly widening the search.
    document_ids: Optional[List[uuid.UUID]] = None
    if payload.document_ids is not None:
        requested = []
        for raw in payload.document_ids:
            try:
                requested.append(uuid.UUID(raw))
            except (ValueError, AttributeError, TypeError):
                continue
        document_ids = await documents_service.accessible_document_ids(
            db, user_id=user.id, document_ids=requested
        )

    # Voyage failure here raises EmbeddingUnavailableError -> 503 (Contract 9).
    query_vector = await embedder.embed_query(question)

    passages = await retrieve(
        db,
        user_id=user.id,
        query_vector=query_vector,
        document_ids=document_ids,
    )

    # Claude is only called when something cleared the similarity floor, so the
    # readiness check belongs here rather than at the top of the handler.
    if passages and not answer_client.is_configured():
        raise AnswerUnavailableError()

    # History is read BEFORE the new question is stored, or the question would arrive in
    # its own history and be asked twice.
    history = await answer_service.load_history(db, conversation.id)

    now = datetime.now(timezone.utc)
    db.add(
        Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            role="user",
            content=question,
            citations=[],
            created_at=now,
        )
    )
    if conversation.title is None:
        conversation.title = answer_service.title_from_question(question)
    await db.commit()

    prepared = answer_service.build_prepared(
        conversation_id=conversation.id,
        question=question,
        passages=passages,
        history=history,
    )

    return StreamingResponse(
        answer_service.stream_answer(
            prepared,
            answer_client=answer_client,
            sessionmaker=get_sessionmaker(),
        ),
        media_type="text/event-stream",
        headers=dict(STREAM_HEADERS),
    )
