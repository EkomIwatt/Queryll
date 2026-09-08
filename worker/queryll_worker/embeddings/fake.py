"""The deterministic fake embedder.

Contract 4's test-suite rule: no test may call the live Voyage API. Both Python instances use
a deterministic fake embedder instead — seeded hash, 1024 floats, L2-normalized — so that the
two fixture worlds are at least *generated* the same way even though the two implementations
were written independently.

**The algorithm is part of the cross-instance contract, so it is spelled out here:**

1. `seed = int.from_bytes(sha256(text.encode("utf-8")).digest()[:8], "big")`
2. `rng = random.Random(seed)`
3. `raw = [rng.gauss(0.0, 1.0) for _ in range(dimension)]`
4. `vector = [x / l2_norm(raw) for x in raw]`

Vectors from this are stable across processes, platforms and Python versions (`random.Random`
with an integer seed and `gauss` are specified, not implementation-defined), which is what
lets a fixture written today still retrieve the same way tomorrow.

It is *not* semantically meaningful: two paraphrases of the same sentence get unrelated
vectors. That is deliberate — it makes fake-embedding tests test plumbing, and keeps anyone
from mistaking a green test suite for evidence about retrieval quality.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence

from queryll_worker.config import DEFAULT_EMBEDDING_DIM
from queryll_worker.embeddings.base import check_batch, l2_norm


def fake_vector(text: str, dimension: int = DEFAULT_EMBEDDING_DIM) -> list[float]:
    """A deterministic unit vector for `text`."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(dimension)]
    norm = l2_norm(raw)
    if norm == 0.0:  # pragma: no cover - probability zero
        raw[0] = 1.0
        norm = 1.0
    return [component / norm for component in raw]


class FakeEmbedder:
    """An `Embedder` that never touches the network.

    Used by the whole test suite, and available in development via `EMBEDDING_PROVIDER=fake`
    so the worker can be run end to end without a Voyage key. It must never be selected in
    production: the vectors it produces are noise, and retrieval over them is meaningless.
    """

    def __init__(
        self,
        *,
        model: str = "fake",
        dimension: int = DEFAULT_EMBEDDING_DIM,
    ) -> None:
        self._model = model
        self._dimension = dimension
        #: Every batch this embedder was asked for, for tests that assert on batching.
        self.calls: list[list[str]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors = [fake_vector(text, self._dimension) for text in texts]
        # The fake goes through the same Contract 4 check as the real client, so a test that
        # passes here is testing the same guard the production path uses.
        check_batch(vectors, self._dimension, len(texts))
        return vectors

    async def aclose(self) -> None:
        return None
