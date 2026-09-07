"""Contract 7 -- the streamed answer.

These tests assert the exact event SEQUENCE and ORDERING, not just the final text. The
ordering is the contract: Instance 3 paints the source cards the instant `retrieval`
lands and streams tokens beneath them, so `retrieval` arriving second -- or twice -- is a
broken UI even if every token is perfect.

No test here calls the live Anthropic API. Answers come from `ScriptedAnswerClient`.
"""

import asyncio
import json
import uuid

from sqlalchemy import select

from app.answer.claude_client import ScriptedAnswerClient
from app.answer.prompt import INSUFFICIENT_CONTEXT_SENTENCE
from app.embeddings.fake import FakeEmbeddingClient
from app.errors import AnswerUnavailableError, EmbeddingUnavailableError
from app.models import Conversation, Message
from conftest import (
    auth_headers,
    make_chunks,
    make_conversation,
    make_document,
    make_user,
)

MATCHING_TEXT = "The sampling frame was national and stratified by region."


def parse_sse(body: str):
    """Return [(event, data)] and ignore `: ping` comments, as a real client does."""
    frames = []
    for block in body.split("\n\n"):
        block = block.strip("\n")
        if not block or block.startswith(":"):
            continue
        event = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None:
            frames.append((event, data))
    return frames


async def ask(client, user, conversation, question="What was the sampling frame?", **kw):
    async with client.stream(
        "POST",
        "/api/conversations/" + str(conversation.id) + "/ask",
        headers={**auth_headers(user), "Accept": "text/event-stream"},
        json={"question": question, **kw},
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])
        return response, parse_sse(body)


async def seed_answerable(db, user):
    document = await make_document(db, user, filename="survey.pdf")
    await make_chunks(db, document, [MATCHING_TEXT], pages=[4], heading_path="3. Methods")
    return document


class TestEventSequence:
    async def test_retrieval_is_always_the_first_event_and_arrives_exactly_once(
        self, client, db, app
    ):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        names = [name for name, _ in frames]
        assert names[0] == "retrieval"
        assert names.count("retrieval") == 1

    async def test_done_is_the_last_event_and_arrives_exactly_once(self, client, db):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        names = [name for name, _ in frames]
        assert names[-1] == "done"
        assert names.count("done") == 1
        assert "error" not in names

    async def test_tokens_arrive_between_retrieval_and_done_in_order(self, client, db, app):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        app.state.answer_client = ScriptedAnswerClient(["one ", "two ", "three"])

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        names = [name for name, _ in frames]
        assert names == ["retrieval", "token", "token", "token", "done"]

        text = "".join(data["text"] for name, data in frames if name == "token")
        assert text == "one two three"

    async def test_the_response_is_an_unbuffered_event_stream(self, client, db):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)

        async with client.stream(
            "POST",
            "/api/conversations/" + str(conversation.id) + "/ask",
            headers=auth_headers(user),
            json={"question": MATCHING_TEXT},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            # The header that stops nginx turning a token stream into one final burst.
            assert response.headers["x-accel-buffering"] == "no"
            assert "no-cache" in response.headers["cache-control"]
            assert "content-encoding" not in response.headers
            await response.aread()


class TestRetrievalEvent:
    async def test_sources_carry_the_full_citation_shape(self, client, db):
        user = await make_user(db)
        document = await seed_answerable(db, user)
        conversation = await make_conversation(db, user)

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        sources = frames[0][1]["sources"]
        assert len(sources) == 1

        source = sources[0]
        assert source["index"] == 1
        assert source["document_id"] == str(document.id)
        assert uuid.UUID(source["chunk_id"])
        assert source["filename"] == "survey.pdf"
        assert source["page_start"] == 4
        assert source["heading_path"] == "3. Methods"
        assert 0.0 <= source["similarity"] <= 1.0
        assert source["snippet"]

    async def test_a_source_never_carries_an_embedding(self, client, db):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        assert "embedding" not in json.dumps(frames[0][1])

    async def test_sources_are_sent_before_the_model_is_called(self, client, db, app):
        # Not just ordering in the output -- the model genuinely has not been reached
        # when the first frame is built.
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        scripted = ScriptedAnswerClient(["answer"])
        app.state.answer_client = scripted

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        assert frames[0][0] == "retrieval"
        assert len(scripted.calls) == 1


class TestGroundingRule:
    async def test_when_nothing_clears_the_floor_claude_is_not_called_at_all(
        self, client, db, app
    ):
        # The single worst thing this app could do is answer from general knowledge when
        # retrieval fails. The cheapest guarantee it never happens is to not make
        # the call -- so that is what is asserted, not the wording of the answer.
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        scripted = ScriptedAnswerClient(["should never be sent"])
        app.state.answer_client = scripted

        _, frames = await ask(
            client, user, conversation, "Something entirely unrelated to any document"
        )
        assert scripted.calls == []

        names = [name for name, _ in frames]
        assert names == ["retrieval", "token", "done"]
        assert frames[0][1] == {"sources": [], "insufficient_context": True}
        assert frames[1][1]["text"] == INSUFFICIENT_CONTEXT_SENTENCE

    async def test_the_honest_refusal_is_persisted_like_any_other_answer(
        self, client, db
    ):
        user = await make_user(db)
        conversation = await make_conversation(db, user)

        _, frames = await ask(client, user, conversation, "Nothing matches this")
        message_id = frames[-1][1]["message_id"]

        stored = await db.get(Message, uuid.UUID(message_id))
        assert stored.role == "assistant"
        assert stored.content == INSUFFICIENT_CONTEXT_SENTENCE
        assert stored.citations == []

    async def test_the_refusal_path_still_works_without_an_anthropic_key(
        self, client, db, app
    ):
        # The app failing and the app correctly finding nothing must not look the same,
        # and the honest answer must not depend on a service it never calls.
        user = await make_user(db)
        conversation = await make_conversation(db, user)
        app.state.answer_client = ScriptedAnswerClient([], configured=False)

        response, frames = await ask(client, user, conversation, "Nothing matches")
        assert response.status_code == 200
        assert [name for name, _ in frames] == ["retrieval", "token", "done"]

    async def test_an_answer_over_real_sources_reports_which_were_used(
        self, client, db, app
    ):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        app.state.answer_client = ScriptedAnswerClient(["Stratified by region [1]."])

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        assert frames[-1][1]["citations_used"] == [1]


class TestMarkerValidationInTheStream:
    async def test_an_invented_source_number_never_reaches_the_client(
        self, client, db, app
    ):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        app.state.answer_client = ScriptedAnswerClient(["Claimed [9] falsely."])

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        text = "".join(d["text"] for n, d in frames if n == "token")
        assert "[9]" not in text
        assert frames[-1][1]["citations_used"] == []

    async def test_a_marker_split_across_two_deltas_is_never_half_emitted(
        self, client, db, app
    ):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        # Deliberately split mid-marker, which is what a real stream does constantly.
        app.state.answer_client = ScriptedAnswerClient(["National in scope ", "[", "1", "]."])

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        tokens = [d["text"] for n, d in frames if n == "token"]
        for token in tokens:
            assert not (token.count("[") > token.count("]"))
        assert "".join(tokens) == "National in scope [1]."
        assert frames[-1][1]["citations_used"] == [1]


class TestPersistence:
    async def test_the_user_message_is_written_before_the_stream_opens(self, client, db):
        user = await make_user(db)
        conversation = await make_conversation(db, user)

        await ask(client, user, conversation, "A question worth keeping")
        rows = (
            (
                await db.execute(
                    select(Message).where(
                        Message.conversation_id == conversation.id,
                        Message.role == "user",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.content for r in rows] == ["A question worth keeping"]

    async def test_the_assistant_message_is_written_on_done_with_its_citations(
        self, client, db, app
    ):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        app.state.answer_client = ScriptedAnswerClient(["Stratified [1]."])

        _, frames = await ask(client, user, conversation, MATCHING_TEXT)
        stored = await db.get(Message, uuid.UUID(frames[-1][1]["message_id"]))

        assert stored.content == "Stratified [1]."
        # The FULL Citation array, so a historical answer keeps showing what it was
        # based on even after its source document is deleted or re-indexed.
        assert len(stored.citations) == 1
        assert stored.citations[0]["filename"] == "survey.pdf"
        assert stored.citations[0]["index"] == 1

    async def test_a_reader_that_disconnects_still_gets_a_persisted_answer(
        self, client, db, app
    ):
        """Contract 7 §8 -- the reason generation runs in a background task.

        If the model were driven from inside the response generator, Starlette closing
        that generator on disconnect would cancel generation and the user would refresh
        into a conversation with a question and no answer.
        """
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        app.state.answer_client = ScriptedAnswerClient(["partial ", "then ", "more"])

        async with client.stream(
            "POST",
            "/api/conversations/" + str(conversation.id) + "/ask",
            headers=auth_headers(user),
            json={"question": MATCHING_TEXT},
        ) as response:
            async for _ in response.aiter_text():
                break  # walk away mid-stream

        for _ in range(100):
            await asyncio.sleep(0.02)
            db.expunge_all()
            rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.conversation_id == conversation.id,
                            Message.role == "assistant",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if rows:
                break
        assert len(rows) == 1
        assert rows[0].content == "partial then more"

    async def test_the_conversation_title_is_set_from_the_first_question(
        self, client, db
    ):
        user = await make_user(db)
        conversation = await make_conversation(db, user, title=None)

        await ask(client, user, conversation, "What did the report conclude about cost?")
        db.expunge_all()
        stored = await db.get(Conversation, conversation.id)
        assert stored.title == "What did the report conclude about cost?"

    async def test_an_existing_title_is_not_overwritten(self, client, db):
        user = await make_user(db)
        conversation = await make_conversation(db, user, title="Chosen by the user")

        await ask(client, user, conversation, "A different question entirely")
        db.expunge_all()
        stored = await db.get(Conversation, conversation.id)
        assert stored.title == "Chosen by the user"


class TestFailurePaths:
    async def test_a_voyage_outage_is_503_before_the_stream_opens(
        self, client, db, app
    ):
        # Not an `error` frame inside a 200 the UI has already begun rendering.
        user = await make_user(db)
        conversation = await make_conversation(db, user)
        app.state.embedding_client = FakeEmbeddingClient(
            fail_with=EmbeddingUnavailableError()
        )

        response = await client.post(
            "/api/conversations/" + str(conversation.id) + "/ask",
            headers=auth_headers(user),
            json={"question": "anything"},
        )
        assert response.status_code == 503
        assert response.json() == {
            "error": "Search is temporarily unavailable. Try again in a moment."
        }

    async def test_a_missing_anthropic_key_is_502_before_the_stream_opens(
        self, client, db, app
    ):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        app.state.answer_client = ScriptedAnswerClient([], configured=False)

        response = await client.post(
            "/api/conversations/" + str(conversation.id) + "/ask",
            headers=auth_headers(user),
            json={"question": MATCHING_TEXT},
        )
        assert response.status_code == 502
        assert list(response.json().keys()) == ["error"]

    async def test_a_failure_after_the_stream_opens_is_an_error_event(
        self, client, db, app
    ):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        app.state.answer_client = ScriptedAnswerClient(
            ["Partial text so far ", "and more"],
            fail_after=1,
            error=AnswerUnavailableError(),
        )

        response, frames = await ask(client, user, conversation, MATCHING_TEXT)
        # The HTTP status is already 200 by then; the status line is spent.
        assert response.status_code == 200

        names = [name for name, _ in frames]
        assert names[0] == "retrieval"
        assert names[-1] == "error"
        assert "done" not in names

        # The partial text the user already saw is not retracted.
        assert "Partial text so far " in "".join(
            d["text"] for n, d in frames if n == "token"
        )
        assert set(frames[-1][1].keys()) == {"error", "code"}

    async def test_nothing_is_persisted_when_the_stream_errors(self, client, db, app):
        user = await make_user(db)
        await seed_answerable(db, user)
        conversation = await make_conversation(db, user)
        app.state.answer_client = ScriptedAnswerClient(
            ["Partial"], fail_after=1, error=AnswerUnavailableError()
        )

        await ask(client, user, conversation, MATCHING_TEXT)
        await asyncio.sleep(0.1)
        db.expunge_all()
        rows = (
            (
                await db.execute(
                    select(Message).where(
                        Message.conversation_id == conversation.id,
                        Message.role == "assistant",
                    )
                )
            )
            .scalars()
            .all()
        )
        # A truncated answer stored as though it were finished would leave a
        # half-argument in the transcript with citations attached to it.
        assert rows == []

    async def test_an_empty_question_is_422(self, client, db):
        user = await make_user(db)
        conversation = await make_conversation(db, user)
        response = await client.post(
            "/api/conversations/" + str(conversation.id) + "/ask",
            headers=auth_headers(user),
            json={"question": "   "},
        )
        assert response.status_code == 422

    async def test_asking_without_a_token_is_401(self, client, db):
        user = await make_user(db)
        conversation = await make_conversation(db, user)
        response = await client.post(
            "/api/conversations/" + str(conversation.id) + "/ask",
            json={"question": "hello"},
        )
        assert response.status_code == 401


class TestScopedAsk:
    async def test_scoping_to_a_document_restricts_the_sources(self, client, db):
        user = await make_user(db)
        wanted = await make_document(db, user, filename="wanted.txt")
        other = await make_document(db, user, filename="other.txt")
        await make_chunks(db, wanted, [MATCHING_TEXT])
        await make_chunks(db, other, [MATCHING_TEXT])
        conversation = await make_conversation(db, user)

        _, frames = await ask(
            client, user, conversation, MATCHING_TEXT, document_ids=[str(wanted.id)]
        )
        filenames = {s["filename"] for s in frames[0][1]["sources"]}
        assert filenames == {"wanted.txt"}

    async def test_null_document_ids_searches_everything_the_caller_owns(
        self, client, db
    ):
        user = await make_user(db)
        first = await make_document(db, user, filename="first.txt")
        second = await make_document(db, user, filename="second.txt")
        await make_chunks(db, first, [MATCHING_TEXT])
        await make_chunks(db, second, [MATCHING_TEXT])
        conversation = await make_conversation(db, user)

        _, frames = await ask(
            client, user, conversation, MATCHING_TEXT, document_ids=None
        )
        filenames = {s["filename"] for s in frames[0][1]["sources"]}
        assert filenames == {"first.txt", "second.txt"}
