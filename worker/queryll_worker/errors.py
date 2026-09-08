"""Ingestion failure taxonomy.

The distinction here drives Contract 3 §6: a *transient* failure is retried with backoff
inside a single run and, if it survives that, costs the job one attempt; a *permanent*
failure fails the document immediately without burning three attempts.

Every error carries ``user_message`` — user-facing copy that Instance 3 renders verbatim
from ``documents.error_message``. It is never a traceback, a provider payload, a file path
or anything containing a key (Contract 9).
"""

from __future__ import annotations

GENERIC_FAILURE_MESSAGE = (
    "We could not index this document. Try re-indexing it, and if it keeps failing the "
    "file may be damaged."
)


class IngestError(Exception):
    """Base class for failures raised while ingesting one document."""

    def __init__(self, user_message: str, *, detail: str | None = None) -> None:
        # `detail` is for logs only and must never reach the database or the client.
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail


class PermanentIngestError(IngestError):
    """The document can never be ingested. Fail it now; do not spend three attempts on it."""


class TransientIngestError(IngestError):
    """Something outside the document failed. Retry per Contract 3 §6."""

    def __init__(
        self,
        user_message: str = GENERIC_FAILURE_MESSAGE,
        *,
        detail: str | None = None,
    ) -> None:
        super().__init__(user_message, detail=detail)


class EmbeddingContractError(RuntimeError):
    """A vector violated Contract 4 (wrong dimension, or not L2-normalized).

    Deliberately *not* an ``IngestError``: this is not a bad document, it is the two-sided
    embedding contract being broken, and it is the failure mode this project exists to make
    loud. It must never be swallowed into a per-document "failed" state and it must never be
    stored or queried with.
    """
