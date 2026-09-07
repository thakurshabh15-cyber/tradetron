"""Alembic migration runner used by application startup (migrate-before-serve).

Render Free provides no Pre-Deploy / Release command, so pending migrations
are applied here — inside the process that will serve traffic, using the exact
runtime environment (DATABASE_URL, python, project root) — BEFORE the FastAPI
lifespan is allowed to complete.  The uvicorn / FastAPI lifespan contract
guarantees no request is routed to the app until startup has finished, which
makes this block the fail-closed schema gate for production.

The migration is delegated to ``python -m alembic upgrade head`` as a
subprocess (the same invocation pattern as ``scripts/ci_alembic_check.py``)
rather than importing Alembic internals into the running event loop:
``alembic/env.py`` calls ``asyncio.run()`` which cannot execute from inside an
already-running event loop.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from app.config import BASE_DIR
from app.core.logging import get_logger

logger = get_logger("db.migrations")


class MigrationError(RuntimeError):
    """Raised when ``alembic upgrade head`` fails — startup must fail closed."""


# ---------------------------------------------------------------------------
# Credential redaction for safe logging of captured alembic output.
# ---------------------------------------------------------------------------
_CREDENTIAL_RE = re.compile(r"(://[^:/@\s]+:)[^@\s]*@")


def _redact(text: str) -> str:
    """Strip embedded credentials from alembic stderr/stdout before logging."""
    return _CREDENTIAL_RE.sub(r"\1***@", text)


def run_migrations() -> None:
    """Apply all pending Alembic migrations (``upgrade head``).

    Runs ``python -m alembic upgrade head`` from the project root with the
    current process environment so ``DATABASE_URL`` and every other runtime
    variable resolve exactly as they do for the running application.

    Raises:
        MigrationError: if the migration subprocess fails for any reason.
            The caller MUST propagate this — never swallow the exception.
    """
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        stderr_tail = _redact((result.stderr or "")[-3000:])
        stdout_tail = _redact((result.stdout or "")[-3000:])
        logger.critical(
            "alembic upgrade head FAILED rc=%s\n--- stderr ---\n%s\n--- stdout ---\n%s",
            result.returncode,
            stderr_tail,
            stdout_tail,
        )
        raise MigrationError(
            f"alembic upgrade head failed (rc={result.returncode}): "
            f"{stderr_tail.strip() or stdout_tail.strip() or 'no output'}"
        )

    logger.info("alembic upgrade head completed successfully.")
