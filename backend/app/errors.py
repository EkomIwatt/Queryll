"""The error envelope (Contract 9).

Every non-2xx HTTP response from every endpoint is `{"error": "<sentence>"}`.
`error` is always ONE human-readable sentence, safe to render directly in the UI —
no stack traces, no SQL, no provider payloads, no key fragments, ever.

Errors raised *inside* an already-open SSE stream do not use this envelope; the HTTP
status is already 200 by then, so they go out as an `error` event (see app/sse.py).
"""

from typing import Any, Dict, Optional


class AppError(Exception):
    """Base class for every error that maps to an HTTP status and a safe sentence."""

    status_code: int = 500
    message: str = "Something went wrong. Please try again."
    # `code` is only used when the failure is reported inside an SSE stream.
    code: str = "internal_error"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
    ) -> None:
        if message is not None:
            self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        super().__init__(self.message)

    def envelope(self) -> Dict[str, Any]:
        return {"error": self.message}


class ValidationError(AppError):
    status_code = 422
    message = "The request could not be processed as sent."
    code = "validation_error"


class UnauthorizedError(AppError):
    status_code = 401
    message = "Your session has expired. Please sign in again."
    code = "unauthorized"


class NotFoundError(AppError):
    """404, never 403.

    Ownership must not be discoverable by probing ids, so a resource belonging to
    another user is indistinguishable from one that does not exist.
    """

    status_code = 404
    message = "That was not found."
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    message = "That document is being processed right now. Try again in a moment."
    code = "conflict"


class PayloadTooLargeError(AppError):
    status_code = 413
    message = "That file is larger than the 20 MB limit."
    code = "payload_too_large"


class UnsupportedMediaTypeError(AppError):
    status_code = 415
    message = "Queryll can read PDF, plain text and Markdown files only."
    code = "unsupported_media_type"


class EmbeddingUnavailableError(AppError):
    """Voyage unavailable at query time (Contract 9)."""

    status_code = 503
    message = "Search is temporarily unavailable. Try again in a moment."
    code = "retrieval_unavailable"


class AnswerUnavailableError(AppError):
    """An Anthropic failure.

    502 when it happens BEFORE the stream opens; the same object is reused to build
    the in-stream `error` event when it happens after.
    """

    status_code = 502
    message = "The answer service is temporarily unavailable. Try again in a moment."
    code = "answer_unavailable"
