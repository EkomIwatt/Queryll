"""The HNSW index check.

pgvector does not error when the query's distance operator does not match the index's
operator class -- it silently falls back to a sequential scan. Retrieval still returns
correct-looking results, just slowly, and no functional test anywhere will notice. This
file is the cheapest possible insurance against that.

Two assertions, and the second one is the one that actually has teeth:

* the real retrieval query CAN use `chunks_embedding_idx`;
* the same query written with `<->` CANNOT -- at any cost setting, because the index is
  built `vector_cosine_ops` and an L2 ordering is simply not answerable from it.

`enable_seqscan = off` AND `enable_sort = off` are both set deliberately. On a few
hundred fixture rows, reading every chunk and sorting them is genuinely cheaper than an
index scan, so on cost alone the planner ignores the HNSW index even when it is
perfectly usable -- which would make this test fail for a reason that has nothing to do
with correctness. Both knobs together take cost out of the question and leave exactly
the thing worth asserting: whether the ordering operator can be answered from the index
at all. Neither knob forbids the fallback, it only prices it absurdly, so the wrong query
still produces a plan -- a Sort costing about 1e10 -- rather than an error.

The production-scale version of this check -- `EXPLAIN ANALYZE` against a few thousand
real chunks at DEFAULT planner settings, where cost is the whole question -- is a ★
merge-time item. This test does not pretend to replace it.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ClauseElement, Executable

from app.embeddings.fake import fake_embedding
from app.models import Chunk, Document
from app.retrieval.service import build_retrieval_query
from conftest import make_document, make_user

SEED_ROWS = 400


class Explain(Executable, ClauseElement):
    """EXPLAIN wrapper that keeps the real statement object, typed binds and all.

    Compiling the statement to a string and re-parameterising it by hand would mean
    explaining a hand-written copy of the query rather than the one retrieval runs.
    """

    inherit_cache = False

    def __init__(self, statement):
        self.statement = statement


@compiles(Explain, "postgresql")
def _compile_explain(element, compiler, **kw):
    return "EXPLAIN " + compiler.process(element.statement, **kw)


async def _seed_chunks(db, document: Document, count: int = SEED_ROWS) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": uuid.uuid4(),
            "document_id": document.id,
            "ordinal": ordinal,
            "text": "passage number " + str(ordinal),
            "token_count": 3,
            "page_start": None,
            "page_end": None,
            "char_start": ordinal * 20,
            "char_end": ordinal * 20 + 18,
            "heading_path": None,
            "embedding": fake_embedding("passage number " + str(ordinal)),
            "created_at": now,
        }
        for ordinal in range(count)
    ]
    await db.execute(Chunk.__table__.insert(), rows)
    await db.commit()
    # Without fresh statistics the planner is reasoning about an empty table.
    await db.execute(text("ANALYZE chunks"))
    await db.commit()


async def _plan(db, statement) -> str:
    """EXPLAIN with cost taken out of the question. See the module docstring.

    The plan is truncated per line: a bound 1024-dimension vector renders in full inside
    the `Order By` line, and an assertion message carrying it is unreadable.
    """
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    await db.execute(text("SET LOCAL enable_sort = off"))
    rows = (await db.execute(Explain(statement))).all()
    return "\n".join(str(row[0])[:120] for row in rows)


@pytest.fixture
async def seeded(db):
    user = await make_user(db)
    document = await make_document(db, user)
    await _seed_chunks(db, document)
    return user, document


class TestHnswIndexIsUsable:
    async def test_the_retrieval_query_uses_the_cosine_hnsw_index(self, db, seeded):
        user, _ = seeded
        statement = build_retrieval_query(
            user_id=user.id,
            vector=fake_embedding("passage number 7"),
            document_ids=None,
            candidate_limit=48,
            per_document_cap=4,
            max_distance=0.65,
            top_k=8,
        )

        plan = await _plan(db, statement)

        assert "chunks_embedding_idx" in plan, (
            "The retrieval query cannot use the cosine HNSW index. The usual cause is "
            "an operator mismatch: the index is vector_cosine_ops and only <=> can be "
            "answered from it.\n\n" + plan
        )

    async def test_the_emitted_sql_orders_by_the_cosine_operator(self, db, seeded):
        user, _ = seeded
        statement = build_retrieval_query(
            user_id=user.id,
            vector=fake_embedding("passage number 7"),
            document_ids=None,
            candidate_limit=48,
            per_document_cap=4,
            max_distance=0.65,
            top_k=8,
        )
        sql = str(statement.compile(compile_kwargs={"literal_binds": False}))
        assert "<=>" in sql
        assert "<->" not in sql
        assert "<#>" not in sql

    async def test_an_l2_ordering_cannot_use_the_cosine_index(self, db, seeded):
        """The negative control, and the reason the positive test means anything.

        If this ever starts finding `chunks_embedding_idx` in the plan, then the
        positive assertion above has stopped distinguishing a correct query from a
        wrong one and both tests are worthless.
        """
        from sqlalchemy import select

        user, _ = seeded
        wrong = (
            select(Chunk.id)
            .join(Document, Document.id == Chunk.document_id)
            .where(Document.user_id == user.id)
            .order_by(Chunk.embedding.l2_distance(fake_embedding("passage number 7")))
            .limit(8)
        )

        plan = await _plan(db, wrong)
        assert "chunks_embedding_idx" not in plan
        # Even with sorting priced out of reach it still has to sort, because an L2
        # ordering simply cannot be produced by a vector_cosine_ops index.
        assert "Sort" in plan


class TestIndexDefinition:
    async def test_the_index_is_hnsw_over_vector_cosine_ops(self, db):
        """Assert the shipped DDL, not a remembered version of it.

        Contract 2 is the single source of truth for the schema and no instance owns it,
        so this reads the index back out of the database the tests just built from
        `db/init.sql`.
        """
        definition = (
            await db.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'chunks' AND indexname = 'chunks_embedding_idx'"
                )
            )
        ).scalar_one()
        assert "USING hnsw" in definition
        assert "vector_cosine_ops" in definition

    async def test_the_embedding_column_has_the_contract_dimension(self, db):
        dimension = (
            await db.execute(
                text(
                    "SELECT a.atttypmod FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "WHERE c.relname = 'chunks' AND a.attname = 'embedding'"
                )
            )
        ).scalar_one()
        assert dimension == 1024
