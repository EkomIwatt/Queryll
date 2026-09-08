"""Cross-tenant isolation, and conversations CRUD (Contracts 7 §9 and 9).

Two accounts, two documents. User B must not be able to list, fetch, re-index, delete or
RETRIEVE OVER user A's document -- and probing A's ids must return 404 rather than 403 on
every single route, because ownership must not be discoverable by probing ids.

The last of those is the one that matters most and is the easiest to get wrong: a 403
says "this exists and is not yours", which is exactly the fact being protected.
"""

import json
import uuid

import pytest
from sqlalchemy import select

from app.models import Conversation, Message
from conftest import (
    auth_headers,
    make_chunks,
    make_conversation,
    make_document,
    make_user,
)

SECRET_TEXT = "The acquisition price was 4.2 million, agreed in March."


@pytest.fixture
async def two_tenants(db):
    owner = await make_user(db, "owner@example.com")
    intruder = await make_user(db, "intruder@example.com")
    document = await make_document(db, owner, filename="confidential.pdf")
    chunks = await make_chunks(db, document, [SECRET_TEXT], pages=[2])
    conversation = await make_conversation(db, owner, title="Owner conversation")
    return owner, intruder, document, chunks[0], conversation


class TestDocumentIsolation:
    async def test_the_intruder_cannot_see_the_document_in_their_library(
        self, client, two_tenants
    ):
        _, intruder, document, _, _ = two_tenants
        body = (await client.get("/api/documents", headers=auth_headers(intruder))).json()
        assert body["documents"] == []

    async def test_fetching_another_users_document_is_404_not_403(
        self, client, two_tenants
    ):
        _, intruder, document, _, _ = two_tenants
        response = await client.get(
            "/api/documents/" + str(document.id), headers=auth_headers(intruder)
        )
        assert response.status_code == 404
        assert response.status_code != 403

    async def test_a_real_id_and_a_random_id_are_indistinguishable(
        self, client, two_tenants
    ):
        # If these ever differ, document ids become an existence oracle.
        _, intruder, document, _, _ = two_tenants
        real = await client.get(
            "/api/documents/" + str(document.id), headers=auth_headers(intruder)
        )
        invented = await client.get(
            "/api/documents/" + str(uuid.uuid4()), headers=auth_headers(intruder)
        )
        assert real.status_code == invented.status_code == 404
        assert real.json() == invented.json()

    async def test_listing_another_users_chunks_is_404(self, client, two_tenants):
        _, intruder, document, _, _ = two_tenants
        response = await client.get(
            "/api/documents/" + str(document.id) + "/chunks",
            headers=auth_headers(intruder),
        )
        assert response.status_code == 404

    async def test_fetching_another_users_chunk_is_404(self, client, two_tenants):
        _, intruder, document, chunk, _ = two_tenants
        response = await client.get(
            "/api/documents/" + str(document.id) + "/chunks/" + str(chunk.id),
            headers=auth_headers(intruder),
        )
        assert response.status_code == 404
        assert SECRET_TEXT not in response.text

    async def test_reindexing_another_users_document_is_404(self, client, two_tenants):
        _, intruder, document, _, _ = two_tenants
        response = await client.post(
            "/api/documents/" + str(document.id) + "/reindex",
            headers=auth_headers(intruder),
        )
        assert response.status_code == 404

    async def test_a_refused_reindex_does_not_touch_the_document(
        self, client, db, two_tenants
    ):
        _, intruder, document, chunk, _ = two_tenants
        await client.post(
            "/api/documents/" + str(document.id) + "/reindex",
            headers=auth_headers(intruder),
        )
        db.expunge_all()
        from app.models import Chunk, Document

        stored = await db.get(Document, document.id)
        assert stored.status == "ready"
        surviving = (
            (await db.execute(select(Chunk).where(Chunk.document_id == document.id)))
            .scalars()
            .all()
        )
        assert len(surviving) == 1

    async def test_deleting_another_users_document_is_404_and_deletes_nothing(
        self, client, db, two_tenants
    ):
        _, intruder, document, _, _ = two_tenants
        response = await client.delete(
            "/api/documents/" + str(document.id), headers=auth_headers(intruder)
        )
        assert response.status_code == 404

        db.expunge_all()
        from app.models import Document

        assert await db.get(Document, document.id) is not None


class TestRetrievalIsolation:
    async def test_the_intruder_cannot_retrieve_over_another_users_documents(
        self, client, two_tenants
    ):
        """The worst possible leak in this app: one user's question answered from
        another user's documents, with citations pointing at them."""
        _, intruder, _, _, _ = two_tenants
        conversation_response = await client.post(
            "/api/conversations", headers=auth_headers(intruder), json={"title": None}
        )
        conversation_id = conversation_response.json()["id"]

        async with client.stream(
            "POST",
            "/api/conversations/" + conversation_id + "/ask",
            headers=auth_headers(intruder),
            json={"question": SECRET_TEXT},
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

        assert SECRET_TEXT not in body
        assert "confidential.pdf" not in body
        assert '"insufficient_context":true' in body.replace(" ", "")

    async def test_naming_another_users_document_id_does_not_widen_the_search(
        self, client, two_tenants
    ):
        _, intruder, document, _, _ = two_tenants
        conversation = (
            await client.post(
                "/api/conversations", headers=auth_headers(intruder), json={}
            )
        ).json()

        async with client.stream(
            "POST",
            "/api/conversations/" + conversation["id"] + "/ask",
            headers=auth_headers(intruder),
            json={"question": SECRET_TEXT, "document_ids": [str(document.id)]},
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

        assert SECRET_TEXT not in body
        assert "confidential.pdf" not in body


class TestConversationIsolation:
    async def test_another_users_conversation_is_404(self, client, two_tenants):
        _, intruder, _, _, conversation = two_tenants
        response = await client.get(
            "/api/conversations/" + str(conversation.id), headers=auth_headers(intruder)
        )
        assert response.status_code == 404

    async def test_the_intruder_cannot_list_another_users_conversations(
        self, client, two_tenants
    ):
        _, intruder, _, _, _ = two_tenants
        body = (
            await client.get("/api/conversations", headers=auth_headers(intruder))
        ).json()
        assert body["conversations"] == []

    async def test_the_intruder_cannot_ask_into_another_users_conversation(
        self, client, two_tenants
    ):
        _, intruder, _, _, conversation = two_tenants
        response = await client.post(
            "/api/conversations/" + str(conversation.id) + "/ask",
            headers=auth_headers(intruder),
            json={"question": "sneaking in"},
        )
        assert response.status_code == 404

    async def test_the_intruder_cannot_delete_another_users_conversation(
        self, client, db, two_tenants
    ):
        _, intruder, _, _, conversation = two_tenants
        response = await client.delete(
            "/api/conversations/" + str(conversation.id), headers=auth_headers(intruder)
        )
        assert response.status_code == 404

        db.expunge_all()
        assert await db.get(Conversation, conversation.id) is not None


class TestConversationsCrud:
    async def test_create_returns_201_with_a_null_title(self, client, db):
        user = await make_user(db)
        response = await client.post(
            "/api/conversations", headers=auth_headers(user), json={"title": None}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["title"] is None
        assert body["created_at"].endswith("Z")

    async def test_create_keeps_a_supplied_title(self, client, db):
        user = await make_user(db)
        body = (
            await client.post(
                "/api/conversations",
                headers=auth_headers(user),
                json={"title": "Q3 planning"},
            )
        ).json()
        assert body["title"] == "Q3 planning"

    async def test_list_returns_the_callers_conversations_newest_first(
        self, client, db
    ):
        user = await make_user(db)
        await make_conversation(db, user, title="older")
        await make_conversation(db, user, title="newer")
        body = (
            await client.get("/api/conversations", headers=auth_headers(user))
        ).json()
        assert [c["title"] for c in body["conversations"]] == ["newer", "older"]

    async def test_detail_returns_messages_oldest_first_with_their_citations(
        self, client, db
    ):
        from datetime import datetime, timedelta, timezone

        user = await make_user(db)
        conversation = await make_conversation(db, user)
        now = datetime.now(timezone.utc)
        citation = {
            "index": 1,
            "chunk_id": str(uuid.uuid4()),
            "document_id": str(uuid.uuid4()),
            "filename": "source.pdf",
            "page_start": 3,
            "page_end": 3,
            "heading_path": None,
            "similarity": 0.812,
            "snippet": "a supporting passage",
        }
        db.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                role="user",
                content="the question",
                citations=[],
                created_at=now,
            )
        )
        db.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                role="assistant",
                content="the answer [1]",
                citations=[citation],
                created_at=now + timedelta(seconds=1),
            )
        )
        await db.commit()

        body = (
            await client.get(
                "/api/conversations/" + str(conversation.id),
                headers=auth_headers(user),
            )
        ).json()
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
        assert body["messages"][1]["citations"][0]["filename"] == "source.pdf"
        assert body["messages"][1]["citations"][0]["similarity"] == 0.812

    async def test_delete_returns_204_and_cascades_to_messages(self, client, db):
        from datetime import datetime, timezone

        user = await make_user(db)
        conversation = await make_conversation(db, user)
        message = Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            role="user",
            content="x",
            citations=[],
            created_at=datetime.now(timezone.utc),
        )
        db.add(message)
        await db.commit()

        response = await client.delete(
            "/api/conversations/" + str(conversation.id), headers=auth_headers(user)
        )
        assert response.status_code == 204

        db.expunge_all()
        assert await db.get(Conversation, conversation.id) is None
        assert await db.get(Message, message.id) is None

    async def test_every_conversation_route_requires_a_token(self, client, db):
        user = await make_user(db)
        conversation = await make_conversation(db, user)
        for method, path in [
            ("GET", "/api/conversations"),
            ("POST", "/api/conversations"),
            ("GET", "/api/conversations/" + str(conversation.id)),
            ("DELETE", "/api/conversations/" + str(conversation.id)),
        ]:
            response = await client.request(method, path, json={})
            assert response.status_code == 401, path
            assert list(response.json().keys()) == ["error"]


class TestErrorEnvelope:
    async def test_an_unmatched_route_still_returns_the_envelope(self, client):
        response = await client.get("/api/does-not-exist")
        assert response.status_code == 404
        assert list(response.json().keys()) == ["error"]

    async def test_a_wrong_method_still_returns_the_envelope(self, client):
        response = await client.put("/api/documents")
        assert response.status_code == 405
        assert list(response.json().keys()) == ["error"]

    async def test_no_error_body_leaks_internals(self, client, db):
        user = await make_user(db)
        responses = [
            await client.get("/api/documents/" + str(uuid.uuid4()), headers=auth_headers(user)),
            await client.get("/api/documents?cursor=@@@", headers=auth_headers(user)),
            await client.get("/api/auth/me"),
        ]
        for response in responses:
            body = json.dumps(response.json()).lower()
            for leak in ["traceback", "select ", "sqlalchemy", "asyncpg", "sk-", "bearer"]:
                assert leak not in body
