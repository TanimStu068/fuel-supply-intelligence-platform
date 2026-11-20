"""Structured JSON logging with correlation IDs.

Every integration action (a simulator call, a sync cycle, an SSE event) gets a
correlation ID that is carried through every log line it produces. That makes
a single sync cycle reconstructable from the logs alone, which is the point of
the brief's "logs must cover important actions, integration failures,
decision events and recoveries".

Two pieces:

* :func:`configure_logging` installs a JSON formatter on the root logger.
* :func:`new_correlation_id` / :func:`bind_correlation_id` / :func:`current_correlation_id`
  manage the per-task correlation ID through a :class:`contextvars.ContextVar`,
  so concurrent tasks never overwrite each other's ID.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any

#: Correlation ID of the unit of work currently being processed.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

#: Standard log record attributes that are not part of the JSON payload.
_RESERVED_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def new_correlation_id(prefix: str = "cid") -> str:
    """Return a fresh correlation ID."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind a correlation ID to the current context and return it.

    Callers that need to restore the previous value should keep the token
    returned by :func:`contextvars.ContextVar.set`; this helper is the simple
    "one scope, one ID" case used by the sync service and the request handler.
    """
    correlation_id = correlation_id or new_correlation_id()
    _correlation_id.set(correlation_id)
    return correlation_id


def current_correlation_id() -> str | None:
    """Return the correlation ID bound to the current context, if any."""
    return _correlation_id.get()


def clear_correlation_id() -> None:
    """Clear the correlation ID bound to the current context."""
    _correlation_id.set(None)


class CorrelationIdFilter(logging.Filter):
    """Attach the current correlation ID to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = current_correlation_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON object.

    Anything passed through ``extra=`` becomes a top-level key, so a log line
    is self-describing without a second parsing step.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_FIELDS or key.startswith("_"):
                continue
            if key == "correlation_id":
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, sort_keys=False)


def configure_logging(level: str | None = None) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: calling it twice (for example in the FastAPI lifespan and in
    a test) replaces the handler rather than stacking duplicates.
    """
    resolved_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationIdFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    # uvicorn installs its own handlers; route them through ours so the whole
    # container emits one log format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
