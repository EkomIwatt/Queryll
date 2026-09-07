"""Vector retrieval (Contract 7 §4).

Four things in here are load-bearing, and three of them fail silently if got wrong:

1. **Cosine, always `<=>`.** Built through pgvector's `cosine_distance()` rather than a
   hand-written operator, because `<->` and `<#>` do not error -- they quietly abandon
   the `vector_cosine_ops` HNSW index for a sequential scan, and a green test suite over
   fifty fixture rows will never notice. `tests/test_index_usage.py` asserts the plan.

2. **Tenant scope lives in the SQL**, in the same WHERE clause as the vector search --
   not in a post-filter and not in Python. A cross-tenant leak here means one user's
   questions answered from another user's documents, with citations.

3. **The per-document cap** stops one long document filling the whole context and
   drowning out a better passage from a shorter one.

4. **Over-fetch, then cap and floor.** HNSW is approximate and pgvector post-filters, so
   a query scoped to one user can come back short of `top_k`. Candidates are pulled at a
   multiple of `top_k`, then the cap and the similarity floor are applied to that set.
"""

import logging
import uuid
from typing import List, Optional, Sequence

from sqlalchemy import Float, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Chunk, Document
from app.retrieval.types import RetrievedPassage

logger = logging.getLogger(__name__)


async def retrieve(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    query_vector: Sequence[float],
    document_ids: Optional[Sequence[uuid.UUID]] = None,
    top_k: Optional[int] = None,
    min_similarity: Optional[float] = None,
    per_document_cap: Optional[int] = None,
) -> List[RetrievedPassage]:
    """Return the passages that clear the similarity floor, best first.

    An empty list means nothing cleared the floor. The caller must treat that as
    `insufficient_context` (Contract 7 §5) and NOT ask Claude to answer anyway.
    """
    settings = get_settings()
    top_k = settings.retrieval_top_k if top_k is None else top_k
    min_similarity = (
        settings.retrieval_min_similarity if min_similarity is None else min_similarity
    )
    per_document_cap = (
        settings.retrieval_per_document_cap
        if per_document_cap is None
        else per_document_cap
    )

    vector = list(query_vector)
    max_distance = 1.0 - min_similarity
    candidate_limit = max(top_k, top_k * settings.retrieval_candidate_multiplier)

    # Raise HNSW's search breadth for this transaction only. The default ef_search of 40
    # is thin once a user-scope filter starts discarding candidates after the index scan.
    # SET LOCAL takes no bind parameters, so the value is interpolated -- it is coerced
    # through int() first, which is what makes that safe rather than an injection point.
    await db.execute(text("SET LOCAL hnsw.ef_search = " + str(int(settings.hnsw_ef_search))))

    statement = build_retrieval_query(
        user_id=user_id,
        vector=vector,
        document_ids=document_ids,
        candidate_limit=candidate_limit,
        per_document_cap=per_document_cap,
        max_distance=max_distance,
        top_k=top_k,
    )

    rows = (await db.execute(statement)).mappings().all()

    passages = [
        RetrievedPassage(
            chunk_id=row["id"],
            document_id=row["document_id"],
            filename=row["filename"],
            ordinal=row["ordinal"],
            text=row["text"],
            token_count=row["token_count"],
            page_start=row["page_start"],
            page_end=row["page_end"],
            heading_path=row["heading_path"],
            similarity=1.0 - float(row["distance"]),
        )
        for row in rows
    ]

    # Document ids are fine to log. Text, embeddings and questions are not.
    logger.info(
        "retrieval: user=%s scope=%s candidates<=%d kept=%d floor=%.2f",
        user_id,
        "all" if not document_ids else str(len(document_ids)) + " docs",
        candidate_limit,
        len(passages),
        min_similarity,
    )
    return passages


def build_retrieval_query(
    *,
    user_id: uuid.UUID,
    vector: Sequence[float],
    document_ids: Optional[Sequence[uuid.UUID]],
    candidate_limit: int,
    per_document_cap: int,
    max_distance: float,
    top_k: int,
):
    """The retrieval statement, separated out so a test can EXPLAIN exactly this query.

    Ordering is `similarity DESC` -- i.e. distance ASC -- with `(document_id, ordinal)`
    ASC as a deterministic tie-break, so two identical questions rank identically.
    """
    distance = Chunk.embedding.cosine_distance(vector).label("distance")

    candidates = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.ordinal,
            Chunk.text,
            Chunk.token_count,
            Chunk.page_start,
            Chunk.page_end,
            Chunk.heading_path,
            Document.filename,
            distance,
        )
        .join(Document, Document.id == Chunk.document_id)
        # Tenant scope, in SQL, in the same WHERE clause as the vector search.
        .where(Document.user_id == user_id)
    )
    if document_ids is not None:
        candidates = candidates.where(Chunk.document_id.in_(list(document_ids)))

    candidates = (
        candidates.order_by(distance).limit(candidate_limit).subquery("candidates")
    )

    row_number = (
        func.row_number()
        .over(
            partition_by=candidates.c.document_id,
            order_by=(candidates.c.distance, candidates.c.ordinal),
        )
        .label("rn")
    )
    ranked = select(candidates, row_number).subquery("ranked")

    return (
        select(ranked)
        .where(ranked.c.rn <= per_document_cap)
        .where(ranked.c.distance <= cast(max_distance, Float))
        .order_by(ranked.c.distance, ranked.c.document_id, ranked.c.ordinal)
        .limit(top_k)
    )
