"""Plain-text and Markdown extraction.

The invariant that matters for every format is the same one: a block's span must slice the
canonical text back to exactly the block's text, because `chunks.char_start`/`char_end` are
built from those spans and a citation viewer resolves passages by them.
"""

from __future__ import annotations

import pytest

from queryll_worker.errors import PermanentIngestError
from queryll_worker.parsing import extract, extract_markdown, extract_plain_text
from queryll_worker.parsing.base import BlockKind

MARKDOWN = """---
title: Notes
---

# Ingestion notes

An opening paragraph that runs
across two source lines.

## Chunking

Chunks are spans, never rebuilt strings.

- Headings start a new chunk.
- Sentences are the smallest unit.

```python
def check(vector):
    assert len(vector) == 1024
```

Setext heading
--------------

A closing paragraph.
"""


def _blocks_slice_back(document) -> None:  # type: ignore[no-untyped-def]
    for block in document.blocks:
        assert document.text[block.char_start : block.char_end] == document.slice(block)
        assert block.char_end > block.char_start


def test_markdown_blocks_slice_back_to_the_canonical_text() -> None:
    document = extract_markdown(MARKDOWN.encode("utf-8"))
    _blocks_slice_back(document)


def test_markdown_canonical_text_is_the_file_itself() -> None:
    """No rewriting: an offset here is reproducible with `content.decode()[start:end]`."""
    document = extract_markdown(MARKDOWN.encode("utf-8"))
    assert document.text == MARKDOWN


def test_markdown_headings_carry_levels_and_labels() -> None:
    document = extract_markdown(MARKDOWN.encode("utf-8"))
    headings = [b for b in document.blocks if b.kind is BlockKind.HEADING]
    assert [(b.level, b.heading_text) for b in headings] == [
        (1, "Ingestion notes"),
        (2, "Chunking"),
        (2, "Setext heading"),
    ]


def test_front_matter_is_not_covered_by_any_block() -> None:
    """Metadata is not prose; nothing should ever cite it."""
    document = extract_markdown(MARKDOWN.encode("utf-8"))
    first = min(block.char_start for block in document.blocks)
    assert "title: Notes" not in document.text[:first] or first > MARKDOWN.index("# Ingestion")
    assert all(
        "title: Notes" not in document.slice(block) for block in document.blocks
    )


def test_fenced_code_is_one_preformatted_block() -> None:
    document = extract_markdown(MARKDOWN.encode("utf-8"))
    code = [b for b in document.blocks if b.kind is BlockKind.PREFORMATTED]
    assert len(code) == 1
    body = document.slice(code[0])
    assert body.startswith("```python")
    assert body.rstrip().endswith("```")


def test_each_list_item_is_its_own_block() -> None:
    """So a long list splits between items rather than inside one."""
    document = extract_markdown(MARKDOWN.encode("utf-8"))
    items = [
        document.slice(b)
        for b in document.blocks
        if document.slice(b).startswith("- ")
    ]
    assert items == ["- Headings start a new chunk.", "- Sentences are the smallest unit."]


def test_plain_text_splits_on_blank_lines_and_detects_no_headings() -> None:
    source = "First paragraph.\nStill the first.\n\nSecond paragraph.\n"
    document = extract_plain_text(source.encode("utf-8"))
    assert [document.slice(b) for b in document.blocks] == [
        "First paragraph.\nStill the first.",
        "Second paragraph.",
    ]
    # No reliable heading signal exists in a .txt file, so none is invented.
    assert all(block.kind is BlockKind.PARAGRAPH for block in document.blocks)


def test_text_formats_carry_no_pages() -> None:
    document = extract_plain_text(b"Only a paragraph.")
    assert document.pages == ()
    assert document.page_count is None


def test_windows_line_endings_are_normalised() -> None:
    document = extract_plain_text(b"One line.\r\nAnother line.\r\n")
    assert "\r" not in document.text


def test_latin1_bytes_still_decode() -> None:
    document = extract_plain_text("Café résumé".encode("cp1252"))
    assert "Caf" in document.text


def test_binary_content_is_a_permanent_failure() -> None:
    with pytest.raises(PermanentIngestError) as error:
        extract_plain_text(b"\x00\x01\x02binary")
    assert "binary" in error.value.user_message.lower()


def test_empty_document_is_a_permanent_failure() -> None:
    with pytest.raises(PermanentIngestError):
        extract_plain_text(b"   \n\n  \n")


def test_dispatch_rejects_a_pdf_uploaded_as_text() -> None:
    with pytest.raises(PermanentIngestError) as error:
        extract(b"%PDF-1.7\nnot really text", "text/plain", max_pages=500)
    assert "PDF" in error.value.user_message


def test_dispatch_rejects_an_unsupported_type() -> None:
    with pytest.raises(PermanentIngestError) as error:
        extract(b"some bytes", "application/zip", max_pages=500)
    assert "PDF, plain text and Markdown" in error.value.user_message


def test_dispatch_rejects_empty_content() -> None:
    with pytest.raises(PermanentIngestError) as error:
        extract(b"", "text/plain", max_pages=500)
    assert "empty" in error.value.user_message.lower()


def test_dispatch_honours_the_content_type_parameters() -> None:
    document = extract(b"# Title\n\nBody.\n", "text/markdown; charset=utf-8", max_pages=500)
    assert any(block.kind is BlockKind.HEADING for block in document.blocks)


def test_error_messages_never_leak_internals() -> None:
    """Contract 9: `error_message` is rendered verbatim in the UI."""
    with pytest.raises(PermanentIngestError) as error:
        extract_plain_text(b"\x00")
    message = error.value.user_message
    assert "Traceback" not in message
    assert "\\" not in message and "/" not in message
    assert message.endswith(".")
