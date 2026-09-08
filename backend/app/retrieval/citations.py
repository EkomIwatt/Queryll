"""Projecting Instance 1's chunk metadata into Contract 8 Citations. Pure functions.

`chunk.id` is the citation primary key. Citations reference chunks by id -- never by
page number, ordinal, text prefix, or similarity rank. Ordinals and pages are display
metadata, and a re-index changes chunk ids, which is exactly why citations are resolved
at answer time and never cached across one.
"""

from typing import List, Sequence

from app.schemas import Citation
from app.retrieval.types import RetrievedPassage

# Contract 8: snippet is <= 300 chars, for the inline hover card only. It is never the
# source of truth for the passage -- that is the chunk route, resolved by chunk_id.
SNIPPET_MAX_CHARS = 300

# Contract 6 §3: ChunkSummary.preview is the first 200 characters.
PREVIEW_MAX_CHARS = 200

_ELLIPSIS = "\u2026"


def _truncate(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[: limit - 1]
    boundary = cut.rfind(" ")
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip() + _ELLIPSIS


def make_snippet(text: str) -> str:
    return _truncate(text, SNIPPET_MAX_CHARS)


def preview_of(text: str) -> str:
    return _truncate(text, PREVIEW_MAX_CHARS)


def make_citations(passages: Sequence[RetrievedPassage]) -> List[Citation]:
    """Index is 1-based and positional: it is the [n] the model is told to cite with.

    It is meaningful only within the message it was sent with (Contract 8 §1), so it is
    assigned here, at answer time, from the order the passages were retrieved in.
    """
    return [
        Citation(
            index=position,
            chunk_id=str(passage.chunk_id),
            document_id=str(passage.document_id),
            filename=passage.filename,
            page_start=passage.page_start,
            page_end=passage.page_end,
            heading_path=passage.heading_path,
            similarity=round(passage.similarity, 3),
            snippet=make_snippet(passage.text),
        )
        for position, passage in enumerate(passages, start=1)
    ]
