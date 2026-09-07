"""Contract 4, enforced.

This is the contract that fails silently. The worker embeds chunks and the API embeds
questions, in two programs written independently that never import each other. If the two
disagree about provider, model, dimension, input type or normalization, nothing crashes:
retrieval quietly returns near-random passages, Claude answers faithfully from them, and both
test suites stay green because each side is internally consistent.

So the shape of every vector is checked at the boundary where it enters the program, and a
failure raises rather than warns. This cannot prove the two sides *agree* — no pre-merge test
can, which is why the cross-process cosine probe is a merge-time check — but it does
guarantee that a wrong-shaped vector never reaches the database.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from queryll_worker.config import NORM_TOLERANCE
from queryll_worker.errors import EmbeddingContractError


class Embedder(Protocol):
    """Anything that can turn chunk texts into document-side vectors."""

    @property
    def model(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed chunk texts with `input_type="document"`, preserving input order."""
        ...

    async def aclose(self) -> None: ...


def l2_norm(vector: Sequence[float]) -> float:
    return math.sqrt(math.fsum(component * component for component in vector))


def check_vector(vector: Sequence[float], dimension: int, *, index: int | None = None) -> None:
    """Assert Contract 4's two invariants on one vector, and raise if either fails.

    Written as explicit raises rather than `assert` statements on purpose: `assert` is
    compiled out under `python -O`, and this is the last line of defence between a
    misconfigured embedding client and a database full of unusable vectors.
    """
    where = "" if index is None else f" (input {index})"
    if len(vector) != dimension:
        raise EmbeddingContractError(
            f"expected {dimension} dims, got {len(vector)}{where}"
        )
    norm = l2_norm(vector)
    if abs(norm - 1.0) >= NORM_TOLERANCE:
        raise EmbeddingContractError(
            f"vector is not L2-normalized: norm={norm:.6f}{where}"
        )


def check_batch(vectors: Sequence[Sequence[float]], dimension: int, expected: int) -> None:
    """Check a whole batch: the right number of vectors, each the right shape."""
    if len(vectors) != expected:
        raise EmbeddingContractError(
            f"expected {expected} vectors, got {len(vectors)}"
        )
    for index, vector in enumerate(vectors):
        check_vector(vector, dimension, index=index)
