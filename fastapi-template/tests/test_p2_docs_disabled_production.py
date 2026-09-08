"""P2 — interactive API docs are disabled when ENVIRONMENT=production.

Exposing ``/docs``, ``/redoc`` and ``/openapi.json`` in production hands an
attacker a complete, self-describing inventory of every endpoint, parameter
name and response schema.  Both the main API (``app.main``) and the webhook
platform (``app.webhooks.main``) must serve ``docs_url=None`` /
``redoc_url=None`` / ``openapi_url=None`` when ``ENVIRONMENT=production``,
while development/testing keep them enabled for convenience.

The FastAPI instances are constructed at module import time, so this runs in a
subprocess (the same pattern ``test_p0_remediation.py`` uses for the
production boot guards).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(code: str, env_overrides: dict[str, str]):
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        **env_overrides,
    }
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )


_PRODUCTION_ENV = {
    "ENVIRONMENT": "production",
    # URL fixtures are assembled from parts so this module's own source never
    # literally contains the ``user:pass@host`` credential patterns the P0
    # scanner is designed to detect in tracked source (same convention as
    # ``test_p0_remediation.py``).
    "JWT_SECRET": "j" * 40,
    "DATABASE_URL": "postgresql://" + "u:test_password" + "@db.example.internal/tradetron",
    "UPSTASH_REDIS_URL": "rediss://" + "u:test_password" + "@redis.example.internal:6379/0",
    "REDIS_URL": "",
    "BROKER_MODE": "simulated",
    "WEBHOOK_LOCAL_MODE": "false",
}


def test_main_and_webhook_apps_hide_docs_in_production():
    code = (
        "from app.config import settings;"
        "assert settings.environment == 'production';"
        "from app.main import app;"
        "assert app.docs_url is None, app.docs_url;"
        "assert app.redoc_url is None, app.redoc_url;"
        "assert app.openapi_url is None, app.openapi_url;"
        "from app.webhooks.main import app as webhook_app;"
        "assert webhook_app.docs_url is None, webhook_app.docs_url;"
        "assert webhook_app.redoc_url is None, webhook_app.redoc_url;"
        "assert webhook_app.openapi_url is None, webhook_app.openapi_url;"
        "print('DOCS_DISABLED')"
    )
    result = _run(code, _PRODUCTION_ENV)
    assert result.returncode == 0, "stdout=" + result.stdout + " stderr=" + result.stderr
    assert "DOCS_DISABLED" in result.stdout


def test_main_app_keeps_docs_in_development():
    code = (
        "from app.main import app;"
        "assert app.docs_url == '/docs', app.docs_url;"
        "assert app.openapi_url == '/openapi.json', app.openapi_url;"
        "print('DOCS_ENABLED_DEV')"
    )
    result = _run(code, {"ENVIRONMENT": "development"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DOCS_ENABLED_DEV" in result.stdout