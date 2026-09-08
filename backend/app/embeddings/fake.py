"""A deterministic fake embedder for tests (Contract 4, test-suite rule).

NO TEST IN THIS SUITE MAY CALL THE LIVE VOYAGE API. Both instances use a fake built the
same way -- seeded hash -> `EMBEDDING_DIM` floats -> L2-normalize -- so the two fixture
worlds are at least generated alike, and so the fake satisfies the same two assertions
the real client's output must satisfy.

The real client is proven exactly once, at merge, by the cross-process cosine probe.
"""

import hashlib
import math
from typing import List, Optional

from app.config import get_settings
from app.embeddings.base import EmbeddingClient, assert_valid_vector

_BLOCK = hashlib.sha256().digest_size  # 32 bytes -> 16 uint16 values per block


def fake_embedding(text: str, *, dim: Optional[int] = None) -> List[float]:
    """Map a string to a stable unit vector. Same text in, same vector out, forever."""
    if dim is None:
        dim = get_settings().embedding_dim

    seed = text.encode("utf-8")
    values: List[float] = []
    counter = 0
    while len(values) < dim:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        for i in range(0, _BLOCK, 2):
            if len(values) >= dim:
                break
            raw = int.from_bytes(block[i : i + 2], "big")  # 0 .. 65535
            values.append(raw / 32767.5 - 1.0)  # -1.0 .. 1.0
        counter += 1

    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:  # unreachable in practice; a zero vector has no direction
        values[0] = 1.0
        norm = 1.0
    return [v / norm for v in values]


class FakeEmbeddingClient(EmbeddingClient):
    """Drop-in replacement for the Voyage client, for tests and offline development.

    It records what it was asked so a test can assert the query text reached it, and it
    can be told to fail so the 503 path (Contract 9) is exercised without a network.
    """

    def __init__(self, *, fail_with: Optional[Exception] = None) -> None:
        self.calls: List[str] = []
        self.fail_with = fail_with

    async def embed_query(self, text: str) -> List[float]:
        self.calls.append(text)
        if self.fail_with is not None:
            raise self.fail_with
        return assert_valid_vector(fake_embedding(text), where="fake query embedding")

    async def aclose(self) -> None:
        return None
