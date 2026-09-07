"""The Claude answer client (Contract 7).

The Anthropic SDK is imported INSIDE `_sdk()`, not at module import. Two reasons, one
practical and one structural: `anthropic>=1.0` requires Python >= 3.10 while the
development machine for this run has 3.9, and no test in this suite may call the live
Anthropic API, so nothing but a real deployment needs the package present at all. The
API boots, serves auth and documents, and runs its whole test suite without it.

Model is `claude-sonnet-5`, settled with the human in the stack table.
"""

import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from app.config import get_settings
from app.errors import AnswerUnavailableError

logger = logging.getLogger(__name__)


class AnswerClient:
    """Streams answer text. Yields plain text deltas -- markers are validated upstream."""

    async def stream_answer(
        self,
        *,
        system_blocks: Sequence[Dict[str, Any]],
        messages: Sequence[Dict[str, str]],
    ) -> AsyncIterator[str]:  # pragma: no cover - interface
        raise NotImplementedError
        yield ""  # pragma: no cover

    def is_configured(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class AnthropicAnswerClient(AnswerClient):
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = None

    def is_configured(self) -> bool:
        """Checked BEFORE the stream opens, so a missing key is a 502 with an envelope
        rather than an `error` event three frames into a 200 response."""
        return bool(self._settings.anthropic_api_key)

    @staticmethod
    def _sdk():
        try:
            import anthropic  # noqa: WPS433 - deliberately deferred
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise AnswerUnavailableError(
                "The answer service is not available on this server."
            ) from exc
        return anthropic

    def _ensure_client(self):
        if self._client is None:
            anthropic = self._sdk()
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key
            )
        return self._client

    def _request_kwargs(
        self,
        system_blocks: Sequence[Dict[str, Any]],
        messages: Sequence[Dict[str, str]],
    ) -> Dict[str, Any]:
        settings = self._settings
        kwargs: Dict[str, Any] = {
            "model": settings.answer_model,
            "max_tokens": settings.answer_max_tokens,
            "system": list(system_blocks),
            "messages": list(messages),
        }
        # Sonnet 5 accepts {"type": "disabled"}; "adaptive" is its only on-mode.
        # Grounded extraction over eight passages does not need reasoning, and the
        # product is judged on how fast the first token appears -- so thinking is off by
        # default and this is the knob for changing that decision under measurement.
        if settings.answer_thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}
        else:
            kwargs["thinking"] = {"type": "disabled"}
        if settings.answer_effort:
            kwargs["output_config"] = {"effort": settings.answer_effort}
        return kwargs

    async def stream_answer(
        self,
        *,
        system_blocks: Sequence[Dict[str, Any]],
        messages: Sequence[Dict[str, str]],
    ) -> AsyncIterator[str]:
        anthropic = self._sdk()
        client = self._ensure_client()
        kwargs = self._request_kwargs(system_blocks, messages)

        try:
            async with client.messages.stream(**kwargs) as stream:
                async for text_delta in stream.text_stream:
                    if text_delta:
                        yield text_delta
                final = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            # Status and request id are safe to log; the prompt and the answer are not.
            logger.error(
                "Anthropic returned %s (request_id=%s).",
                getattr(exc, "status_code", "?"),
                getattr(exc, "request_id", None),
            )
            raise AnswerUnavailableError()
        except anthropic.APIConnectionError:
            logger.error("Could not reach Anthropic.")
            raise AnswerUnavailableError()

        stop_reason = getattr(final, "stop_reason", None)
        if stop_reason == "refusal":
            logger.warning("Anthropic declined the request (stop_reason=refusal).")
            raise AnswerUnavailableError(
                "The answer service could not respond to that question."
            )

    async def aclose(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                await close()


class ScriptedAnswerClient(AnswerClient):
    """Test double. Replays scripted text deltas, optionally failing part-way through.

    Used by every stream test in this suite. No test calls the live Anthropic API.
    """

    def __init__(
        self,
        chunks: Optional[Sequence[str]] = None,
        *,
        fail_after: Optional[int] = None,
        error: Optional[Exception] = None,
        configured: bool = True,
    ) -> None:
        self.chunks: List[str] = list(chunks or [])
        self.fail_after = fail_after
        self.error = error or AnswerUnavailableError()
        self._configured = configured
        self.calls: List[Dict[str, Any]] = []

    def is_configured(self) -> bool:
        return self._configured

    async def stream_answer(
        self,
        *,
        system_blocks: Sequence[Dict[str, Any]],
        messages: Sequence[Dict[str, str]],
    ) -> AsyncIterator[str]:
        self.calls.append(
            {"system_blocks": list(system_blocks), "messages": list(messages)}
        )
        for position, chunk in enumerate(self.chunks):
            if self.fail_after is not None and position >= self.fail_after:
                raise self.error
            yield chunk
        if self.fail_after is not None and self.fail_after >= len(self.chunks):
            raise self.error
