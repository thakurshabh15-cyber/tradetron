"""Phase 14 (Observability) regression tests — error-path visibility (OBS-1 / OBS-2).

OBS-1  Unhandled exceptions must:
  • return the app's JSON ``{"detail": "Internal server error"}`` convention
    (not Starlette's bare plain-text ``Internal Server Error``);
  • be counted in ``tradetron_http_requests_total`` under the matched route
    template with status ``"500"`` so Prometheus scrapes see error volume;
  • reach the monitoring sentinel so Sentry / Telegram channels fire.

OBS-2  ``MonitoringSentinel.capture_exception`` must dispatch Telegram
      when the channel is configured (parity with all other sentinel methods).
"""

from __future__ import annotations

import re

import httpx
import pytest

from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def raising_route():
    """Add a throwaway endpoint that always raises to the live app, then clean up."""

    async def _boom():
        raise RuntimeError("p14-forced-boom")

    path = "/__p14_boom"
    app.add_api_route(path, _boom, include_in_schema=False, methods=["GET"])
    yield path
    app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != path]


# ---------------------------------------------------------------------------
# OBS-1 tests
# ---------------------------------------------------------------------------

class TestUnhandledExceptionProducesJson500:
    """When an endpoint raises an unexpected exception the response MUST be a
    JSON ``{"detail": "Internal server error"}`` — never the default plain-text
    ``Internal Server Error`` that Starlette's ServerErrorMiddleware emits."""

    async def test_status_500(self, raising_route: str) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            res = await c.get(raising_route)
        assert res.status_code == 500

    async def test_json_content_type(self, raising_route: str) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            res = await c.get(raising_route)
        assert res.headers["content-type"].startswith("application/json")

    async def test_detail_body(self, raising_route: str) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            res = await c.get(raising_route)
        assert res.json() == {"detail": "Internal server error"}

    async def test_no_leaked_exception_text(self, raising_route: str) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            res = await c.get(raising_route)
        assert "p14-forced-boom" not in res.text


class TestUnhandledExceptionCountedInMetrics:
    """500s from unhandled exceptions MUST appear in the Prometheus counter
    so that error volume is visible in dashboards / alerts."""

    async def test_500_metric_present(self, raising_route: str) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get(raising_route)
            metrics = await c.get("/metrics")

        pattern = re.compile(
            r'tradetron_http_requests_total\{method="GET",route="/__p14_boom",status="500"\}\s+(\d+)',
        )
        match = pattern.search(metrics.text)
        assert match is not None, (
            "tradetron_http_requests_total missing the /__p14_boom 500 entry"
        )
        assert int(match.group(1)) >= 1


# ---------------------------------------------------------------------------
# OBS-2 tests
# ---------------------------------------------------------------------------

class TestCaptureExceptionDispatchesTelegram:
    """When Telegram is configured, ``capture_exception`` must fire the
    Telegram channel — matching the behaviour of every other sentinel method
    (order failures, risk breaches, broker disconnects)."""

    def test_dispatches_telegram(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.core.monitoring as mon

        dispatched: list[str] = []
        monkeypatch.setattr(mon, "_telegram_configured", True)
        monkeypatch.setattr(mon, "_fire_and_forget_telegram", dispatched.append)

        mon.MonitoringSentinel.capture_exception(
            RuntimeError("p14-telegram-check"),
            context={"path": "/api/test"},
        )

        assert len(dispatched) >= 1, (
            "capture_exception did not fire Telegram when configured"
        )
        msg = dispatched[0]
        assert "EXCEPTION" in msg.upper()
        assert "RuntimeError" in msg

    def test_no_dispatch_when_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.core.monitoring as mon

        dispatched: list[str] = []
        monkeypatch.setattr(mon, "_telegram_configured", False)
        monkeypatch.setattr(mon, "_fire_and_forget_telegram", dispatched.append)

        mon.MonitoringSentinel.capture_exception(ValueError("quiet"))

        assert dispatched == []
