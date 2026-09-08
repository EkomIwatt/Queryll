"""Contract 6 -- the documents HTTP API, and Contract 3's half of it.

The worker never runs in these tests. Documents and chunks are seeded directly, exactly
as the decomposition intended: the two backend instances share nothing but the database,
so each can be tested against rows it writes itself.
"""

import uuid

from sqlalchemy import select, text

from app.models import Chunk, Document, IngestionJob
from conftest import auth_headers, make_chunks, make_document, make_user

DOCS = "/api/documents"

# Smallest thing that parses as a PDF header, which is all the sniffer looks at.
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def upload(content: bytes, filename: str, content_type: str = "application/octet-stream"):
    return {"file": (filename, content, content_type)}


class TestUpload:
    async def test_returns_202_with_a_pending_document(self, client, db):
        user = await make_user(db)
        response = await client.post(
            DOCS, headers=auth_headers(user), files=upload(b"Some notes.", "notes.txt")
        )
        assert response.status_code == 202

        body = response.json()
        assert body["status"] == "pending"
        assert body["progress"] == 0.0
        assert body["filename"] == "notes.txt"
        assert body["size_bytes"] == len(b"Some notes.")

    async def test_page_and_chunk_counts_are_null_because_nothing_has_parsed_yet(
        self, client, db
    ):
        user = await make_user(db)
        body = (
            await client.post(
                DOCS, headers=auth_headers(user), files=upload(b"text", "a.txt")
            )
        ).json()
        assert body["page_count"] is None
        assert body["chunk_count"] is None
        assert body["indexed_at"] is None
        assert body["error_message"] is None

    async def test_the_job_is_enqueued_in_the_same_transaction_as_the_document(
        self, client, db
    ):
        # Contract 3 §2. A committed document without a job is an unreachable state,
        # so the assertion is that one never exists rather than that a sweeper fixes it.
        user = await make_user(db)
        body = (
            await client.post(
                DOCS, headers=auth_headers(user), files=upload(b"text", "a.txt")
            )
        ).json()

        job = (
            await db.execute(
                select(IngestionJob).where(
                    IngestionJob.document_id == uuid.UUID(body["id"])
                )
            )
        ).scalar_one()
        assert job.state == "queued"
        assert job.attempts == 0
        assert job.locked_at is None

    async def test_the_original_bytes_are_stored_in_the_database(self, client, db):
        # `content` as bytea is load-bearing: the API and the worker share nothing but
        # this database, so there is deliberately no disk and no object store.
        user = await make_user(db)
        payload = b"the exact bytes that were uploaded"
        body = (
            await client.post(
                DOCS, headers=auth_headers(user), files=upload(payload, "a.txt")
            )
        ).json()
        stored = await db.get(Document, uuid.UUID(body["id"]))
        assert stored.content == payload

    async def test_a_pdf_is_detected_from_its_bytes(self, client, db):
        user = await make_user(db)
        response = await client.post(
            DOCS, headers=auth_headers(user), files=upload(PDF_BYTES, "paper.pdf")
        )
        assert response.json()["mime_type"] == "application/pdf"

    async def test_markdown_is_distinguished_by_extension(self, client, db):
        user = await make_user(db)
        response = await client.post(
            DOCS, headers=auth_headers(user), files=upload(b"# Title\n", "readme.md")
        )
        assert response.json()["mime_type"] == "text/markdown"

    async def test_a_lying_content_type_does_not_win_over_the_bytes(self, client, db):
        # Declared application/pdf, actually plain text. The bytes decide.
        user = await make_user(db)
        response = await client.post(
            DOCS,
            headers=auth_headers(user),
            files=upload(b"just text really", "trap.pdf", "application/pdf"),
        )
        assert response.status_code == 202
        assert response.json()["mime_type"] == "text/plain"

    async def test_a_binary_upload_is_415(self, client, db):
        user = await make_user(db)
        response = await client.post(
            DOCS,
            headers=auth_headers(user),
            files=upload(b"\x89PNG\r\n\x1a\n\x00\x00", "image.png", "image/png"),
        )
        assert response.status_code == 415
        assert list(response.json().keys()) == ["error"]

    async def test_an_upload_over_twenty_megabytes_is_413(self, client, db):
        user = await make_user(db)
        oversized = b"a" * (20 * 1024 * 1024 + 1)
        response = await client.post(
            DOCS, headers=auth_headers(user), files=upload(oversized, "big.txt")
        )
        assert response.status_code == 413

    async def test_an_empty_file_is_rejected(self, client, db):
        user = await make_user(db)
        response = await client.post(
            DOCS, headers=auth_headers(user), files=upload(b"", "empty.txt")
        )
        assert response.status_code == 422

    async def test_a_missing_file_field_is_422(self, client, db):
        user = await make_user(db)
        response = await client.post(DOCS, headers=auth_headers(user))
        assert response.status_code == 422
        assert list(response.json().keys()) == ["error"]

    async def test_upload_requires_authentication(self, client):
        response = await client.post(DOCS, files=upload(b"text", "a.txt"))
        assert response.status_code == 401


class TestList:
    async def test_returns_only_the_callers_own_documents_newest_first(
        self, client, db
    ):
        user = await make_user(db)
        stranger = await make_user(db)
        await make_document(db, user, filename="first.txt")
        await make_document(db, user, filename="second.txt")
        await make_document(db, stranger, filename="not-yours.txt")

        body = (await client.get(DOCS, headers=auth_headers(user))).json()
        names = [d["filename"] for d in body["documents"]]
        assert "not-yours.txt" not in names
        assert names == ["second.txt", "first.txt"]
        assert body["next_cursor"] is None

    async def test_paginates_with_an_opaque_cursor_without_repeating_a_row(
        self, client, db
    ):
        user = await make_user(db)
        for index in range(5):
            await make_document(db, user, filename="doc" + str(index) + ".txt")

        first = (
            await client.get(DOCS + "?limit=2", headers=auth_headers(user))
        ).json()
        assert len(first["documents"]) == 2
        assert first["next_cursor"]

        second = (
            await client.get(
                DOCS + "?limit=2&cursor=" + first["next_cursor"],
                headers=auth_headers(user),
            )
        ).json()
        seen = [d["id"] for d in first["documents"]] + [
            d["id"] for d in second["documents"]
        ]
        assert len(seen) == len(set(seen))

    async def test_a_corrupt_cursor_is_422_not_a_silent_reset(self, client, db):
        user = await make_user(db)
        response = await client.get(
            DOCS + "?cursor=!!!not-base64!!!", headers=auth_headers(user)
        )
        assert response.status_code == 422

    async def test_timestamps_are_iso_utc_with_a_trailing_z(self, client, db):
        user = await make_user(db)
        await make_document(db, user)
        body = (await client.get(DOCS, headers=auth_headers(user))).json()
        assert body["documents"][0]["created_at"].endswith("Z")


class TestDetailAndChunks:
    async def test_get_document_returns_the_worker_written_lifecycle_columns(
        self, client, db
    ):
        user = await make_user(db)
        document = await make_document(db, user, status="failed")
        body = (
            await client.get(DOCS + "/" + str(document.id), headers=auth_headers(user))
        ).json()
        assert body["status"] == "failed"
        # Rendered verbatim by the UI, so it must survive the round trip untouched.
        assert body["error_message"] == (
            "This PDF is password-protected and could not be read."
        )

    async def test_chunks_list_returns_summaries_in_ordinal_order(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["first passage", "second passage", "third"])

        body = (
            await client.get(
                DOCS + "/" + str(document.id) + "/chunks", headers=auth_headers(user)
            )
        ).json()
        assert [c["ordinal"] for c in body["chunks"]] == [0, 1, 2]
        assert body["chunks"][0]["preview"] == "first passage"

    async def test_a_chunk_summary_never_carries_the_embedding_or_full_text(
        self, client, db
    ):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["a passage"])

        response = await client.get(
            DOCS + "/" + str(document.id) + "/chunks", headers=auth_headers(user)
        )
        assert "embedding" not in response.text
        assert "token_count" not in response.text

    async def test_a_single_chunk_comes_back_with_its_neighbours(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user)
        chunks = await make_chunks(db, document, ["one", "two", "three"])

        body = (
            await client.get(
                DOCS + "/" + str(document.id) + "/chunks/" + str(chunks[1].id),
                headers=auth_headers(user),
            )
        ).json()
        assert body["chunk"]["text"] == "two"
        assert body["prev"]["text"] == "one"
        assert body["next"]["text"] == "three"

    async def test_the_first_and_last_chunks_have_null_neighbours(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user)
        chunks = await make_chunks(db, document, ["only", "second"])

        first = (
            await client.get(
                DOCS + "/" + str(document.id) + "/chunks/" + str(chunks[0].id),
                headers=auth_headers(user),
            )
        ).json()
        assert first["prev"] is None
        assert first["next"]["text"] == "second"

    async def test_a_chunk_never_serializes_its_embedding(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user)
        chunks = await make_chunks(db, document, ["a passage"])

        response = await client.get(
            DOCS + "/" + str(document.id) + "/chunks/" + str(chunks[0].id),
            headers=auth_headers(user),
        )
        assert "embedding" not in response.text

    async def test_a_chunk_id_from_another_document_is_404(self, client, db):
        user = await make_user(db)
        mine = await make_document(db, user, filename="mine.txt")
        other = await make_document(db, user, filename="other.txt")
        other_chunks = await make_chunks(db, other, ["elsewhere"])

        response = await client.get(
            DOCS + "/" + str(mine.id) + "/chunks/" + str(other_chunks[0].id),
            headers=auth_headers(user),
        )
        assert response.status_code == 404

    async def test_a_malformed_id_is_404_not_422(self, client, db):
        # A 422 would confirm which guesses were at least well-formed ids.
        user = await make_user(db)
        response = await client.get(DOCS + "/not-a-uuid", headers=auth_headers(user))
        assert response.status_code == 404


class TestReindex:
    async def test_resets_the_document_and_deletes_its_chunks(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user, status="ready")
        await make_chunks(db, document, ["one", "two"])

        response = await client.post(
            DOCS + "/" + str(document.id) + "/reindex", headers=auth_headers(user)
        )
        assert response.status_code == 202
        assert response.json()["status"] == "pending"
        assert response.json()["progress"] == 0.0

        remaining = (
            await db.execute(select(Chunk).where(Chunk.document_id == document.id))
        ).scalars().all()
        assert remaining == []

    async def test_enqueues_a_fresh_job(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user, status="ready")
        await client.post(
            DOCS + "/" + str(document.id) + "/reindex", headers=auth_headers(user)
        )
        jobs = (
            await db.execute(
                select(IngestionJob).where(IngestionJob.document_id == document.id)
            )
        ).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].state == "queued"

    async def test_clears_a_previous_error_message(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user, status="failed")
        body = (
            await client.post(
                DOCS + "/" + str(document.id) + "/reindex", headers=auth_headers(user)
            )
        ).json()
        assert body["error_message"] is None

    async def test_reindexing_a_processing_document_is_409(self, client, db):
        # Racing a worker that currently holds the document is refused, not resolved.
        user = await make_user(db)
        document = await make_document(db, user, status="processing")
        response = await client.post(
            DOCS + "/" + str(document.id) + "/reindex", headers=auth_headers(user)
        )
        assert response.status_code == 409

    async def test_reindexing_a_pending_document_is_409(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user, status="pending")
        response = await client.post(
            DOCS + "/" + str(document.id) + "/reindex", headers=auth_headers(user)
        )
        assert response.status_code == 409


class TestDelete:
    async def test_returns_204_and_cascades_to_chunks(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["one", "two"])

        response = await client.delete(
            DOCS + "/" + str(document.id), headers=auth_headers(user)
        )
        assert response.status_code == 204

        # The delete happened in the request's own session; drop this session's
        # identity map or `get` answers from cache rather than from the database.
        db.expunge_all()
        assert await db.get(Document, document.id) is None
        remaining = (
            await db.execute(select(Chunk).where(Chunk.document_id == document.id))
        ).scalars().all()
        assert remaining == []

    async def test_deleting_a_processing_document_is_409(self, client, db):
        user = await make_user(db)
        document = await make_document(db, user, status="processing")
        response = await client.delete(
            DOCS + "/" + str(document.id), headers=auth_headers(user)
        )
        assert response.status_code == 409

    async def test_deleting_a_document_leaves_historical_citations_in_place(
        self, client, db
    ):
        # Contract 8 §3: a historical answer keeps showing what it was based on. The
        # citation goes dangling; the message does not lose it.
        from conftest import make_conversation
        from datetime import datetime, timezone

        from app.models import Message

        user = await make_user(db)
        document = await make_document(db, user)
        conversation = await make_conversation(db, user)
        citation = {
            "index": 1,
            "chunk_id": str(uuid.uuid4()),
            "document_id": str(document.id),
            "filename": document.filename,
            "page_start": None,
            "page_end": None,
            "heading_path": None,
            "similarity": 0.9,
            "snippet": "what it was based on",
        }
        message = Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            role="assistant",
            content="An answer [1].",
            citations=[citation],
            created_at=datetime.now(timezone.utc),
        )
        db.add(message)
        await db.commit()

        await client.delete(DOCS + "/" + str(document.id), headers=auth_headers(user))

        stored = await db.get(Message, message.id)
        assert stored is not None
        assert stored.citations[0]["filename"] == document.filename
