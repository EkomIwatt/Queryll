"""The real Voyage query-embedding client (Contract 4).

Frozen values, all read from the contract's env var names with the contract's defaults:

    provider      Voyage AI, https://api.voyageai.com/v1/embeddings
    model         EMBEDDING_MODEL, default "voyage-4"
    dimension     EMBEDDING_DIM,   default 1024
    input_type    "query"   <- the asymmetry; Instance 1 sends "document". Do not skip it.
    output_dtype  float
    normalization returned already L2-normalized; stored/queried as returned

Instance 1 has its own, separately written client that must agree with this one about
the meaning of a 1024-dimensional number. Nothing before merge can prove they do -- that
is what the merge-time cosine probe is for. What this file can do is fail loudly rather
than quietly whenever its own half of the deal is broken.

Never logged from here: the question text, the returned vector, or the API key.
"""

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings
from app.embeddings.base import (
    EMBEDDING_INPUT_TYPE_QUERY,
    EmbeddingClient,
    EmbeddingContractViolation,
    assert_valid_vector,
)
from app.errors import EmbeddingUnavailableError

logger = logging.getLogger(__name__)

# Contract 4: at most the model's 32,000-token context per input. A question will never
# come close, but truncate rather than drop if one somehow does -- and say so in the log
# without reproducing the text.
MAX_INPUT_CHARS = 32_000 * 4

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class VoyageInputTypeRejected(RuntimeError):
    """Voyage refused the `input_type` parameter.

    Contract 4 is explicit: if the parameter is rejected, ESCALATE -- do not silently
    drop it. Dropping it on one side only is exactly the asymmetry the contract exists
    to prevent, and it would be invisible to every test on both sides.
    """


class VoyageEmbeddingClient(EmbeddingClient):
    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.voyage_timeout_seconds
        )

    async def embed_query(self, text: str) -> List[float]:
        payload = self._build_payload(text)
        data = await self._post_with_backoff(payload)
        vector = self._extract_vector(data)
        # Contract 4: assert at the boundary where the vector enters the program.
        return assert_valid_vector(vector, where="Voyage query embedding")

    # -- request ------------------------------------------------------------

    def _build_payload(self, text: str) -> Dict[str, Any]:
        if len(text) > MAX_INPUT_CHARS:
            logger.warning(
                "Truncating an over-long query to %d characters before embedding "
                "(original length %d). Text not logged.",
                MAX_INPUT_CHARS,
                len(text),
            )
            text = text[:MAX_INPUT_CHARS]
        return {
            "input": [text],
            "model": self._settings.embedding_model,
            "input_type": EMBEDDING_INPUT_TYPE_QUERY,
            "output_dtype": "float",
        }

    async def _post_with_backoff(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        settings = self._settings
        if not settings.voyage_api_key:
            logger.error("VOYAGE_API_KEY is not configured; cannot embed the question.")
            raise EmbeddingUnavailableError()

        headers = {
            "Authorization": "Bearer " + settings.voyage_api_key,
            "Content-Type": "application/json",
        }

        last_status: Optional[int] = None
        # Exponential backoff with jitter: 1s, 2s, 4s, 8s, then surface the failure.
        for attempt in range(settings.voyage_max_retries + 1):
            try:
                response = await self._client.post(
                    settings.embedding_provider_url, json=payload, headers=headers
                )
            except httpx.HTTPError:
                last_status = None
                logger.warning(
                    "Voyage request failed at the transport layer (attempt %d).",
                    attempt + 1,
                )
            else:
                if response.status_code < 400:
                    return response.json()

                last_status = response.status_code
                if response.status_code == 400:
                    self._raise_if_input_type_rejected(response)
                if response.status_code not in _RETRYABLE_STATUS:
                    logger.error(
                        "Voyage rejected the embedding request with status %d.",
                        response.status_code,
                    )
                    raise EmbeddingUnavailableError()
                logger.warning(
                    "Voyage returned %d (attempt %d).", response.status_code, attempt + 1
                )

            if attempt < settings.voyage_max_retries:
                delay = (2 ** attempt) * (1.0 + random.random() * 0.25)
                await asyncio.sleep(delay)

        logger.error(
            "Voyage still unavailable after %d attempts (last status %s).",
            settings.voyage_max_retries + 1,
            last_status,
        )
        raise EmbeddingUnavailableError()

    def _raise_if_input_type_rejected(self, response: httpx.Response) -> None:
        try:
            body = response.text[:500].lower()
        except Exception:  # pragma: no cover - defensive
            return
        if "input_type" in body or "input type" in body:
            raise VoyageInputTypeRejected(
                "Voyage rejected the `input_type` parameter. Contract 4 requires an "
                "ESCALATION here, not a retry without it: dropping input_type on the "
                "query side alone would silently decorrelate every query vector from "
                "every stored chunk vector."
            )

    # -- response -----------------------------------------------------------

    @staticmethod
    def _extract_vector(data: Dict[str, Any]) -> List[float]:
        try:
            items = data["data"]
            vector = items[0]["embedding"]
        except (KeyError, IndexError, TypeError):
            raise EmbeddingContractViolation(
                "Voyage response did not contain an embedding in the expected shape"
            )
        if not isinstance(vector, list):
            raise EmbeddingContractViolation(
                "Voyage returned an embedding that is not a list of floats"
            )
        return vector

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
