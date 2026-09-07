"""Opaque cursors for the paginated list routes (Contract 6 §2, §3).

The cursor is base64url of a small JSON object. It is opaque by contract -- the client
never parses it -- but it is deliberately not encrypted either: it carries only ordering
keys the caller already has, and every list query is still scoped to the caller's own
rows in SQL, so a forged cursor can move you around your own data and nowhere else.

A cursor that will not decode is a 422, not a 500 and not a silent reset to page one.
"""

import base64
import json
from typing import Any, Dict, Optional

from app.errors import ValidationError


def encode_cursor(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    if cursor is None or cursor == "":
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValidationError("That page link is no longer valid. Start again.")
    if not isinstance(payload, dict):
        raise ValidationError("That page link is no longer valid. Start again.")
    return payload


def clamp_limit(limit: Optional[int], *, default: int, maximum: int) -> int:
    if limit is None:
        return default
    if limit < 1:
        raise ValidationError("`limit` must be at least 1.")
    return min(limit, maximum)
