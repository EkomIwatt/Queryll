"""The deterministic fake embedder for tests (Contract 4, test-suite rule).

NO TEST IN THIS SUITE MAY CALL THE LIVE VOYAGE API. Both instances use a fake instead, and
**the algorithm below is contract text, not an implementation detail** -- it is byte-identical
to `worker/queryll_worker/embeddings/fake.py`, on purpose.

Why that matters, and why it was not always true: this file originally used a different
(equally valid) seeded-hash construction. Both versions were deterministic, both returned
exactly `EMBEDDING_DIM` floats, and both were exactly L2-normalized -- so both satisfied every
assertion Contract 4 mandates, and neither test suite could possibly have noticed that the two
sides disagreed. They were in fact mutually orthogonal (measured cosine ~0.045), which meant
that chunks embedded by the real worker and questions embedded here landed in unrelated vector
spaces: retrieval returned nothing, every question fell below the similarity floor, and the
integrated app answered "I could not find anything about that in your documents" to everything
-- with all 441 tests green.

That was found at merge by the Reconciler and fixed by adopting Instance 1's algorithm here.
The lesson is written into Contract 4: when two independently-written programs must agree
about the meaning of a number, the *test double* is part of the interface too.

The algorithm, spelled out because it is shared:

1. `seed = int.from_bytes(sha256(text.encode("utf-8")).digest()[:8], "big")`
2. `rng = random.Random(seed)`
3. `raw = [rng.gauss(0.0, 1.0) for _ in range(dimension)]`
4. `vector = [x / l2_norm(raw) for x in raw]`

`random.Random` with an integer seed and `gauss` are specified rather than
implementation-defined, so these vectors are stable across processes, platforms and Python
versions. The fake is NOT semantically meaningful -- two paraphrases get unrelated vectors --
which is deliberate: it keeps a green suite from being mistaken for evidence about retrieval
quality. The real client is proven exactly once, at merge, by the cross-process cosine probe.
"""

import hashlib
import math
import random
from typing import List, Optional

from app.config import get_settings
from app.embeddings.base import EmbeddingClient, assert_valid_vector


def fake_embedding(text: str, *, dim: Optional[int] = None) -> List[float]:
    """Map a string to a stable unit vector. Same text in, same vector out, forever.

    Byte-identical in behaviour to the worker's `fake_vector`. Do not "improve" one side.
    """
    if dim is None:
        dim = get_settings().embedding_dim

    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    values = [rng.gauss(0.0, 1.0) for _ in range(dim)]

    norm = math.sqrt(math.fsum(v * v for v in values))
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
