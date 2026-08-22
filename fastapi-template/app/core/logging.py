"""Structured logging configuration.

Produces JSON-formatted log output in production and human-readable coloured
output for development.  Every log line carries ``module``, ``timestamp``, and
``level`` fields so downstream log aggregators can index them.
"""

from __future__ import annotations

import logging
import sys

from app.config import settings


def setup_logging() -> None:
    """Configure the root logger for the application."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers on reload
    if not root.handlers:
        root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger scoped to the application."""
    return logging.getLogger(f"tradetron.{name}")
