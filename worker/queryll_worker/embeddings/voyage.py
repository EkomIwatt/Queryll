"""The Voyage embeddings client — the worker's half of Contract 4.

This client only ever embeds with `input_type="document"`. The query half lives in the API
process and is written independently; the asymmetry between the two input types is the point,
and dropping it on one side only is precisely the divergence Contract 4 exists to prevent. If
the live API ever rejects the parameter, this client raises a loud, specific error rather than
retrying without it — silently degrading to a symmetric embedding would poison retrieval
while leaving every test green.

No test in this repository calls the live API (Contract 4's test-suite rule). The real client
is exercised exactly once, by the merge-time cross-process cosine probe.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Sequence

import httpx

from queryll_worker.config import (
    INPUT_TYPE_DOCUMENT,
    MAX_INPUTS_PER_REQUEST,
    MAX_TOKENS_PER_INPUT,
    OUTPUT_DTYPE,
    VOYAGE_API_URL,
)
from queryll_worker.chunking.tokenizer import count_tokens, truncate_to_tokens
from queryll_worker.embeddings.base import check_batch
from queryll_worker.errors import PermanentIngestError, TransientIngestError
from queryll_worker.logging_setup import kv

logger = logging.getLogger(__name__)

#: Contract 4: 1s, 2s, 4s, 8s, then surface the failure.
BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

SleepFn = Callable[[float], Awaitable[None]]


class VoyageEmbedder:
    """Embeds chunk texts through Voyage AI, with Contract 4 enforced on every response."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        dimension: int,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn | None = None,
        rng: random.Random | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("VOYAGE_API_KEY is required to embed with Voyage")
        self._api_key = api_key
        self._model = model
        self._dimension = dimension
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._sleep: SleepFn = sleep or asyncio.sleep
        # Jitter is randomised on purpose; it affects only *when* a retry happens, never what
        # is stored, so it cannot touch Contract 5 §4 determinism.
        self._rng = rng or random.Random()

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed one batch of chunk texts, in order.

        Raises:
            TransientIngestError: after the backoff schedule is exhausted.
            PermanentIngestError: on a request the API will never accept.
            EmbeddingContractError: if a returned vector is the wrong shape or not normalized.
        """
        if not texts:
            return []
        if len(texts) > MAX_INPUTS_PER_REQUEST:
            raise ValueError(
                f"at most {MAX_INPUTS_PER_REQUEST} inputs per request, got {len(texts)}"
            )

        payload_inputs = [self._fit(text, index) for index, text in enumerate(texts)]
        body = {
            "input": payload_inputs,
            "model": self._model,
            "input_type": INPUT_TYPE_DOCUMENT,
            "output_dtype": OUTPUT_DTYPE,
        }

        data = await self._post_with_retries(body)
        vectors = _vectors_in_order(data, len(texts))
        check_batch(vectors, self._dimension, len(texts))
        return vectors

    def _fit(self, text: str, index: int) -> str:
        """Truncate rather than drop an over-long input (Contract 4), and say so."""
        if count_tokens(text) <= MAX_TOKENS_PER_INPUT:
            return text
        logger.warning(
            "truncating over-long embedding input %s",
            kv(input_index=index, limit_tokens=MAX_TOKENS_PER_INPUT),
        )
        return truncate_to_tokens(text, MAX_TOKENS_PER_INPUT)

    async def _post_with_retries(self, body: dict[str, object]) -> dict[str, object]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last_detail = "no response"

        for attempt in range(len(BACKOFF_SCHEDULE) + 1):
            try:
                response = await self._client.post(
                    VOYAGE_API_URL, json=body, headers=headers
                )
            except httpx.HTTPError as exc:
                last_detail = f"transport: {type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return _decode(response)
                last_detail = f"http {response.status_code}"
                if response.status_code not in RETRYABLE_STATUSES:
                    self._raise_for_client_error(response)

            if attempt < len(BACKOFF_SCHEDULE):
                delay = BACKOFF_SCHEDULE[attempt] * (1.0 + self._rng.random() * 0.25)
                logger.warning(
                    "voyage request failed, backing off %s",
                    kv(attempt=attempt + 1, delay_s=delay, reason=last_detail),
                )
                await self._sleep(delay)

        raise TransientIngestError(detail=f"voyage unavailable after retries ({last_detail})")

    def _raise_for_client_error(self, response: httpx.Response) -> None:
        """Turn a non-retryable HTTP status into the right kind of failure."""
        status = response.status_code
        message = _error_message(response)

        if status in (401, 403):
            # An operator problem, not a document problem. Treated as transient so the
            # document recovers on its own once the key is fixed, and logged loudly because
            # nothing will get indexed until someone acts on it.
            logger.error(
                "voyage rejected our credentials %s", kv(status=status)
            )
            raise TransientIngestError(detail=f"voyage auth failure ({status})")

        if _mentions_input_type(message):
            # Contract 4: do not silently drop `input_type`. Dropping it on one side only is
            # exactly the asymmetry that makes retrieval quietly wrong.
            raise TransientIngestError(
                detail=(
                    "voyage rejected input_type — ESCALATE per Contract 4 rather than "
                    f"removing the parameter ({status}: {message})"
                )
            )

        raise PermanentIngestError(
            "We could not index this document because the indexing service rejected its "
            "text. The file may contain characters we cannot process.",
            detail=f"voyage {status}: {message}",
        )


def _decode(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TransientIngestError(detail="voyage returned a non-JSON body") from exc
    if not isinstance(payload, dict):
        raise TransientIngestError(detail="voyage returned an unexpected body")
    return payload


def _vectors_in_order(payload: dict[str, object], expected: int) -> list[list[float]]:
    """Read `data` back into request order.

    Voyage returns each embedding with its `index`; relying on list order instead would put a
    chunk's text next to another chunk's vector, which is unfindable after the fact.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        raise TransientIngestError(detail="voyage response had no data array")

    slots: list[list[float] | None] = [None] * expected
    for item in data:
        if not isinstance(item, dict):
            raise TransientIngestError(detail="voyage response item was not an object")
        index = item.get("index")
        vector = item.get("embedding")
        if not isinstance(index, int) or not 0 <= index < expected:
            raise TransientIngestError(detail="voyage response had an out-of-range index")
        if not isinstance(vector, list):
            raise TransientIngestError(detail="voyage response had no embedding")
        slots[index] = [float(component) for component in vector]

    if any(slot is None for slot in slots):
        raise TransientIngestError(detail="voyage response was missing an embedding")
    return [slot for slot in slots if slot is not None]


def _error_message(response: httpx.Response) -> str:
    """A short, log-safe description of an error body. Never rendered to a user."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error") or payload.get("message")
        if isinstance(detail, str):
            return detail[:200]
        if isinstance(detail, dict):
            inner = detail.get("message")
            if isinstance(inner, str):
                return inner[:200]
    return str(payload)[:200]


def _mentions_input_type(message: str) -> bool:
    lowered = message.lower()
    return "input_type" in lowered or "input type" in lowered
