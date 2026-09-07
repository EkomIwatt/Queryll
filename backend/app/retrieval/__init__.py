"""Retrieval: query embedding in, ranked passages and Citations out."""

from app.retrieval.citations import (
    SNIPPET_MAX_CHARS,
    make_citations,
    make_snippet,
    preview_of,
)
from app.retrieval.types import RetrievedPassage

__all__ = [
    "RetrievedPassage",
    "SNIPPET_MAX_CHARS",
    "make_citations",
    "make_snippet",
    "preview_of",
]
