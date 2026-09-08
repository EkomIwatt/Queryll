"""SSE frame formatting (Contract 7 §2).

Standard SSE: `event: <name>\ndata: <json>\n\n`. Every `data` is a SINGLE-LINE JSON
object -- `json.dumps` escapes embedded newlines inside strings, so a token containing a
line break cannot split a frame in two and desynchronise the client's parser.

A comment heartbeat is sent while the model is thinking so an idle proxy does not close
the connection. Comments are ignored by every SSE parser, including the hand-rolled one
Instance 3 is writing.
"""

import json
from typing import Any, Dict

HEARTBEAT = ": ping\n\n"

EVENT_RETRIEVAL = "retrieval"
EVENT_TOKEN = "token"
EVENT_DONE = "done"
EVENT_ERROR = "error"


def frame(event: str, data: Dict[str, Any]) -> str:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return "event: " + event + "\ndata: " + payload + "\n\n"


# Headers that keep a stream a stream all the way to the browser. `X-Accel-Buffering`
# turns off nginx's response buffering, which is what silently converts a token stream
# into one burst at the end on a real deploy.
STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
