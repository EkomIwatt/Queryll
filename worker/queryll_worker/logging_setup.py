"""Logging for a process whose inputs are private documents.

Contract 9: never log file bytes, extracted document text, embeddings, API keys or JWTs.
Document ids are fine. The helpers here exist so that "log the useful thing" is the easy
path and "log the document" is not something you reach for by accident.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Install a single stderr handler. Idempotent, so tests can call it freely."""
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(level)
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # pdfminer is extremely chatty at DEBUG and its debug output includes document text.
    logging.getLogger("pdfminer").setLevel(logging.WARNING)
    logging.getLogger("pdfplumber").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True


def kv(**fields: Any) -> str:
    """Render structured fields as a stable `key=value` suffix.

    Values are rendered with `repr` for strings so a stray newline in a value cannot forge a
    log line. Never pass document text, bytes or keys through here.
    """
    parts = []
    for key, value in fields.items():
        if value is None:
            rendered = "-"
        elif isinstance(value, str):
            rendered = repr(value) if (" " in value or "\n" in value) else value
        elif isinstance(value, float):
            rendered = f"{value:.4g}"
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")
    return " ".join(parts)
