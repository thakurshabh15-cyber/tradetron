"""Structured logging configuration.

Produces single-line JSON log output in production and human-readable output
for development.  Every production log line carries ``timestamp`` (RFC3339
UTC), ``level``, ``logger``, and ``message`` fields so downstream log
aggregators can index them, plus any extra context attached by the caller.

P2-3 hardening:
- the JSON-in-production behaviour documented by the module docstring is now
  actually implemented;
- ``setup_logging()`` is idempotent — handlers installed by earlier calls are
  removed first, so uvicorn ``--reload`` or a second setup never double-logs.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.config import settings

#: Attribute marker placed on handlers created here so repeated
#: ``setup_logging()`` calls (uvicorn reload, webhook main) never stack
#: duplicate handlers.
_OWNED_HANDLER_ATTR = "_tradetron_owned_handler"

#: Stdlib ``LogRecord`` attributes we never forward into the JSON payload.
_STDLIB_RECORD_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "msecs", "module", "msg", "name", "pathname",
    "process", "processName", "relativeCreated", "stack_info", "taskName", "thread",
    "threadName",
}


class JsonLogFormatter(logging.Formatter):
    """Deterministic single-line JSON formatter for production logs.

    Emits one JSON object per record: ``timestamp``, ``level``, ``logger``,
    ``message`` and — when present — ``exc_info`` plus any non-``LogRecord``
    extra attributes supplied by the caller (e.g. ``extra={"user_id": 1}``).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _STDLIB_RECORD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def _build_formatter() -> logging.Formatter:
    """JSON formatter in production; the human-readable formatter otherwise."""
    if settings.environment == "production":
        return JsonLogFormatter()
    return logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def setup_logging() -> None:
    """Configure the root logger for the application.

    Idempotent: any handler installed by a previous ``setup_logging()`` call is
    removed first (by attribute marker), so it is safe to call again on process
    reload or from a second entrypoint.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _OWNED_HANDLER_ATTR, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _OWNED_HANDLER_ATTR, True)
    handler.setFormatter(_build_formatter())

    root.setLevel(level)
    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger scoped to the application."""
    return logging.getLogger(f"tradetron.{name}")
