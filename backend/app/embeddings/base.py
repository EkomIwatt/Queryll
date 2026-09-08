"""The frozen embedding invariants (Contract 4).

A vector is never trusted on shape alone. Every vector that enters this program is
checked for dimension and L2 norm at the boundary, and a failure RAISES -- it is never
warned about, never rounded off, and never stored or queried with.

Why this matters more than it looks: the worker embeds chunks and this API embeds
questions, in two separately-written clients. If those two diverge -- different model,
different dimension, a missing input_type -- nothing crashes. Retrieval quietly returns
near-random passages, Claude faithfully answers from them, and both test suites stay
green because each side is internally consistent. These checks are one of the three
layers that turn that silent failure into a loud one.

The contract writes these as bare `assert` statements. They are implemented here as
explicit raises instead, because `assert` is removed by `python -O` and this check must
survive being run in production.
"""

import math
from typing import List, Sequence

from app.config import get_settings

EMBEDDING_INPUT_TYPE_QUERY = "query"
EMBEDDING_INPUT_TYPE_DOCUMENT = "document"

# Contract 4: Voyage returns L2-normalized vectors. Store/query as returned.
NORM_TOLERANCE = 1e-3


class EmbeddingContractViolation(RuntimeError):
    """A vector violated Contract 4. Never recoverable -- never swallow this."""


def l2_norm(vec: Sequence[float]) -> float:
    return math.sqrt(sum(float(x) * float(x) for x in vec))


def assert_valid_vector(vec: Sequence[float], *, where: str = "embedding") -> List[float]:
    """Contract 4's two mandatory runtime assertions. Returns the vector as a list."""
    expected_dim = get_settings().embedding_dim

    if vec is None:
        raise EmbeddingContractViolation(f"{where}: no vector was returned")

    length = len(vec)
    if length != expected_dim:
        raise EmbeddingContractViolation(
            f"{where}: expected {expected_dim} dims, got {length}"
        )

    norm = l2_norm(vec)
    if not math.isfinite(norm) or abs(norm - 1.0) >= NORM_TOLERANCE:
        raise EmbeddingContractViolation(
            f"{where}: vector is not L2-normalized (norm={norm!r})"
        )

    return [float(x) for x in vec]


class EmbeddingClient:
    """The query-embedding interface the retrieval service depends on.

    Deliberately narrow: this instance embeds questions and nothing else. If you find
    yourself wanting to embed a chunk here, you are writing Instance 1's job.
    """

    async def embed_query(self, text: str) -> List[float]:  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover
        return None
