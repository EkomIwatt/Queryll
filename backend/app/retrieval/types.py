"""The internal shape a retrieved passage has between SQL and the wire.

This is deliberately NOT `Citation`. A Citation carries a 300-character `snippet` for a
hover card; the model needs the whole passage. Keeping them separate is what stops the
snippet from quietly becoming the thing Claude actually reads.
"""

import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RetrievedPassage:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    ordinal: int
    text: str
    token_count: int
    page_start: Optional[int]
    page_end: Optional[int]
    heading_path: Optional[str]
    similarity: float  # 1.0 - cosine distance
