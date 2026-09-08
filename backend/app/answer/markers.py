"""Citation marker validation (Contract 7 §7). Pure -- no database, no network.

Two jobs, both of which have to happen BEFORE a `token` event leaves the process:

1. **Validate.** A `[n]` whose n is outside `1..len(sources)` is STRIPPED from the text.
   It is not passed through, and it is not rendered as a broken link. A model that
   invents source [9] when eight were sent must not be able to put a dead chip in front
   of a user.

2. **Buffer across chunk boundaries.** Claude streams text in arbitrary slices, so `[`
   and `2]` routinely arrive in different chunks. Emitting the first half immediately
   would put a literal `[` on screen a beat before its number. So a trailing partial
   marker is held back until the chunk that completes it arrives.

One normalization is applied on the way out: a grouped marker like `[1, 2]` -- which
models emit despite being told not to -- is rewritten to `[1][2]`. Nothing but
contract-shaped single `[n]` markers ever reaches the client, which is the property
Instance 3 renders against.

`citations_used` falls out of this for free: an index is "used" only if a marker
carrying it actually survived validation into the emitted text.
"""

import re
from typing import List, Set

# A complete marker: [3] or [1, 2]. Digits, commas and whitespace only.
_MARKER_RE = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")

# A trailing partial marker: an unclosed "[" with nothing but digits/commas/space after.
# Any other character rules out a marker, so ordinary prose containing "[" is not held.
_PARTIAL_TAIL_RE = re.compile(r"\[[\d,\s]*$")

# A partial marker cannot legitimately grow past a few characters. Beyond this, whatever
# is buffered is prose, and holding it would stall the stream.
MAX_PARTIAL_TAIL = 32


class MarkerFilter:
    """Streaming filter. Feed it text chunks; it returns text safe to emit."""

    def __init__(self, source_count: int) -> None:
        if source_count < 0:
            raise ValueError("source_count cannot be negative")
        self.source_count = source_count
        self._buffer = ""
        self._used: Set[int] = set()

    # -- streaming interface -------------------------------------------------

    def feed(self, text: str) -> str:
        """Consume a stream chunk; return the portion that is safe to send now."""
        if not text:
            return ""
        self._buffer += text

        match = _PARTIAL_TAIL_RE.search(self._buffer)
        if match and len(self._buffer) - match.start() <= MAX_PARTIAL_TAIL:
            safe = self._buffer[: match.start()]
            self._buffer = self._buffer[match.start() :]
        else:
            safe = self._buffer
            self._buffer = ""

        return self._apply(safe)

    def flush(self) -> str:
        """End of stream. Emit what is left, dropping any never-completed marker."""
        remaining = self._buffer
        self._buffer = ""
        tail = _PARTIAL_TAIL_RE.search(remaining)
        if tail:
            # "...as shown in [1" -- the closing bracket never came. A half-marker is
            # never emitted, so the fragment goes rather than showing as literal text.
            remaining = remaining[: tail.start()]
        return self._apply(remaining)

    # -- results -------------------------------------------------------------

    @property
    def citations_used(self) -> List[int]:
        """1-based indices actually referenced in the final text, ascending."""
        return sorted(self._used)

    # -- internals -----------------------------------------------------------

    def _apply(self, text: str) -> str:
        if not text:
            return ""
        return _MARKER_RE.sub(self._replace, text)

    def _replace(self, match: "re.Match") -> str:
        indices = [int(part.strip()) for part in match.group(1).split(",")]
        kept = [i for i in indices if 1 <= i <= self.source_count]
        self._used.update(kept)
        return "".join("[" + str(i) + "]" for i in kept)


def validate_markers(text: str, source_count: int) -> "tuple":
    """Non-streaming convenience: returns (clean_text, citations_used)."""
    flt = MarkerFilter(source_count)
    out = flt.feed(text) + flt.flush()
    return out, flt.citations_used
