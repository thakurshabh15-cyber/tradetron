"""P0 remediation regression tests.

Covers the four P0 blockers resolved in this phase:

1. Secrets/Credentials — tracked source must not contain credential values;
   ``.env`` secret files must be git-ignored; ``.env.example`` files must carry
   placeholders only.
2. Production configuration — ``ENVIRONMENT=production`` fails fast and
   diagnostically when JWT_SECRET / DATABASE_URL / an explicit Redis URL are
   missing or invalid; no silent localhost/SQLite fallback in production.
3. OpenTelemetry dependency + webhook queue Redis selection.
4. Readiness behaviour — cache failure in production yields HTTP 503 and the
   error body must never contain connection strings.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.brokers import (
    BrokerModeBlockedError,
    assert_live_dispatch_allowed,
    live_dispatch_allowed,
)
from app.config import Settings, settings

REPO_ROOT = Path(__file__).resolve().parent.parent  # fastapi-template/
GIT_ROOT = REPO_ROOT.parent


class _FakeComponent:
    """Minimal stand-in for async singletons (queue / rate limiter / idempotency)."""

    def __init__(self, init_fn=None, shutdown_fn=None):
        self._init_fn = init_fn or (lambda: _async_noop())
        self._shutdown_fn = shutdown_fn or (lambda: _async_noop())

    async def initialize(self):
        await self._init_fn()

    async def shutdown(self):
        await self._shutdown_fn()


class _FakePool:
    """Minimal stand-in for WorkerPool (start/stop only)."""

    def __init__(self, start_fn=None, stop_fn=None):
        self._start_fn = start_fn or (lambda: _async_noop())
        self._stop_fn = stop_fn or (lambda: _async_noop())

    async def start(self):
        await self._start_fn()

    async def stop(self):
        await self._stop_fn()


async def _async_noop():
    """Async no-op used by fake component init/shutdown hooks."""
    return None



# ─────────────────────────────────────────────────────────────────────────────
# 1. Production configuration fail-fast
# ─────────────────────────────────────────────────────────────────────────────

def _prod_settings(**overrides) -> Settings:
    # URL fixtures are assembled from parts so this test module's own source
    # never literally contains the ``user:pass@host`` patterns the P0 scanner is
    # designed to detect in tracked source.
    base = dict(
        environment="production",
        jwt_secret="p" * 40,
        skip_signature_verification=False,
        webhook_local_mode=False,
        database_url="postgresql://" + "tradetron_user:test_password" + "@db.example.internal:5432/tradetron",
        upstash_redis_url="rediss://" + "test_user:test_password" + "@eu1.test-redis.example:6379/0",
        broker_mode="simulated",
    )
    base.update(overrides)
    return Settings(**base)


def test_production_requires_strong_jwt_secret():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _prod_settings(jwt_secret="short")


def test_production_requires_hosted_database_url():
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        _prod_settings(database_url="")


def test_production_rejects_sqlite_database_url():
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        _prod_settings(database_url="sqlite+aiosqlite:///./trading.db")


def test_production_requires_explicit_redis_url():
    with pytest.raises(ValidationError, match="UPSTASH_REDIS_URL|REDIS_URL"):
        _prod_settings(upstash_redis_url="", redis_url="")
    with pytest.raises(ValidationError, match="UPSTASH_REDIS_URL|REDIS_URL"):
        _prod_settings(upstash_redis_url="", redis_url="redis://localhost:6379/0")


def test_production_accepts_valid_redis_urls():
    s = _prod_settings()
    assert s.environment == "production"
    assert s.effective_redis_url == s.upstash_redis_url
    # Non-localhost plain redis:// is also acceptable when explicitly configured.
    s2 = _prod_settings(
        upstash_redis_url="", redis_url="redis://cache.example.internal:6379/0"
    )
    assert s2.effective_redis_url == "redis://cache.example.internal:6379/0"


@pytest.mark.parametrize(
    "bad_url",
    ["http://cache.example:6379", "mysql://cache.example/db", "not-a-url", "rediss://"],
)
def test_invalid_redis_url_rejected(bad_url):
    with pytest.raises(ValidationError, match="Invalid (UPSTASH_REDIS_URL|REDIS_URL) URL"):
        _prod_settings(upstash_redis_url=bad_url)
    with pytest.raises(ValidationError, match="Invalid REDIS_URL URL"):
        _prod_settings(upstash_redis_url="", redis_url=bad_url)


def test_production_rejects_skip_signature_verification():
    with pytest.raises(ValidationError, match="SKIP_SIGNATURE_VERIFICATION"):
        _prod_settings(skip_signature_verification=True)


def test_development_boots_without_any_secrets():
    # Explicit widths neutralise the developer's local .env (which may carry
    # real dev secrets); the point is that development tolerates empty secrets
    # and falls back to safe local defaults — production does NOT.
    s = Settings(
        environment="development",
        broker_mode="simulated",
        jwt_secret="",
        upstash_redis_url="",
        redis_url="redis://localhost:6379/0",
    )
    assert s.jwt_secret == ""
    assert s.database_url.startswith("sqlite")
    assert "localhost" in s.effective_redis_url


def _run_config_subprocess(code: str, env_overrides: dict[str, str]):
    """Run a config-boot probe with deterministic, UTF-8-safe stdio capture.

    Windows regression: under ``python -X utf8`` the parent pytest process
    decodes subprocess output as UTF-8, but the child process (which does not
    inherit the ``-X utf8`` flag) emits locale-encoded bytes — e.g. the em dash
    in the production Redis diagnostic renders as a single ``0x97`` byte on
    cp125x locales, crashing the parent with ``UnicodeDecodeError`` instead of
    failing the real assertion.  The harness fixes BOTH directions of that
    mismatch:

    * the child always runs in UTF-8 mode (``-X utf8`` + ``PYTHONUTF8=1``), so
      its own stderr/stdout encoding is deterministic on every OS/CI image;
    * the parent explicitly decodes as UTF-8 with ``errors="replace"`` so a
      stray non-UTF-8 byte can never crash collection of the diagnostics.

    Subprocess failures are preserved for the caller to assert on
    (``returncode`` is never hidden or rewritten).
    """
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
    )


def test_production_boot_refuses_missing_redis_in_subprocess():
    """The real module-level ``settings = Settings()`` must die on boot."""
    env = {
        "ENVIRONMENT": "production",
        "JWT_SECRET": "j" * 40,
        "DATABASE_URL": "postgresql://" + "u:test_password" + "@db.example.internal/tradetron",
        "UPSTASH_REDIS_URL": "",
        "REDIS_URL": "",
        "BROKER_MODE": "simulated",
        # WEBHOOK_LOCAL_MODE is validated away in production configs; the
        # fixture builds an otherwise-complete production boot.
        "WEBHOOK_LOCAL_MODE": "false",
    }
    result = _run_config_subprocess("import app.config", env)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "UPSTASH_REDIS_URL" in combined or "REDIS_URL" in combined


def test_production_boot_succeeds_with_full_config_in_subprocess():
    env = {
        **os.environ,
        "ENVIRONMENT": "production",
        "JWT_SECRET": "j" * 40,
        "DATABASE_URL": "postgresql://" + "u:test_password" + "@db.example.internal/tradetron",
        "UPSTASH_REDIS_URL": "rediss://" + "u:test_password" + "@redis.example.internal:6379/0",
        "REDIS_URL": "",
        "BROKER_MODE": "simulated",
        "WEBHOOK_LOCAL_MODE": "false",
    }
    result = _run_config_subprocess(
        "import app.config; print(app.config.settings.environment)",
        env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "production"


def test_config_subprocess_capture_is_deterministic_utf8():
    """Regression for the Windows ``-X utf8`` UnicodeDecodeError.

    The child writes the em dash as raw cp1253 byte ``0x97`` — exactly the
    output shape that previously crashed the parent under Python UTF-8 mode —
    alongside the real production fail-fast diagnostic.  The harness must
    decode it deterministically, still surface the Redis diagnostic, and keep
    the production boot-failure behavior intact.
    """
    code = (
        "import sys;"
        "sys.stderr.buffer.write('\\u2014'.encode('cp1253'));"
        "sys.stderr.buffer.flush();"
        "import app.config"
    )
    env = {
        "ENVIRONMENT": "production",
        "JWT_SECRET": "j" * 40,
        "DATABASE_URL": "postgresql://" + "u:test_password" + "@db.example.internal/tradetron",
        "UPSTASH_REDIS_URL": "",
        "REDIS_URL": "",
        "BROKER_MODE": "simulated",
        "WEBHOOK_LOCAL_MODE": "false",
    }
    result = _run_config_subprocess(code, env)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "UPSTASH_REDIS_URL" in combined or "REDIS_URL" in combined
    # The stray non-UTF-8 byte was replaced with U+FFFD instead of crashing.
    assert "\ufffd" in combined


# ─────────────────────────────────────────────────────────────────────────────
# 2. Trading safety guarantees
# ─────────────────────────────────────────────────────────────────────────────

def test_broker_mode_default_is_simulated():
    assert Settings(environment="development").broker_mode == "simulated"
    assert Settings(environment="testing").broker_mode == "simulated"


def test_live_dispatch_guard_blocks_outside_live_mode():
    original = settings.broker_mode
    try:
        settings.broker_mode = "simulated"
        assert live_dispatch_allowed() is False
        with pytest.raises(BrokerModeBlockedError):
            assert_live_dispatch_allowed()
    finally:
        settings.broker_mode = original


# ─────────────────────────────────────────────────────────────────────────────
# 3. Credentials absent from tracked source, env files ignored, placeholders
# ─────────────────────────────────────────────────────────────────────────────

GENERIC_SECRET_RX = {
    "coingecko_key": re.compile(r"CG-[A-Za-z0-9]{20,}"),
    "angel_key_id": re.compile(r"AACE[0-9]{6,}"),
    # NOTE: both alternatives use [-_] separator character classes so this
    # file's own source never literally spells the credential tokens the
    # scanner below detects (previously the dev-prefixed placeholder literal
    # made the P0 scanner trip over this file's own source).
    "jwt_known": re.compile(
        r"(?:super[-_]secret[-_]jwt[-_]key|dev[-_]super[-_]secret[-_]jwt[-_]key)"
    ),
    "claude_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    # The BEGIN marker is assembled from separate literals
    # (``"-----BEGIN" + " "``) so the scanner never matches this file's own
    # source while still matching real private keys (the runtime pattern
    # preserves the original ``.*`` so PEM/OpenSSH and PKCS#8 formats remain
    # detected).
    "private_key": re.compile(
        "-----BEGIN" + " " + r".*" + "PRIVATE KEY-----"
    ),
}

URL_CREDS_RX = re.compile(
    r"(?:rediss?|postgres(?:ql)?(?:\+\w+)?)://"
    r"(?P<user>[^@\s\"'<>/]+):(?P<pass>[^@\s\"'<>/]+)@"
    r"(?P<host>[^/\s\"'<>:]+)"
)

_ALLOWED_URL_HOSTS = {"host", "localhost", "postgres", "127.0.0.1", "0.0.0.0"}


def _tracked_project_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=GIT_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.splitlines() if p.startswith("fastapi-template/")]


def _is_example(rel: str) -> bool:
    return rel.endswith(".env.example") or rel.endswith(".env.production.example")


def test_no_real_credentials_in_tracked_source():
    for rel in _tracked_project_files():
        full = Path(GIT_ROOT) / rel
        if not full.is_file():
            continue
        text = full.read_text(encoding="utf-8", errors="replace")
        for name, rx in GENERIC_SECRET_RX.items():
            assert not rx.search(text), f"{name} pattern matched in tracked file {rel}"
        for m in URL_CREDS_RX.finditer(text):
            host = m.group("host")
            if host in _ALLOWED_URL_HOSTS:
                continue
            if _is_example(rel):
                user_pass = m.group("user") + m.group("pass")
                assert "your_" in user_pass or "replace_with" in user_pass, (
                    f"example file {rel} contains a non-placeholder credential URL"
                )
            else:
                raise AssertionError(
                    f"credential-bearing URL with non-local host '{host}' in {rel}"
                )


def test_scanner_detects_genuine_credential_fixtures():
    """The P0 scanner must still detect genuine credential-like values.

    Fixtures are assembled at runtime so this module's own source never
    literally contains the very patterns the scanner is designed to find.
    """
    dev_jwt = "dev_" + "_".join(["super", "secret", "jwt", "key"])
    super_jwt = "_".join(["super", "secret", "jwt", "key"])
    pem_begin = "-----BEGIN" + " "
    pem_end = "PRIVATE KEY-----"

    samples = {
        "coingecko_key": ["CG-" + "A" * 22],
        "angel_key_id": ["AACE" + "12345678"],
        "jwt_known": [dev_jwt, super_jwt],
        "claude_key": ["sk-ant-" + "b" * 24],
        "aws_key": ["AKIA" + "ABCDEFGHIJKLMNOP"],
        "private_key": [pem_begin + "RSA" + " " + pem_end, pem_begin + pem_end],
    }
    for name, fixtures in samples.items():
        for fixture in fixtures:
            assert GENERIC_SECRET_RX[name].search(fixture), (
                f"{name} must detect fixture {fixture!r}"
            )

    url_fixture = "rediss://" + "user:pass" + "@fixture.example.internal:6379/0"
    assert URL_CREDS_RX.search(url_fixture) is not None, (
        "URL_CREDS_RX must detect a credential-bearing fixture URL"
    )


def test_scanner_does_not_flag_this_module_source():
    """Regression: the scanner must not trip over its own test module.

    The old self-referential literals (JWT placeholder, credential-bearing
    Redis/Postgres fixture URLs) have been removed; the scanner must run
    clean over this file.
    """
    text = Path(__file__).read_text(encoding="utf-8", errors="replace")
    for name, rx in GENERIC_SECRET_RX.items():
        assert not rx.search(text), f"{name} self-matched in {Path(__file__).name}"
    for m in URL_CREDS_RX.finditer(text):
        assert m.group("host") in _ALLOWED_URL_HOSTS, (
            f"credential-bearing URL self-matched with host={m.group('host')!r}"
        )


def test_example_env_files_are_placeholders_only():
    for rel in (
        ".env.example",
        ".env.production.example",
        "client/.env.example",
        "client/.env.production.example",
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for name, rx in GENERIC_SECRET_RX.items():
            assert not rx.search(text), f"{name} in {rel}"
        for m in URL_CREDS_RX.finditer(text):
            assert "your_" in m.group("user") + m.group("pass") or \
                "replace_with" in m.group("user") + m.group("pass"), rel


@pytest.mark.parametrize(
    "f",
    [
        "fastapi-template/.env",
        "fastapi-template/.env.production",
        "fastapi-template/.env.staging",
    ],
)
def test_env_secret_files_are_gitignored(f):
    result = subprocess.run(
        ["git", "check-ignore", f], cwd=GIT_ROOT, capture_output=True
    )
    assert result.returncode == 0, f"{f} must be ignored by git"


def test_docker_compose_uses_env_interpolation_not_literals():
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${JWT_SECRET:-" in text or "${JWT_SECRET:?" in text, \
        "JWT_SECRET must be env-interpolated in docker-compose.yml"
    # The dev-prefixed token is assembled at runtime; never spell the literal
    # so the secret scanner (which runs over this same file) stays clean.
    dev_jwt = "dev_" + "_".join(["super", "secret", "jwt", "key"])
    assert dev_jwt not in text
    assert "${POSTGRES_PASSWORD:-" in text


# ─────────────────────────────────────────────────────────────────────────────
# 4. webhook queue Redis selection + readiness behaviour
# ─────────────────────────────────────────────────────────────────────────────

def test_webhook_queue_honours_effective_redis_url():
    from app.webhooks.queue.redis_streams import WebhookQueue

    original_upstash = settings.upstash_redis_url
    original_redis = settings.redis_url
    try:
        settings.upstash_redis_url = ""
        # URL fixtures are assembled from parts so the P0 scanner (which runs
        # over this very file) does not flag this test module's own source.
        cache_url = "redis://cache.example.internal:6379" + "/0"
        settings.redis_url = cache_url
        assert WebhookQueue().redis_url == cache_url
        upstash_url = "rediss://" + "u:p" + "@up.example.internal:6379" + "/0"
        settings.upstash_redis_url = upstash_url
        assert WebhookQueue().redis_url == upstash_url
    finally:
        settings.upstash_redis_url = original_upstash
        settings.redis_url = original_redis

def test_webhook_idempotency_and_rate_limiter_honour_effective_redis_url():
    """IdempotencyStore and the webhook TokenBucketRateLimiter must select the
    same effective Redis URL as WebhookQueue (upstash overrides redis_url)."""
    from app.webhooks.resiliency.idempotency import IdempotencyStore
    from app.webhooks.resiliency.rate_limiter import TokenBucketRateLimiter

    original_upstash = settings.upstash_redis_url
    original_redis = settings.redis_url
    try:
        settings.upstash_redis_url = ""
        # URL fixtures are assembled from parts so the P0 scanner (which runs
        # over this very file) does not flag this test module's own source.
        cache_url = "redis://cache.example.internal:6379" + "/0"
        settings.redis_url = cache_url
        assert IdempotencyStore().redis_url == cache_url
        assert TokenBucketRateLimiter().redis_url == cache_url

        upstash_url = "rediss://" + "u:p" + "@up.example.internal:6379" + "/0"
        settings.upstash_redis_url = upstash_url
        assert IdempotencyStore().redis_url == upstash_url
        assert TokenBucketRateLimiter().redis_url == upstash_url
    finally:
        settings.upstash_redis_url = original_upstash
        settings.redis_url = original_redis


@pytest.mark.asyncio
async def test_webhook_lifespan_starts_pipeline(monkeypatch):
    """Outside webhook_local_mode, the webhook app lifespan must initialize the
    Redis queue, rate limiter, idempotency store and start the worker pool."""
    from app.webhooks.main import lifespan
    from fastapi import FastAPI

    started_components = {}

    async def fake_queue_init():
        started_components["queue"] = True
    async def fake_ratelimit_init():
        started_components["rate_limiter"] = True
    async def fake_idem_init():
        started_components["idempotency"] = True
    async def fake_pool_start():
        started_components["workers"] = True

    monkeypatch.setattr(settings, "webhook_local_mode", False)
    monkeypatch.setattr("app.webhooks.main.init_db", lambda *a, **k: _async_noop())
    monkeypatch.setattr("app.webhooks.main.init_verifiers", lambda *a, **k: None)
    monkeypatch.setattr("app.webhooks.main.init_circuit_breakers", lambda: None)
    monkeypatch.setattr("app.webhooks.main.init_bulkheads", lambda: None)

    # The lifespan imports these singletons locally from their own modules, so
    # patch them at their source module attributes.
    monkeypatch.setattr(
        "app.webhooks.queue.redis_streams.webhook_queue",
        _FakeComponent(fake_queue_init),
    )
    monkeypatch.setattr(
        "app.webhooks.resiliency.rate_limiter.rate_limiter",
        _FakeComponent(fake_ratelimit_init),
    )
    monkeypatch.setattr(
        "app.webhooks.resiliency.idempotency.idempotency_store",
        _FakeComponent(fake_idem_init),
    )
    monkeypatch.setattr(
        "app.webhooks.workers.pool.worker_pool",
        _FakePool(fake_pool_start),
    )

    async with lifespan(FastAPI()):
        pass

    assert started_components == {
        "queue": True,
        "rate_limiter": True,
        "idempotency": True,
        "workers": True,
    }


@pytest.mark.asyncio
async def test_webhook_lifespan_skips_pipeline_in_local_mode(monkeypatch):
    """In webhook_local_mode the pipeline must NOT be started (Redis bypassed)."""
    from app.webhooks.main import lifespan
    from fastapi import FastAPI

    started_components = {}

    async def fake_queue_init():
        started_components["queue"] = True
    async def fake_pool_start():
        started_components["workers"] = True
    async def fake_ratelimit_init():
        started_components["rate_limiter"] = True
    async def fake_idem_init():
        started_components["idempotency"] = True

    monkeypatch.setattr(settings, "webhook_local_mode", True)
    monkeypatch.setattr("app.webhooks.main.init_db", lambda *a, **k: _async_noop())
    monkeypatch.setattr("app.webhooks.main.init_verifiers", lambda *a, **k: None)
    monkeypatch.setattr("app.webhooks.main.init_circuit_breakers", lambda: None)
    monkeypatch.setattr("app.webhooks.main.init_bulkheads", lambda: None)

    monkeypatch.setattr(
        "app.webhooks.queue.redis_streams.webhook_queue",
        _FakeComponent(fake_queue_init),
    )
    monkeypatch.setattr(
        "app.webhooks.resiliency.rate_limiter.rate_limiter",
        _FakeComponent(fake_ratelimit_init),
    )
    monkeypatch.setattr(
        "app.webhooks.resiliency.idempotency.idempotency_store",
        _FakeComponent(fake_idem_init),
    )
    monkeypatch.setattr(
        "app.webhooks.workers.pool.worker_pool",
        _FakePool(fake_pool_start),
    )

    async with lifespan(FastAPI()):
        pass

    assert started_components == {}





def test_sanitize_error_redacts_configured_urls():
    from app.main import _sanitize_error

    crafted = ConnectionError(f"cannot reach {settings.effective_redis_url}")
    redacted = _sanitize_error(crafted)
    assert settings.effective_redis_url not in redacted
    assert "<redacted>" in redacted


@pytest.mark.asyncio
async def test_readyz_503_when_cache_down_in_production(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    monkeypatch.setattr(settings, "environment", "production")

    def boom(*args, **kwargs):
        raise ConnectionError(f"unreachable {settings.effective_redis_url}")

    monkeypatch.setattr("redis.asyncio.from_url", boom)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["cache"] is False
    assert "<redacted>" in body["errors"]["cache"]
    assert "redis://" not in body["errors"]["cache"]


@pytest.mark.asyncio
async def test_readyz_200_when_cache_ok_non_production(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    monkeypatch.setattr(settings, "environment", "development")
    # conftest's mock_redis makes ping() succeed -> cache check passes.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"]["cache"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 5. OpenTelemetry importability (the P0 dependency fix)
# ─────────────────────────────────────────────────────────────────────────────

def test_otel_instrumentation_imports_resolve():
    """The exact import chain that broke test collection must now import."""
    import opentelemetry.instrumentation.fastapi  # noqa: F401
    import opentelemetry.instrumentation.redis  # noqa: F401
    import opentelemetry.instrumentation.sqlalchemy  # noqa: F401
    import prometheus_client  # noqa: F401
    from app.webhooks.main import app as webhook_app  # noqa: F401

    assert webhook_app.title == "TradeThrone Webhook Platform"