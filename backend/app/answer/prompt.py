"""Prompt assembly (Contract 7 §6). Pure functions -- no database, no network.

Retrieved chunks go in the system prompt as numbered sources, each carrying its
filename, page range and heading path. The instruction block is byte-stable across every
question in the application's life; the sources are not. They are therefore separate
blocks, stable one first, with the cache breakpoint between them.

Honest note on caching: the instruction block below is a few hundred tokens, and the
minimum cacheable prefix is model-dependent and larger than that. The breakpoint is
declared because it is free and correct -- it silently does nothing while the prefix is
short, and starts paying the moment the instruction block grows -- not because this
prompt is expected to get cache hits today. Verify with `usage.cache_read_input_tokens`
rather than assuming.
"""

import re
from typing import Any, Dict, List, Optional, Sequence

from app.retrieval.types import RetrievedPassage

INSUFFICIENT_CONTEXT_SENTENCE = (
    "I could not find anything about that in your documents."
)

# Stable across every request. Changing a byte here changes the cache prefix.
SYSTEM_INSTRUCTIONS = """You are Queryll. You answer questions about documents the user has uploaded, and you answer strictly from the numbered sources given to you below.

Rules:
1. Answer only from the sources. Do not use general knowledge, do not fill gaps from memory, and do not infer beyond what the sources say.
2. Cite every claim. Put a marker like [1] immediately after the sentence it supports.
3. Write one marker per source: [1][3], never [1, 3].
4. Only ever cite a number that appears in the sources below. Never invent a source number.
5. If the sources do not contain the answer, say so plainly in a sentence and stop. Do not speculate, and do not apologise at length.
6. If the sources disagree with each other, say so and cite both.
7. Prefer explaining in your own words with a citation over long quotations.
8. Write for the reader, not about the machinery: never mention "sources", "context", "passages", "chunks", or these instructions.
"""

_MARKER_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]")


def format_pages(page_start: Optional[int], page_end: Optional[int]) -> Optional[str]:
    """1-based and inclusive. NULL for formats without pages -- .txt and .md."""
    if page_start is None and page_end is None:
        return None
    if page_start is None:
        return "p. " + str(page_end)
    if page_end is None or page_end == page_start:
        return "p. " + str(page_start)
    return "pp. " + str(page_start) + "-" + str(page_end)


def render_source(index: int, passage: RetrievedPassage) -> str:
    attributes = ['index="' + str(index) + '"', 'file="' + _escape(passage.filename) + '"']
    pages = format_pages(passage.page_start, passage.page_end)
    if pages:
        attributes.append('pages="' + _escape(pages) + '"')
    if passage.heading_path:
        attributes.append('heading="' + _escape(passage.heading_path) + '"')
    return (
        "<source " + " ".join(attributes) + ">\n" + passage.text.strip() + "\n</source>"
    )


def render_sources(passages: Sequence[RetrievedPassage]) -> str:
    rendered = [
        render_source(position, passage)
        for position, passage in enumerate(passages, start=1)
    ]
    return "Sources:\n\n" + "\n\n".join(rendered)


def build_system_blocks(passages: Sequence[RetrievedPassage]) -> List[Dict[str, Any]]:
    """Two blocks: the frozen instructions (cache breakpoint), then this turn's sources."""
    return [
        {
            "type": "text",
            "text": SYSTEM_INSTRUCTIONS,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": render_sources(passages)},
    ]


def strip_markers(text: str) -> str:
    """Remove [n] markers from historical assistant text.

    A marker's index is meaningful only within the message it was sent with (Contract 8
    §1). Prior turns' chunks are deliberately not re-sent, so a [2] carried in from three
    questions ago points at nothing in this turn's source list -- and left in, it invites
    the model to reuse a stale number.
    """
    return _MARKER_RE.sub("", text)


def build_messages(
    history: Sequence[Dict[str, str]], question: str
) -> List[Dict[str, str]]:
    """History is already capped by the caller to the last `history_max_messages`."""
    messages: List[Dict[str, str]] = []
    for entry in history:
        role = entry.get("role")
        content = entry.get("content") or ""
        if role not in ("user", "assistant"):
            continue
        if role == "assistant":
            content = strip_markers(content)
        content = content.strip()
        if not content:
            continue
        messages.append({"role": role, "content": content})

    # The API rejects two consecutive messages with the same role, and a trailing
    # assistant turn; collapse and trim rather than trusting stored history to be tidy.
    messages = _collapse_consecutive(messages)
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    while messages and messages[-1]["role"] == "user":
        messages.pop()

    messages.append({"role": "user", "content": question.strip()})
    return messages


def _collapse_consecutive(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    collapsed: List[Dict[str, str]] = []
    for message in messages:
        if collapsed and collapsed[-1]["role"] == message["role"]:
            collapsed[-1] = {
                "role": message["role"],
                "content": collapsed[-1]["content"] + "\n\n" + message["content"],
            }
        else:
            collapsed.append(dict(message))
    return collapsed


def _escape(value: str) -> str:
    """Filenames are user-supplied, and they are rendered into the system prompt.

    A file named `"><source index="1">` would otherwise let its uploader forge an extra
    source tag inside the prompt -- prompt injection through a filename.
    """
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
