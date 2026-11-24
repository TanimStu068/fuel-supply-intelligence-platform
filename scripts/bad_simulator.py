"""A stub simulator that answers with a payload that violates the contract.

Used to prove that the platform *rejects* malformed simulator data rather
than storing it, and that the rejection raises an alert.

The response below is the same shape as a real ``GET /v1/instance`` body,
except that ``id`` is a string where the contract says an integer. The
platform's Pydantic models are strict, so the whole payload is refused.

Served on every path, so the backend's first read of the world fails on
the schema check rather than on a transport error.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

MALFORMED_INSTANCE = {
    "id": "not-an-int",
    "scenario_id": "baseline",
    "scenario_version": "1.0",
    "seed": 12345,
    "sim_time": "2026-01-01T00:00:00",
    "tick": 0,
    "tick_minutes": 15,
    "status": "RUNNING",
    "dispatch_capacity_per_tick": 12000,
}


class Handler(BaseHTTPRequestHandler):
    """Answer every read with the same malformed payload."""

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's naming
        body = json.dumps(MALFORMED_INSTANCE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Stay quiet — the container's log is not the point."""


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
