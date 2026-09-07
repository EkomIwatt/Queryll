"""Document parsing: bytes and a declared MIME type in, one canonical text out."""

from __future__ import annotations

from queryll_worker.errors import PermanentIngestError
from queryll_worker.parsing.base import (
    Block,
    BlockKind,
    ExtractedDocument,
    PageSpan,
    page_at,
    page_range,
)
from queryll_worker.parsing.pdf import PDF_MAGIC, extract_pdf
from queryll_worker.parsing.text import decode_text, extract_markdown, extract_plain_text

__all__ = [
    "Block",
    "BlockKind",
    "ExtractedDocument",
    "PageSpan",
    "extract",
    "extract_markdown",
    "extract_pdf",
    "extract_plain_text",
    "decode_text",
    "page_at",
    "page_range",
]


def extract(
    content: bytes, mime_type: str, *, max_pages: int
) -> ExtractedDocument:
    """Parse `content` according to `mime_type`, distrusting the declared type.

    Instance 2 sniffs content at upload and 415s anything unsupported, so a mismatch reaching
    here means the file lied convincingly. That is a permanent failure with copy the user can
    act on, not something worth three attempts.

    Raises:
        PermanentIngestError: empty, mistyped, unsupported, or unreadable content.
    """
    if not content:
        raise PermanentIngestError("This file is empty, so there was nothing to index.")

    declared = (mime_type or "").split(";", 1)[0].strip().lower()
    looks_like_pdf = content.lstrip()[:1024].startswith(PDF_MAGIC)

    if declared == "application/pdf":
        return extract_pdf(content, max_pages=max_pages)

    if declared in {"text/plain", "text/markdown"}:
        if looks_like_pdf:
            raise PermanentIngestError(
                "This file is a PDF but was uploaded as a text document, so it could not be "
                "read. Try uploading it again."
            )
        return (
            extract_markdown(content)
            if declared == "text/markdown"
            else extract_plain_text(content)
        )

    raise PermanentIngestError(
        "Queryll can only index PDF, plain text and Markdown documents."
    )
