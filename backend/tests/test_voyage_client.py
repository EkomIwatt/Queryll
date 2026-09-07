"""Contract 4 -- the real Voyage query client, exercised without touching the network.

`httpx.MockTransport` stands in for api.voyageai.com, so the request this instance would
actually send is inspectable: the model, the dimension, and above all `input_type`.

That last one is the whole point. Instance 1 sends `input_type="document"` for chunks;
this instance sends `input_type="query"` for questions. If either side drops it, both
sides still work, both test suites still pass, and retrieval quietly stops meaning
anything. These tests pin this side of that asymmetry in place.

The real API is never called here. It is proven exactly once, at merge, by the
cross-process cosine probe.
"""

import json

import httpx
import pytest

from app.config import get_settings
from app.embeddings.base import EmbeddingContractViolation
from app.embeddings.fake import fake_embedding
from app.embeddings.voyage import VoyageEmbeddingClient, VoyageInputTypeRejected
from app.errors import EmbeddingUnavailableError


def voyage_response(vector=None, status=200, body=None):
    if body is None:
        body = {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": vector}],
            "model": "voyage-4",
        }
    return httpx.Response(status, json=body)


def client_with(handler, api_key="test-key"):
    settings = get_settings()
    settings.voyage_api_key = api_key
    transport = httpx.MockTransport(handler)
    return VoyageEmbeddingClient(client=httpx.AsyncClient(transport=transport))


@pytest.fixture(autouse=True)
def _restore_key():
    settings = get_settings()
    original = settings.voyage_api_key
    yield
    settings.voyage_api_key = original


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Backoff is 1s, 2s, 4s, 8s. Assert that it happens; do not sit through it."""
    import app.embeddings.voyage as module

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    return slept


class TestRequestShape:
    async def test_sends_input_type_query_the_model_and_the_output_dtype(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            seen["auth"] = request.headers.get("authorization")
            seen["url"] = str(request.url)
            return voyage_response(fake_embedding("q"))

        embedder = client_with(handler)
        await embedder.embed_query("what did the report conclude?")

        # The asymmetry Contract 4 exists to protect.
        assert seen["input_type"] == "query"
        assert seen["input_type"] != "document"
        assert seen["model"] == "voyage-4"
        assert seen["output_dtype"] == "float"
        assert seen["input"] == ["what did the report conclude?"]
        assert seen["url"] == "https://api.voyageai.com/v1/embeddings"
        assert seen["auth"] == "Bearer test-key"

    async def test_the_model_follows_the_configured_env_value(self):
        settings = get_settings()
        original = settings.embedding_model
        settings.embedding_model = "voyage-3.5"
        seen = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return voyage_response(fake_embedding("q"))

        try:
            await client_with(handler).embed_query("q")
            assert seen["model"] == "voyage-3.5"
        finally:
            settings.embedding_model = original

    async def test_an_absurdly_long_question_is_truncated_rather_than_dropped(self):
        seen = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return voyage_response(fake_embedding("q"))

        await client_with(handler).embed_query("word " * 40_000)
        assert len(seen["input"][0]) == 32_000 * 4


class TestResponseValidation:
    async def test_a_valid_vector_is_returned_as_given(self):
        vector = fake_embedding("the question")

        def handler(request):
            return voyage_response(vector)

        result = await client_with(handler).embed_query("the question")
        assert result == pytest.approx(vector)

    async def test_a_wrong_dimension_vector_raises_rather_than_being_used(self):
        # The silent-divergence failure: right-looking response, wrong-shaped vector.
        def handler(request):
            return voyage_response([0.1, 0.2, 0.3])

        with pytest.raises(EmbeddingContractViolation):
            await client_with(handler).embed_query("q")

    async def test_an_unnormalized_vector_raises(self):
        def handler(request):
            return voyage_response([0.5] * 1024)

        with pytest.raises(EmbeddingContractViolation):
            await client_with(handler).embed_query("q")

    async def test_a_malformed_response_body_raises(self):
        def handler(request):
            return voyage_response(body={"unexpected": "shape"})

        with pytest.raises(EmbeddingContractViolation):
            await client_with(handler).embed_query("q")


class TestInputTypeRejection:
    async def test_a_rejected_input_type_escalates_instead_of_retrying_without_it(self):
        """Contract 4 is explicit: if `input_type` is rejected, ESCALATE.

        Retrying without it would "work" -- and would silently decorrelate every query
        vector from every chunk vector the worker has ever written.
        """
        attempts = []

        def handler(request):
            attempts.append(json.loads(request.content))
            return httpx.Response(
                400, json={"detail": "unknown parameter: input_type"}
            )

        with pytest.raises(VoyageInputTypeRejected):
            await client_with(handler).embed_query("q")

        assert len(attempts) == 1
        assert attempts[0]["input_type"] == "query"

    async def test_an_ordinary_400_is_a_503_not_an_escalation(self):
        def handler(request):
            return httpx.Response(400, json={"detail": "malformed request"})

        with pytest.raises(EmbeddingUnavailableError):
            await client_with(handler).embed_query("q")


class TestRetriesAndFailure:
    async def test_a_429_is_retried_with_exponential_backoff_then_succeeds(
        self, _no_real_sleeping
    ):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, json={"detail": "slow down"})
            return voyage_response(fake_embedding("q"))

        result = await client_with(handler).embed_query("q")
        assert len(result) == 1024
        assert calls["n"] == 3
        # 1s then 2s, each with a little jitter on top.
        assert len(_no_real_sleeping) == 2
        assert 1.0 <= _no_real_sleeping[0] < 1.3
        assert 2.0 <= _no_real_sleeping[1] < 2.6

    async def test_a_500_is_retried(self, _no_real_sleeping):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, json={"detail": "upstream"})
            return voyage_response(fake_embedding("q"))

        await client_with(handler).embed_query("q")
        assert calls["n"] == 2

    async def test_persistent_failure_surfaces_as_the_contract_503_sentence(
        self, _no_real_sleeping
    ):
        def handler(request):
            return httpx.Response(429, json={"detail": "slow down"})

        with pytest.raises(EmbeddingUnavailableError) as caught:
            await client_with(handler).embed_query("q")

        assert caught.value.status_code == 503
        assert caught.value.message == (
            "Search is temporarily unavailable. Try again in a moment."
        )
        # 1s, 2s, 4s, 8s, then give up.
        assert len(_no_real_sleeping) == 4

    async def test_a_transport_error_is_retried_then_surfaces_as_503(
        self, _no_real_sleeping
    ):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        with pytest.raises(EmbeddingUnavailableError):
            await client_with(handler).embed_query("q")
        assert len(_no_real_sleeping) == 4

    async def test_a_missing_api_key_is_a_503_and_never_a_request(self):
        called = {"n": 0}

        def handler(request):
            called["n"] += 1
            return voyage_response(fake_embedding("q"))

        with pytest.raises(EmbeddingUnavailableError):
            await client_with(handler, api_key=None).embed_query("q")
        assert called["n"] == 0


class TestSecrecy:
    async def test_the_api_key_never_appears_in_a_raised_error(self):
        def handler(request):
            return httpx.Response(401, json={"detail": "bad key"})

        embedder = client_with(handler, api_key="sk-super-secret-value")
        with pytest.raises(EmbeddingUnavailableError) as caught:
            await embedder.embed_query("q")
        assert "sk-super-secret" not in str(caught.value)

    async def test_the_question_text_never_appears_in_a_raised_error(self):
        def handler(request):
            return voyage_response([0.0] * 10)

        with pytest.raises(EmbeddingContractViolation) as caught:
            await client_with(handler).embed_query("a private question about salaries")
        assert "salaries" not in str(caught.value)
