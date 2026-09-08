"""Contract 4 — the contract that fails silently.

Nothing here calls the live Voyage API (Contract 4's test-suite rule); the real client is
exercised once, at merge, by the cross-process cosine probe. What *is* tested here is
everything that can be tested without it: that the client sends the frozen parameters, that it
reassembles responses by index rather than by list order, that it backs off on the statuses
the contract names, and above all that a vector of the wrong shape raises instead of being
stored.
"""

from __future__ import annotations

import json
import math
import random

import httpx
import pytest

from queryll_worker.config import (
    INPUT_TYPE_DOCUMENT,
    MAX_INPUTS_PER_REQUEST,
    MAX_TOKENS_PER_INPUT,
)
from queryll_worker.embeddings import FakeEmbedder, fake_vector
from queryll_worker.embeddings.base import check_batch, check_vector, l2_norm
from queryll_worker.embeddings.voyage import BACKOFF_SCHEDULE, VoyageEmbedder
from queryll_worker.errors import (
    EmbeddingContractError,
    PermanentIngestError,
    TransientIngestError,
)

DIM = 1024


def _unit(seed: int, dimension: int = DIM) -> list[float]:
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(dimension)]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


# --- the guard itself -------------------------------------------------------------------


def test_a_correct_vector_passes() -> None:
    check_vector(_unit(1), DIM)


def test_wrong_dimension_raises() -> None:
    with pytest.raises(EmbeddingContractError, match="expected 1024 dims, got 512"):
        check_vector(_unit(1, 512), DIM)


def test_unnormalised_vector_raises() -> None:
    scaled = [value * 1.5 for value in _unit(2)]
    with pytest.raises(EmbeddingContractError, match="not L2-normalized"):
        check_vector(scaled, DIM)


def test_a_short_batch_raises_rather_than_being_padded() -> None:
    with pytest.raises(EmbeddingContractError, match="expected 3 vectors, got 2"):
        check_batch([_unit(1), _unit(2)], DIM, 3)


def test_the_guard_survives_optimised_python() -> None:
    """It is written as an explicit raise, not `assert`, which `python -O` would strip."""
    source = (__import__("queryll_worker.embeddings.base", fromlist=["base"]).__file__)
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    assert "raise EmbeddingContractError" in body
    assert "\n    assert " not in body


# --- the deterministic fake ---------------------------------------------------------------


def test_fake_vectors_are_deterministic_and_normalised() -> None:
    first = fake_vector("the sampling frame")
    second = fake_vector("the sampling frame")
    assert first == second
    assert len(first) == DIM
    assert abs(l2_norm(first) - 1.0) < 1e-9


def test_fake_vectors_differ_between_texts() -> None:
    assert fake_vector("alpha") != fake_vector("beta")


async def test_fake_embedder_preserves_order_and_records_batches() -> None:
    embedder = FakeEmbedder()
    texts = ["alpha", "beta", "gamma"]
    vectors = await embedder.embed_documents(texts)
    assert vectors == [fake_vector(text) for text in texts]
    assert embedder.calls == [texts]


# --- the real client, against a mock transport ---------------------------------------------


def _response(count: int, *, shuffled: bool = False, dimension: int = DIM) -> httpx.Response:
    data = [
        {"object": "embedding", "index": index, "embedding": _unit(index, dimension)}
        for index in range(count)
    ]
    if shuffled:
        data = list(reversed(data))
    return httpx.Response(200, json={"object": "list", "data": data, "model": "voyage-4"})


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _noop_sleep(_delay: float) -> None:
    return None


def _embedder(handler, **kwargs) -> VoyageEmbedder:  # type: ignore[no-untyped-def]
    return VoyageEmbedder(
        "test-key",
        model="voyage-4",
        dimension=DIM,
        client=_client(handler),
        sleep=_noop_sleep,
        rng=random.Random(0),
        **kwargs,
    )


async def test_request_carries_the_frozen_contract_parameters() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return _response(2)

    embedder = _embedder(handler)
    await embedder.embed_documents(["one", "two"])
    await embedder.aclose()

    assert seen["model"] == "voyage-4"
    assert seen["input_type"] == INPUT_TYPE_DOCUMENT == "document"
    assert seen["output_dtype"] == "float"
    assert seen["input"] == ["one", "two"]
    assert seen["auth"] == "Bearer test-key"
    assert seen["url"] == "https://api.voyageai.com/v1/embeddings"


async def test_vectors_are_reassembled_by_index_not_by_list_order() -> None:
    """Trusting list order would pair a chunk's text with another chunk's vector."""
    embedder = _embedder(lambda _request: _response(3, shuffled=True))
    vectors = await embedder.embed_documents(["a", "b", "c"])
    await embedder.aclose()
    assert vectors == [_unit(0), _unit(1), _unit(2)]


async def test_a_wrong_width_response_raises_rather_than_being_stored() -> None:
    embedder = _embedder(lambda _request: _response(1, dimension=512))
    with pytest.raises(EmbeddingContractError):
        await embedder.embed_documents(["a"])
    await embedder.aclose()


async def test_an_unnormalised_response_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        vector = [value * 2.0 for value in _unit(0)]
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": vector}]}
        )

    embedder = _embedder(handler)
    with pytest.raises(EmbeddingContractError, match="not L2-normalized"):
        await embedder.embed_documents(["a"])
    await embedder.aclose()


async def test_rate_limits_are_retried_with_the_contract_backoff() -> None:
    attempts: list[int] = []
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, json={"detail": "slow down"})
        return _response(1)

    async def record(delay: float) -> None:
        delays.append(delay)

    embedder = VoyageEmbedder(
        "test-key",
        model="voyage-4",
        dimension=DIM,
        client=_client(handler),
        sleep=record,
        rng=random.Random(0),
    )
    vectors = await embedder.embed_documents(["a"])
    await embedder.aclose()

    assert len(vectors) == 1
    assert len(attempts) == 3
    # 1s, 2s, 4s, 8s with a little jitter on top — never shorter than the schedule.
    assert delays[0] >= BACKOFF_SCHEDULE[0]
    assert delays[1] >= BACKOFF_SCHEDULE[1]


async def test_persistent_server_errors_surface_as_transient() -> None:
    embedder = _embedder(lambda _request: httpx.Response(503, text="unavailable"))
    with pytest.raises(TransientIngestError):
        await embedder.embed_documents(["a"])
    await embedder.aclose()


async def test_transport_failures_are_retried_then_surfaced() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("reset", request=request)

    embedder = _embedder(handler)
    with pytest.raises(TransientIngestError):
        await embedder.embed_documents(["a"])
    await embedder.aclose()
    assert len(calls) == len(BACKOFF_SCHEDULE) + 1


async def test_auth_failures_are_transient_so_documents_recover() -> None:
    """A bad key is an operator problem; it must not permanently fail every user's uploads."""
    embedder = _embedder(lambda _request: httpx.Response(401, json={"detail": "bad key"}))
    with pytest.raises(TransientIngestError):
        await embedder.embed_documents(["a"])
    await embedder.aclose()


async def test_a_rejected_input_type_escalates_instead_of_being_dropped() -> None:
    """Contract 4: dropping `input_type` on one side only is the divergence, not the fix."""
    embedder = _embedder(
        lambda _request: httpx.Response(
            400, json={"detail": "unknown parameter: input_type"}
        )
    )
    with pytest.raises(TransientIngestError) as error:
        await embedder.embed_documents(["a"])
    await embedder.aclose()
    assert "ESCALATE" in (error.value.detail or "")


async def test_other_client_errors_fail_the_document_permanently() -> None:
    embedder = _embedder(
        lambda _request: httpx.Response(400, json={"detail": "input too long"})
    )
    with pytest.raises(PermanentIngestError) as error:
        await embedder.embed_documents(["a"])
    await embedder.aclose()
    # User-facing copy, with nothing from the provider payload in it.
    assert "input too long" not in error.value.user_message
    assert error.value.user_message.endswith(".")


async def test_an_over_long_input_is_truncated_rather_than_dropped() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _response(1)

    embedder = _embedder(handler)
    await embedder.embed_documents(["word " * (MAX_TOKENS_PER_INPUT + 500)])
    await embedder.aclose()

    sent = seen["input"]
    assert isinstance(sent, list) and len(sent) == 1
    assert len(sent[0]) < len("word " * (MAX_TOKENS_PER_INPUT + 500))


async def test_an_over_large_batch_is_a_programming_error() -> None:
    embedder = _embedder(lambda _request: _response(1))
    with pytest.raises(ValueError, match="at most 128 inputs"):
        await embedder.embed_documents(["x"] * (MAX_INPUTS_PER_REQUEST + 1))
    await embedder.aclose()


async def test_an_empty_batch_makes_no_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    embedder = _embedder(handler)
    assert await embedder.embed_documents([]) == []
    await embedder.aclose()
