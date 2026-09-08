"""Upload type detection by SNIFFING CONTENT (Contract 5 §1, Contract 6 §1).

The declared Content-Type and the filename extension are both attacker-controlled and
are used for nothing but the Markdown-vs-plain-text distinction, which is cosmetic.
What is actually stored in `documents.mime_type` -- and what the worker will parse the
bytes as -- is decided here, from the bytes.

Supported: application/pdf, text/plain, text/markdown. Everything else is a 415.
"""

from typing import Optional

PDF = "application/pdf"
TEXT_PLAIN = "text/plain"
TEXT_MARKDOWN = "text/markdown"

SUPPORTED = (PDF, TEXT_PLAIN, TEXT_MARKDOWN)

_PDF_MAGIC = b"%PDF-"
_MARKDOWN_EXTENSIONS = (".md", ".markdown", ".mdown", ".mkd")

# Enough to catch a PDF header and to judge whether the head of the file is text.
SNIFF_BYTES = 8192


def sniff_mime_type(head: bytes, filename: Optional[str] = None) -> Optional[str]:
    """Return the supported MIME type these bytes actually are, or None for a 415."""
    if not head:
        return None

    # A PDF may carry a short preamble before %PDF- in the wild; be slightly generous
    # but not open-ended, otherwise the marker could sit anywhere in a text file.
    if head[:1024].find(_PDF_MAGIC) != -1:
        return PDF

    if b"\x00" in head:
        return None  # binary, and not a PDF

    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # A truncated multi-byte character at the sniff boundary is not a failure.
        try:
            head[: max(0, len(head) - 4)].decode("utf-8")
        except UnicodeDecodeError:
            return None

    if filename:
        lowered = filename.lower()
        for extension in _MARKDOWN_EXTENSIONS:
            if lowered.endswith(extension):
                return TEXT_MARKDOWN

    return TEXT_PLAIN
