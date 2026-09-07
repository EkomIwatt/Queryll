"""Contract 7 §4 -- retrieval, against real Postgres with pgvector.

Chunks are seeded with fixture vectors from the deterministic fake embedder. The worker
never runs. What is under test here is the SQL: the floor, the per-document cap, the
ordering, and above all the tenant scope.
"""

import uuid

from app.config import get_settings
from app.embeddings.fake import fake_embedding
from app.retrieval.service import retrieve
from conftest import make_chunks, make_document, make_user


def blend(a: str, b: str, weight: float):
    """A vector deliberately partway between two fixture texts.

    The fake embedder is a hash, so unrelated strings are near-orthogonal. To test a
    floor at all we need a query that is *somewhat* like a chunk, which means building
    one by interpolation rather than hoping two strings happen to be similar.
    """
    import math

    left, right = fake_embedding(a), fake_embedding(b)
    mixed = [weight * x + (1.0 - weight) * y for x, y in zip(left, right)]
    norm = math.sqrt(sum(v * v for v in mixed))
    return [v / norm for v in mixed]


class TestRanking:
    async def test_the_exact_chunk_ranks_first_with_similarity_one(self, db):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["alpha text", "beta text", "gamma text"])

        results = await retrieve(
            db, user_id=user.id, query_vector=fake_embedding("beta text")
        )
        assert results[0].text == "beta text"
        assert results[0].similarity > 0.999

    async def test_results_are_ordered_by_similarity_descending(self, db):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["near", "far"])

        results = await retrieve(
            db,
            user_id=user.id,
            query_vector=blend("near", "far", 0.9),
            min_similarity=0.0,
        )
        similarities = [r.similarity for r in results]
        assert similarities == sorted(similarities, reverse=True)
        assert results[0].text == "near"

    async def test_the_same_question_twice_ranks_identically(self, db):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["one", "two", "three", "four"])

        vector = blend("two", "three", 0.6)
        first = await retrieve(db, user_id=user.id, query_vector=vector, min_similarity=0.0)
        second = await retrieve(db, user_id=user.id, query_vector=vector, min_similarity=0.0)
        assert [r.chunk_id for r in first] == [r.chunk_id for r in second]

    async def test_passages_carry_the_metadata_a_citation_needs(self, db):
        user = await make_user(db)
        document = await make_document(db, user, filename="paper.pdf")
        await make_chunks(
            db, document, ["the passage"], pages=[7], heading_path="2. Results"
        )

        result = (
            await retrieve(db, user_id=user.id, query_vector=fake_embedding("the passage"))
        )[0]
        assert result.filename == "paper.pdf"
        assert result.page_start == 7
        assert result.heading_path == "2. Results"
        assert result.ordinal == 0


class TestSimilarityFloor:
    async def test_nothing_is_returned_when_nothing_clears_the_floor(self, db):
        # This empty list IS the insufficient_context path. If it ever silently returned
        # the nearest junk instead, the app would answer confidently from noise.
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["completely unrelated content"])

        results = await retrieve(
            db, user_id=user.id, query_vector=fake_embedding("a totally other question")
        )
        assert results == []

    async def test_a_passage_just_above_the_floor_is_kept(self, db):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["target passage"])

        results = await retrieve(
            db,
            user_id=user.id,
            query_vector=fake_embedding("target passage"),
            min_similarity=0.99,
        )
        assert len(results) == 1

    async def test_a_passage_just_below_the_floor_is_dropped(self, db):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["target passage"])

        results = await retrieve(
            db,
            user_id=user.id,
            query_vector=blend("target passage", "unrelated other", 0.5),
            min_similarity=0.95,
        )
        assert results == []

    async def test_a_user_with_no_documents_gets_nothing(self, db):
        user = await make_user(db)
        assert await retrieve(db, user_id=user.id, query_vector=fake_embedding("q")) == []


class TestPerDocumentCap:
    async def test_no_single_document_may_fill_the_whole_context(self, db):
        # A long document must not drown out a better passage from a shorter one.
        user = await make_user(db)
        long_document = await make_document(db, user, filename="long.txt")
        short_document = await make_document(db, user, filename="short.txt")

        await make_chunks(db, long_document, ["shared topic " + str(i) for i in range(10)])
        await make_chunks(db, short_document, ["shared topic 0"])

        results = await retrieve(
            db,
            user_id=user.id,
            query_vector=fake_embedding("shared topic 0"),
            min_similarity=0.0,
            per_document_cap=4,
        )
        per_document = {}
        for result in results:
            per_document[result.document_id] = per_document.get(result.document_id, 0) + 1
        assert max(per_document.values()) <= 4
        assert short_document.id in per_document

    async def test_the_cap_keeps_the_best_passages_from_each_document(self, db):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["alpha", "beta", "gamma", "delta", "epsilon"])

        capped = await retrieve(
            db,
            user_id=user.id,
            query_vector=fake_embedding("gamma"),
            min_similarity=0.0,
            per_document_cap=2,
        )
        assert len(capped) == 2
        assert capped[0].text == "gamma"


class TestTopK:
    async def test_at_most_top_k_passages_come_back(self, db):
        user = await make_user(db)
        for index in range(4):
            document = await make_document(db, user, filename="d" + str(index) + ".txt")
            await make_chunks(db, document, ["topic " + str(index) + " " + str(j) for j in range(4)])

        results = await retrieve(
            db, user_id=user.id, query_vector=fake_embedding("topic 0 0"),
            min_similarity=0.0, top_k=3,
        )
        assert len(results) == 3

    async def test_the_default_top_k_matches_the_contract(self):
        settings = get_settings()
        assert settings.retrieval_top_k == 8
        assert settings.retrieval_min_similarity == 0.35
        assert settings.retrieval_per_document_cap == 4


class TestScope:
    async def test_another_users_chunks_are_never_retrievable(self, db):
        # The single worst failure this API could have: one user's question answered
        # from another user's documents, with citations.
        owner = await make_user(db)
        stranger = await make_user(db)
        document = await make_document(db, owner, filename="private.txt")
        await make_chunks(db, document, ["a confidential passage"])

        results = await retrieve(
            db,
            user_id=stranger.id,
            query_vector=fake_embedding("a confidential passage"),
            min_similarity=0.0,
        )
        assert results == []

    async def test_scoping_to_document_ids_excludes_the_others(self, db):
        user = await make_user(db)
        wanted = await make_document(db, user, filename="wanted.txt")
        ignored = await make_document(db, user, filename="ignored.txt")
        await make_chunks(db, wanted, ["shared phrase"])
        await make_chunks(db, ignored, ["shared phrase"])

        results = await retrieve(
            db,
            user_id=user.id,
            query_vector=fake_embedding("shared phrase"),
            document_ids=[wanted.id],
            min_similarity=0.0,
        )
        assert results
        assert {r.document_id for r in results} == {wanted.id}

    async def test_scoping_to_an_empty_list_retrieves_nothing(self, db):
        user = await make_user(db)
        document = await make_document(db, user)
        await make_chunks(db, document, ["a passage"])

        results = await retrieve(
            db,
            user_id=user.id,
            query_vector=fake_embedding("a passage"),
            document_ids=[],
            min_similarity=0.0,
        )
        assert results == []

    async def test_scoping_to_a_stranger_document_id_retrieves_nothing(self, db):
        user = await make_user(db)
        stranger = await make_user(db)
        theirs = await make_document(db, stranger)
        await make_chunks(db, theirs, ["their passage"])

        results = await retrieve(
            db,
            user_id=user.id,
            query_vector=fake_embedding("their passage"),
            document_ids=[theirs.id],
            min_similarity=0.0,
        )
        assert results == []

    async def test_an_unknown_document_id_is_simply_empty(self, db):
        user = await make_user(db)
        results = await retrieve(
            db,
            user_id=user.id,
            query_vector=fake_embedding("q"),
            document_ids=[uuid.uuid4()],
            min_similarity=0.0,
        )
        assert results == []
