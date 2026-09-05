"""Application-level metrics (P2-4).

Prometheus text-format metrics for the main API, exposed at ``GET /metrics``
(see ``app/main.py``).  Low-cardinality operational metrics only:

- ``tradetron_http_requests_total`` — HTTP request volume by method / route
  template / status, recorded by the HTTP middleware,
- ``tradetron_engine_state`` / ``tradetron_broker_mode_live`` /
  ``tradetron_ws_channels`` — current runtime state.

Uses ``prometheus-client`` (declared in requirements.txt) with a defensive
fallback so the endpoint never 500s if the library is ever missing.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

try:  # prometheus-client is a declared runtime dependency
    from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

    _PROM_CLIENT_AVAILABLE = True
except Exception:  # pragma: no cover — defensive fallback only
    CollectorRegistry = Counter = Gauge = generate_latest = None  # type: ignore[assignment,misc]
    _PROM_CLIENT_AVAILABLE = False


APP_REGISTRY = CollectorRegistry() if _PROM_CLIENT_AVAILABLE else None

if _PROM_CLIENT_AVAILABLE:
    http_requests_total = Counter(
        "tradetron_http_requests_total",
        "HTTP requests by method, route template and status",
        ["method", "route", "status"],
        registry=APP_REGISTRY,
    )
    engine_state = Gauge(
        "tradetron_engine_state",
        "1 when the trading engine is running, else 0",
        registry=APP_REGISTRY,
    )
    broker_mode_live = Gauge(
        "tradetron_broker_mode_live",
        "1 when BROKER_MODE=live, else 0",
        registry=APP_REGISTRY,
    )
    ws_channels = Gauge(
        "tradetron_ws_channels",
        "Current number of websocket subscribers",
        registry=APP_REGISTRY,
    )
else:  # pragma: no cover — defensive fallback
    http_requests_total = engine_state = broker_mode_live = ws_channels = None  # type: ignore[assignment]


async def metrics_middleware(request: Request, call_next: Any):
    """Starlette ``BaseHTTPMiddleware``-compatible request counter.

    Works with the ``@app.middleware("http")`` registration style used in
    ``app/main.py``; never raises — metrics are best-effort.
    """
    response = await call_next(request)
    if _PROM_CLIENT_AVAILABLE and http_requests_total is not None:
        route = getattr(request.scope.get("route"), "path", None) or request.url.path
        http_requests_total.labels(
            method=request.method,
            route=route,
            status=str(response.status_code),
        ).inc()
    return response


def render_metrics() -> Response:
    """Serialize the application registry in Prometheus text format.

    Falls back to a minimal explanatory response if the client library is
    unavailable so monitoring scrapes never fail the endpoint.
    """
    if _PROM_CLIENT_AVAILABLE and APP_REGISTRY is not None:
        return Response(
            content=generate_latest(APP_REGISTRY),
            media_type="text/plain; version=0.0.4",
        )
    return PlainTextResponse(  # pragma: no cover — defensive fallback
        "# TradeThrone: prometheus_client not installed; metrics unavailable.\n",
        status_code=200,
    )