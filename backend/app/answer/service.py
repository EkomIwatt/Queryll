"""The streamed answer (Contract 7). The centrepiece.

Event sequence, strictly ordered and enforced by construction here:

    retrieval   exactly once, ALWAYS FIRST, before the model is called
    token       zero or more, in order
    done        exactly once, terminal
    error       terminal, replaces `done`

Two structural decisions in this file are worth reading before changing anything:

**The work is split either side of the response headers.** Everything that can fail with
an HTTP status -- auth, ownership, embedding the question, retrieval, persisting the user
message -- happens in the request scope, BEFORE a `StreamingResponse` is returned. So a
Voyage outage is a 503 with an error envelope, not an `error` frame inside a 200 that the
UI has already started rendering. After the headers go out, every failure is an in-stream
`error` event, because the status line is spent.

**Generation runs in a background task, not in the response generator.** Contract 7 §8
requires that a client which disconnects mid-stream still gets a persisted message. If
the model were driven from inside the generator, Starlette closing that generator on
disconnect would cancel generation. So the task pushes frames into a queue and the
generator drains it: the reader can vanish and the answer still finishes and is saved.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, List, Optional, Sequence, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.answer.claude_client import AnswerClient
from app.answer.markers import MarkerFilter
from app.answer.prompt import (
    INSUFFICIENT_CONTEXT_SENTENCE,
    build_messages,
    build_system_blocks,
)
from app.config import get_settings
from app.errors import AppError
from app.models import Message
from app.retrieval.citations import make_citations
from app.retrieval.types import RetrievedPassage
from app.schemas import Citation
from app.sse import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_RETRIEVAL,
    EVENT_TOKEN,
    HEARTBEAT,
    frame,
)

logger = logging.getLogger(__name__)

CONVERSATION_TITLE_MAX_CHARS = 60

# Strong references to in-flight generations. Without this the event loop is free to
# garbage-collect a task nobody is awaiting -- which is precisely the task that has to
# outlive its reader.
_background_tasks: Set["asyncio.Task"] = set()


class _End:
    """Queue sentinel. Not an event -- it only closes the generator."""


_END = _End()


@dataclass
class PreparedAnswer:
    conversation_id: uuid.UUID
    question: str
    passages: List[RetrievedPassage]
    citations: List[Citation]
    insufficient_context: bool
    history: List[Dict[str, str]] = field(default_factory=list)


def build_prepared(
    *,
    conversation_id: uuid.UUID,
    question: str,
    passages: Sequence[RetrievedPassage],
    history: Sequence[Dict[str, str]],
) -> PreparedAnswer:
    """Passages in, Citations out. An empty passage list IS insufficient_context."""
    passage_list = list(passages)
    return PreparedAnswer(
        conversation_id=conversation_id,
        question=question,
        passages=passage_list,
        citations=make_citations(passage_list),
        insufficient_context=not passage_list,
        history=list(history),
    )


def title_from_question(question: str) -> str:
    """Contract 7 §9: first 60 chars of the first question. ASSUMED -- display only."""
    collapsed = " ".join(question.split())
    return collapsed[:CONVERSATION_TITLE_MAX_CHARS]


async def load_history(
    db: AsyncSession, conversation_id: uuid.UUID, limit: Optional[int] = None
) -> List[Dict[str, str]]:
    """The last `history_max_messages` turns.

    Prior turns are included as message history, but prior turns' retrieved chunks are
    NOT re-sent -- each question retrieves fresh (Contract 7 §6).
    """
    if limit is None:
        limit = get_settings().history_max_messages
    if limit <= 0:
        return []
    rows = (
        (
            await db.execute(
                select(Message.role, Message.content)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


# ---------------------------------------------------------------------------
# The stream
# ---------------------------------------------------------------------------


def stream_answer(
    prepared: PreparedAnswer,
    *,
    answer_client: AnswerClient,
    sessionmaker: async_sessionmaker,
    heartbeat_seconds: Optional[float] = None,
) -> AsyncIterator[str]:
    """Return the SSE body generator. Generation starts as soon as it is iterated."""
    if heartbeat_seconds is None:
        heartbeat_seconds = get_settings().sse_heartbeat_seconds

    queue: "asyncio.Queue" = asyncio.Queue()

    async def drain() -> AsyncIterator[str]:
        task = asyncio.create_task(
            _generate(queue, prepared, answer_client, sessionmaker)
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                # A comment heartbeat while the model is thinking, so an idle proxy
                # does not decide the connection is dead and close it.
                yield HEARTBEAT
                continue
            if isinstance(item, _End):
                return
            yield item

    return drain()


async def _generate(
    queue: "asyncio.Queue",
    prepared: PreparedAnswer,
    answer_client: AnswerClient,
    sessionmaker: async_sessionmaker,
) -> None:
    try:
        # ALWAYS FIRST, exactly once -- the UI paints the sources it is about to answer
        # from while the answer is still being generated.
        await queue.put(
            frame(
                EVENT_RETRIEVAL,
                {
                    "sources": [c.model_dump(mode="json") for c in prepared.citations],
                    "insufficient_context": prepared.insufficient_context,
                },
            )
        )

        if prepared.insufficient_context:
            await _answer_without_the_model(queue, prepared, sessionmaker)
            return

        await _answer_with_the_model(queue, prepared, answer_client, sessionmaker)

    except AppError as exc:
        # The status line is long gone by now, so this goes out as an error event.
        await queue.put(frame(EVENT_ERROR, {"error": exc.message, "code": exc.code}))
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        raise
    except Exception:
        logger.exception("Unhandled failure while generating an answer.")
        await queue.put(
            frame(
                EVENT_ERROR,
                {
                    "error": "Something went wrong while writing that answer.",
                    "code": "internal_error",
                },
            )
        )
    finally:
        await queue.put(_END)


async def _answer_without_the_model(
    queue: "asyncio.Queue", prepared: PreparedAnswer, sessionmaker: async_sessionmaker
) -> None:
    """The grounding rule (Contract 7 §5).

    Nothing cleared the similarity floor, so Claude is NOT CALLED AT ALL. Answering from
    general knowledge when retrieval fails is the single worst thing this app can do --
    it looks exactly like the app working -- and the cheapest way to guarantee it never
    happens is to not make the call.
    """
    await queue.put(frame(EVENT_TOKEN, {"text": INSUFFICIENT_CONTEXT_SENTENCE}))
    message_id = await _persist_assistant_message(
        sessionmaker,
        conversation_id=prepared.conversation_id,
        content=INSUFFICIENT_CONTEXT_SENTENCE,
        citations=[],
    )
    await queue.put(
        frame(EVENT_DONE, {"message_id": str(message_id), "citations_used": []})
    )


async def _answer_with_the_model(
    queue: "asyncio.Queue",
    prepared: PreparedAnswer,
    answer_client: AnswerClient,
    sessionmaker: async_sessionmaker,
) -> None:
    marker_filter = MarkerFilter(len(prepared.citations))
    emitted: List[str] = []

    system_blocks = build_system_blocks(prepared.passages)
    messages = build_messages(prepared.history, prepared.question)

    async for delta in answer_client.stream_answer(
        system_blocks=system_blocks, messages=messages
    ):
        safe = marker_filter.feed(delta)
        if safe:
            emitted.append(safe)
            await queue.put(frame(EVENT_TOKEN, {"text": safe}))

    tail = marker_filter.flush()
    if tail:
        emitted.append(tail)
        await queue.put(frame(EVENT_TOKEN, {"text": tail}))

    content = "".join(emitted)
    message_id = await _persist_assistant_message(
        sessionmaker,
        conversation_id=prepared.conversation_id,
        content=content,
        citations=prepared.citations,
    )
    await queue.put(
        frame(
            EVENT_DONE,
            {
                "message_id": str(message_id),
                "citations_used": marker_filter.citations_used,
            },
        )
    )


async def _persist_assistant_message(
    sessionmaker: async_sessionmaker,
    *,
    conversation_id: uuid.UUID,
    content: str,
    citations: Sequence[Citation],
) -> uuid.UUID:
    """Written on `done`, with the FULL Citation array in the `citations` column.

    It opens its own session: the request-scoped one is closed by the time this runs,
    which is the whole point -- the reader may already be gone.

    Nothing is persisted on the error path. An `error` event replaces `done`, the answer
    is incomplete, and storing a truncated answer as though it were finished would leave
    a half-argument in the transcript with citations attached to it.
    """
    message = Message(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        citations=[c.model_dump(mode="json") for c in citations],
        created_at=datetime.now(timezone.utc),
    )
    async with sessionmaker() as db:
        db.add(message)
        await db.commit()
    return message.id
