"""P2-3 / P2-4 regression tests: structured logging and /metrics.

Logging:
- production environment ⇒ root logger emits single-line JSON with stable
  fields (timestamp/level/logger/message) plus caller ``extra`` context;
- development/testing ⇒ human-readable formatter;
- ``setup_logging()`` is idempotent (no duplicate handlers after reload).

Metrics:
- ``GET /metrics`` serves Prometheus text format with application metrics;
- the HTTP middleware records request volume with route/status labels.
"""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest

from app.config import settings
from app.main import app


# ── P2-3: structured logging ──────────────────────────────────────────────────


def test_json_formatter_emits_stable_fields_with_extra_context():
    from app.core.logging import JsonLogFormatter

    logger = logging.getLogger("tradetron.tests.logging")
    logger.setLevel(logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    try:
        logger.info("hello %s", "world", extra={"user_id": 7})
    finally:
        logger.removeHandler(handler)

    payload = json.loads(stream.getvalue())
    assert payload["logger"] == "tradetron.tests.logging"
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello world"
    assert payload["user_id"] == 7
    assert "timestamp" in payload
    assert payload["timestamp"].endswith("Z") or "+00:00" in payload["timestamp"]


def test_json_formatter_never_exposes_stdlib_record_attrs():
    from app.core.logging import JsonLogFormatter

    logger = logging.getLogger("tradetron.tests.logging")
    logger.setLevel(logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    try:
        logger.info("plain")
    finally:
        logger.removeHandler(handler)

    payload = json.loads(stream.getvalue())
    assert set(payload) == {"timestamp", "level", "logger", "message"}


def test_setup_logging_is_idempotent(monkeypatch):
    from app.core.logging import _OWNED_HANDLER_ATTR, setup_logging

    monkeypatch.setattr(settings, "environment", "development")
    setup_logging()
    setup_logging()  # must not stack a second handler

    root = logging.getLogger()
    owned = [h for h in root.handlers if getattr(h, _OWNED_HANDLER_ATTR, False)]
    assert len(owned) == 1


def test_production_setup_installs_json_formatter(monkeypatch):
    from app.core.logging import JsonLogFormatter, _OWNED_HANDLER_ATTR, setup_logging

    monkeypatch.setattr(settings, "environment", "production")
    setup_logging()

    root = logging.getLogger()
    owned = [h for h in root.handlers if getattr(h, _OWNED_HANDLER_ATTR, False)]
    assert len(owned) == 1
    assert isinstance(owned[0].formatter, JsonLogFormatter)

    # Restore the dev/testing formatter for the rest of the suite.
    monkeypatch.setattr(settings, "environment", "testing")
    setup_logging()
    owned = [h for h in root.handlers if getattr(h, _OWNED_HANDLER_ATTR, False)]
    assert not isinstance(owned[0].formatter, JsonLogFormatter)


def test_monitoring_no_bot_token_prefix_in_logs(monkeypatch):
    """Token prefix must not be printed in the channel-config log line."""
    import app.core.monitoring as monitoring

    monkeypatch.setattr(monitoring.settings, "telegram_bot_token", "123456789:AA_TESTSECRET__")
    monkeypatch.setattr(monitoring.settings, "telegram_chat_id", "-100123")
    # Re-evaluate the module-level config block by checking the string source:
    src = open(monitoring.__file__, encoding="utf-8").read()
    assert "channel configured (bot token:" not in src


# ── P2-4: /metrics ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_prometheus_text(monkeypatch):
    monkeypatch.setattr(settings, "environment", "testing")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/metrics")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    assert "tradetron_http_requests_total" in res.text


@pytest.mark.asyncio
async def test_metrics_middleware_records_request_volume(monkeypatch):
    monkeypatch.setattr(settings, "environment", "testing")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/healthz")
        await client.get("/healthz")
        res = await client.get("/metrics")
    assert 'route="/healthz"' in res.text
    assert 'status="200"' in res.text

    # The two /healthz hits must be counted (metric is cumulative across tests,
    # so a count >= 2 is the safe assertion).
    import re

    match = re.search(
        r'tradetron_http_requests_total\{method="GET",route="/healthz",status="200"\} (\d+)',
        res.text,
    )
    assert match is not None and int(match.group(1)) >= 2