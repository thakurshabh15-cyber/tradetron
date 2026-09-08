"""P2 — production CORS must never be unlocked to generic *.vercel.app hosts.

Regression target: the previous production CORS configuration registered a
``allow_origin_regex`` of ``https://([a-z0-9-]+\\.)*vercel\\.app`` on the main
API together with ``allow_credentials=True``.  Because ``vercel.app`` is a
public hosting surface, ANY Vercel deployment (e.g. ``https://evil.vercel.app``
or a prefix-squat like ``https://tradethrone-evil.vercel.app``) was treated as
a trusted origin and granted credentialed browser access to the API — a
standing cross-origin trust leak in the certificate of the "CORS locked to
production frontends" claim.

  - RED: ``https://evil.vercel.app`` passed the production preflight, and the
    production config leaked a generic Vercel regex.
  - GREEN: production uses exact origins only (no origin regex at all); any
    ``*.vercel.app`` host other than the two official exact origins is
    rejected, and specific Vercel preview origins must be operator-listed via
    ``ALLOWED_ORIGINS``.  Development keeps the broad rule for local workflow.

These tests never touch a broker or a real deployment.  The middleware probe
uses a purpose-built throwaway app so no production service is exercised.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

from app.core.cors import DEV_VERCEL_REGEX, build_cors_config


def test_development_regex_matches_all_vercel_hosts():
    """Development keeps the broad Vercel host space for local preview testing."""
    for origin in (
        "https://evil.vercel.app",
        "https://tradethrone.vercel.app",
        "https://tradethrone-git-main-abc123-team.vercel.app",
        "https://tradethrone-evil.vercel.app",
        "https://a.b.c.vercel.app",
    ):
        assert re.fullmatch(DEV_VERCEL_REGEX, origin), origin


def test_production_config_has_no_origin_regex():
    """regex == None means Starlette falls back to exact-origin matching only."""
    cfg = build_cors_config(
        environment="production",
        allowed_origins="",
        frontend_url="https://tradethrone.vercel.app",
    )
    assert cfg.origin_regex is None


def test_production_config_never_wildcard_origin_with_credentials():
    cfg = build_cors_config(
        environment="production",
        allowed_origins="",
        frontend_url="https://tradethrone.vercel.app",
    )
    assert "*" not in cfg.origins, "a wildcard origin must never be combined with credentials"


def test_development_config_stays_permissive():
    cfg = build_cors_config(environment="development", allowed_origins="*", frontend_url="")
    assert cfg.origins == ["*"]


def test_explicit_allowed_origins_are_merged_with_frontend_url():
    cfg = build_cors_config(
        environment="production",
        allowed_origins="https://custom.example.com",
        frontend_url="https://tradethrone.vercel.app",
    )
    assert "https://custom.example.com" in cfg.origins
    assert "https://tradethrone.vercel.app" in cfg.origins
    assert "*" not in cfg.origins


def _preflight_probe(
    origin: str,
    *,
    production: bool,
    allowed_origins: str | None = None,
    frontend_url: str | None = None,
) -> bool:
    """Real CORSMiddleware preflight against the builder's config.

    Returns True when the middleware echoes the caller's origin back as
    ``access-control-allow-origin`` (i.e. credentialed access is granted).
    """
    if allowed_origins is None:
        allowed_origins = (
            "https://tradethrone.vercel.app,https://tradethron.vercel.app"
            if production
            else "*"
        )
    if frontend_url is None:
        frontend_url = "https://tradethrone.vercel.app" if production else ""
    cfg = build_cors_config(
        environment="production" if production else "development",
        allowed_origins=allowed_origins,
        frontend_url=frontend_url,
    )

    probe = FastAPI()
    probe.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.origins,
        allow_origin_regex=cfg.origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @probe.get("/probe")
    async def _probe():
        return {"ok": True}

    with TestClient(probe) as client:
        resp = client.options(
            "/probe",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        return resp.headers.get("access-control-allow-origin") == origin


def test_production_preflight_rejects_attacker_vercel_page():
    assert _preflight_probe("https://evil.vercel.app", production=True) is False


def test_production_preflight_rejects_prefix_squat_vercel_page():
    # Any *.vercel.app host not in the exact allow-list must be rejected —
    # including one that starts with the official project slug.
    assert _preflight_probe("https://tradethrone-evil.vercel.app", production=True) is False


def test_production_preflight_rejects_unlisted_official_preview_subdomain():
    # Official previews are NOT auto-trusted in production; the operator lists
    # the exact URL in ALLOWED_ORIGINS when a branch preview must reach the API.
    assert (
        _preflight_probe("https://tradethrone-git-main-abc123-team.vercel.app", production=True)
        is False
    )


def test_production_preflight_allows_official_vercel_origin():
    assert _preflight_probe("https://tradethrone.vercel.app", production=True) is True


def test_production_preflight_allows_second_official_vercel_origin():
    assert _preflight_probe("https://tradethron.vercel.app", production=True) is True


def test_production_preflight_allows_localhost_dev_frontend():
    # With BOTH ALLOWED_ORIGINS and FRONTEND_URL unset the deployment falls
    # back to the default exact-origin lock, which deliberately keeps the
    # operator's own localhost dev frontends working.
    assert (
        _preflight_probe(
            "http://localhost:5173", production=True, allowed_origins="", frontend_url=""
        )
        is True
    )


def test_development_preflight_allows_any_vercel_host():
    assert _preflight_probe("https://evil.vercel.app", production=False) is True


# ── Webhook platform shares the production exact-origin lock ────────────────
# apps are constructed at import time, so this probe runs in a subprocess (the
# same pattern test_p2_docs_disabled_production.py uses).

_REPO_ROOT = Path(__file__).resolve().parent.parent

_WEBHOOK_PRODUCTION_ENV = {
    "ENVIRONMENT": "production",
    # URL fixtures are assembled from parts so this module's own source never
    # literally contains the ``user:pass@host`` credential patterns the P0
    # scanner detects in tracked source (same convention as test_p0_remediation.py).
    "JWT_SECRET": "j" * 40,
    "DATABASE_URL": "postgresql://" + "u:test_password" + "@db.example.internal/tradetron",
    "UPSTASH_REDIS_URL": "rediss://" + "u:test_password" + "@redis.example.internal:6379/0",
    "REDIS_URL": "",
    "BROKER_MODE": "simulated",
    "WEBHOOK_LOCAL_MODE": "false",
}


def _webhook_preflight_echoes(origin: str) -> bool:
    code = (
        "from app.webhooks.main import app;"
        "from starlette.testclient import TestClient;"
        "c = TestClient(app);"
        "r = c.options('/webhooks/razorpay', headers={"
        "'Origin': '" + origin + "',"
        "'Access-Control-Request-Method': 'POST'});"
        "print('ACAO=' + (r.headers.get('access-control-allow-origin') or 'NONE'))"
    )
    env = {**os.environ, "PYTHONUTF8": "1", **{k: v for k, v in _WEBHOOK_PRODUCTION_ENV.items()}}
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    assert result.returncode == 0, "stdout=" + result.stdout + " stderr=" + result.stderr
    return "ACAO=" + origin in result.stdout


def test_webhook_platform_rejects_prefix_squat_vercel_origin_in_production():
    # The webhook platform must no longer trust ANY *.vercel.app host in
    # production — a prefix-squat page must be denied credentialed access.
    assert _webhook_preflight_echoes("https://tradethrone-evil.vercel.app") is False


def test_webhook_platform_allows_official_vercel_origin_in_production():
    assert _webhook_preflight_echoes("https://tradethrone.vercel.app") is True