"""Embedding clients (Contract 4)."""

from __future__ import annotations

from queryll_worker.config import Settings
from queryll_worker.embeddings.base import (
    Embedder,
    check_batch,
    check_vector,
    l2_norm,
)
from queryll_worker.embeddings.fake import FakeEmbedder, fake_vector
from queryll_worker.embeddings.voyage import VoyageEmbedder

__all__ = [
    "Embedder",
    "FakeEmbedder",
    "VoyageEmbedder",
    "build_embedder",
    "check_batch",
    "check_vector",
    "fake_vector",
    "l2_norm",
]


def build_embedder(settings: Settings) -> Embedder:
    """Select the embedder for this process.

    `EMBEDDING_PROVIDER=fake` exists so the worker can be run end to end locally without a
    Voyage key. It produces noise, so retrieval over a fake-embedded library is meaningless —
    which is why the choice is explicit and logged rather than an automatic fallback when the
    key is missing.
    """
    if settings.embedding_provider == "fake":
        return FakeEmbedder(dimension=settings.embedding_dim)
    return VoyageEmbedder(
        settings.voyage_api_key,
        model=settings.embedding_model,
        dimension=settings.embedding_dim,
    )
