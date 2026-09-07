"""Query-side embedding client (Contract 4).

This instance embeds QUESTIONS with input_type="query". Instance 1 embeds CHUNKS with
input_type="document". Neither may import the other's client; both read the same env var
names with the same defaults, and both assert the same invariants at the boundary.
"""

from app.embeddings.base import (
    EMBEDDING_INPUT_TYPE_QUERY,
    EmbeddingClient,
    EmbeddingContractViolation,
    assert_valid_vector,
    l2_norm,
)
from app.embeddings.fake import FakeEmbeddingClient, fake_embedding

__all__ = [
    "EMBEDDING_INPUT_TYPE_QUERY",
    "EmbeddingClient",
    "EmbeddingContractViolation",
    "FakeEmbeddingClient",
    "assert_valid_vector",
    "fake_embedding",
    "l2_norm",
]
