"""FastAPI application factory.

Wires together:
  - CORS middleware (permissive for local development)
  - API route routers
  - Database initialisation (lifespan)
  - Market data simulator (lifespan)
  - Trading engine (lifespan)
  - Structured logging
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.logging import get_logger, setup_logging

# Module-level references set during lifespan
_engine = None
_simulator = None


def get_engine():
    """Return the running TradingEngine instance (or None)."""
    return _engine


def get_simulator():
    """Return the running MarketSimulator instance (or None)."""
    return _simulator


@asynccontextmanager
async def lifespan(application: FastAPI):  # noqa: ARG001
    """Application lifespan — start/stop engine and simulator."""
    global _engine, _simulator

    setup_logging()
    logger = get_logger("main")
    logger.info("Starting %s…", settings.app_name)

    # 0. Non-negotiable pre-flight: apply pending Alembic migrations BEFORE
    #    anything else.  On Render Free there is no Pre-Deploy / Release hook,
    #    so the schema is brought to head here; the uvicorn / FastAPI lifespan
    #    model guarantees no request is served until this block completes.
    #    Uses a subprocess (same pattern as scripts/ci_alembic_check.py)
    #    because alembic/env.py calls asyncio.run() which cannot run from
    #    inside a live event loop.
    from app.db.migrations import MigrationError, run_migrations

    try:
        run_migrations()
        logger.info("Alembic migrations up to date (upgrade head).")
    except MigrationError as exc:
        logger.critical(
            "Refusing to start %s — Alembic migration failed: %s. "
            "The application will NOT serve any request until the schema is "
            "brought to head.",
            settings.app_name,
            exc,
            exc_info=True,
        )
        raise

    # 1. Initialise database (create tables and seed data)
    from app.db.session import init_db

    try:
        await init_db()
        logger.info("Database schema & seed initialization completed successfully.")
    except Exception as exc:
        logger.critical("Failed to initialize database schema: %s", exc, exc_info=True)
        raise

    # 2. Create shared tick queue
    tick_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)

    # 3. Create broker
    if settings.broker_mode == "live":
        from app.brokers.angelone import AngelOneBroker

        broker = AngelOneBroker()
        await broker.connect()
    else:
        from app.brokers.simulated import SimulatedBroker

        broker = SimulatedBroker()
        await broker.connect()

    # 4. Start market data simulator & Unified Multi-Asset Hub
    from app.market_data.simulator import MarketSimulator
    from app.market_data.unified_manager import unified_market_manager

    _simulator = MarketSimulator(tick_queue)
    if settings.broker_mode == "simulated":
        _simulator.set_broker(broker)
    await _simulator.start(settings.sim_symbol_list)

    unified_market_manager.set_tick_queue(tick_queue)
    if settings.broker_mode == "simulated":
        unified_market_manager.set_broker(broker)
    await unified_market_manager.start()

    # 5. Start trading engine
    from app.engine.trading_engine import TradingEngine

    _engine = TradingEngine(broker=broker, tick_queue=tick_queue)
    await _engine.start()

    # 6. Start Automated Daily 8:45 AM IST Broker TOTP & Session Renewal Scheduler
    from app.engine.broker_cron import broker_scheduler

    broker_scheduler.start()

    # 7. Start the bounded broker-order reconciliation scheduler (P1
    #    crash-window read-back for stale keyed PENDING DMA/manual orders).
    from app.engine.order_reconciliation import broker_order_reconciliation_scheduler

    broker_order_reconciliation_scheduler.start()

    logger.info(
        "%s ready — broker=%s, symbols=%s",
        settings.app_name,
        settings.broker_mode,
        settings.sim_symbol_list,
    )

    yield  # ← Application runs here

    # Shutdown
    logger.info("Shutting down %s…", settings.app_name)
    broker_scheduler.stop()
    broker_order_reconciliation_scheduler.stop()
    if _engine:
        await _engine.stop()
    if _simulator:
        await _simulator.stop()
    await unified_market_manager.stop()
    _engine = None
    _simulator = None


# ── Create application ───────────────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    description="Algorithmic trading platform with real-time market data",
    version="1.0.0",
    lifespan=lifespan,
    # Interactive docs (/docs, /redoc, /openapi.json) are a development aid but
    # expose the complete endpoint/schema inventory to attackers in production.
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url="/redoc" if settings.environment != "production" else None,
    openapi_url="/openapi.json" if settings.environment != "production" else None,
)


# Phase 14 / OBS-1: unhandled-exception handling — inner middleware layer.
# Registered FIRST (before CORS/security/metrics) because Starlette's
# ``add_middleware`` prepends: the first-registered user middleware ends up
# wrapping directly above ExceptionMiddleware (the innermost position). This
# turns route-level exceptions into the app's JSON 500 INSIDE the chain, so the
# normal response path completes — the 500 is counted by
# ``tradetron_http_requests_total`` and hardened by the security-headers
# middleware exactly like any other response.
@app.middleware("http")
async def catch_unhandled_errors(request, call_next):
    """Convert route-level unhandled exceptions into the app's JSON 500."""
    try:
        return await call_next(request)
    except Exception as exc:
        _client = request.client
        client_host = _client[0] if isinstance(_client, tuple) else None

        logger = get_logger("main")
        logger.error(
            "Unhandled exception on %s %s: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

        from app.core.monitoring import monitoring_sentinel

        monitoring_sentinel.capture_exception(
            exc,
            context={
                "method": request.method,
                "path": request.url.path,
                "client_host": client_host,
            },
        )
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Dynamic CORS Configuration
# Hardened defaults: a wildcard origin is NEVER combined with credentials in
# production — deployments without an explicit ALLOWED_ORIGINS lock to the
# official TradeThrone domains automatically, and the Vercel preview regex is
# anchored to the two official project slugs (never generic `*.vercel.app`).
from app.core.cors import build_cors_config

_cors = build_cors_config(
    environment=settings.environment,
    allowed_origins=settings.allowed_origins,
    frontend_url=settings.frontend_url,
)
if settings.environment == "production" and settings.allowed_origins.strip() in ("", "*"):
    print(f"[SECURITY] ALLOWED_ORIGINS unset in production — locking CORS to exact origins only: {_cors.origins} (no *.vercel.app regex trust)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors.origins,
    allow_origin_regex=_cors.origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    """Attach hardened security headers to every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    if settings.environment == "production":
        # Force HTTPS for 2 years across all subdomains (HSTS preload eligible).
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
    return response


# P2-4: Prometheus request-volume metrics (best-effort; never fails requests).
from app.core.metrics import metrics_middleware

app.middleware("http")(metrics_middleware)


# Phase 14 / OBS-1: unhandled-exception handling — outer edge layer.
#
# Two complementary layers are needed because Starlette routes
# ``@app.exception_handler(Exception)`` to the OUTERMOST ServerErrorMiddleware,
# whose 500 response never flows back through the inner middleware chain
# (metrics counter, security headers) and which ALWAYS re-raises the exception
# for the server to log:
#
#   1. ``catch_unhandled_errors`` — registered FIRST in this file (above the
#      CORS block), so it sits innermost, directly above ExceptionMiddleware.
#      It converts route-level exceptions into a JSON 500 response INSIDE the
#      chain where the normal response path completes (metrics + security
#      headers).
#   2. ``unhandled_exception_handler`` (this handler) — the last-resort edge
#      handler: an exception that escapes every middleware (e.g. raised inside
#      another middleware) becomes the JSON error convention instead of
#      Starlette's bare plain-text "Internal Server Error".
#
# Both record the incident through the monitoring sentinel (structured log +
# Sentry + Telegram) and never leak internals to the client. For the common
# route-level case only the middleware layer fires (no double alerting).
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    method = getattr(request, "method", "WEBSOCKET")
    url = getattr(request, "url", None)
    path = getattr(url, "path", "?")
    _client = getattr(request, "client", None)
    client_host = _client[0] if isinstance(_client, tuple) else None

    logger = get_logger("main")
    logger.error(
        "Unhandled exception on %s %s: %s",
        method,
        path,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )

    from app.core.monitoring import monitoring_sentinel

    monitoring_sentinel.capture_exception(
        exc,
        context={"method": method, "path": path, "client_host": client_host},
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Mount routers
from app.api import admin, alerts, auth, backtest, billing, brokers, broker_cron, compliance, copy_trading, dashboard, market_data, payouts, quant_lab, reports, risk_guard, strategies, subscriptions, trades, user, visual_strategies, watchlist, websocket  # noqa: E402

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(billing.router)
app.include_router(subscriptions.router)
app.include_router(alerts.router)
app.include_router(visual_strategies.router)
app.include_router(brokers.router)
app.include_router(broker_cron.router)
app.include_router(compliance.router)
app.include_router(copy_trading.router)
app.include_router(dashboard.router)
app.include_router(strategies.router)
app.include_router(trades.router)
app.include_router(trades.dma_router)
app.include_router(market_data.router)
app.include_router(watchlist.router)
app.include_router(websocket.router)
app.include_router(user.router)
app.include_router(reports.router)
app.include_router(risk_guard.router)
app.include_router(backtest.router)
app.include_router(quant_lab.router)
app.include_router(payouts.router)


@app.get("/api/health", tags=["health"])
async def health_check():
    """Health check endpoint."""
    from app.market_data.manager import ws_manager

    return {
        "status": "healthy",
        "broker_mode": settings.broker_mode,
        "engine_running": _engine is not None,
        "ws_channels": ws_manager.channel_counts,
    }


@app.get("/healthz", tags=["health"])
async def healthz():
    """Liveness probe — instant, no external dependency calls."""
    return {"status": "healthy", "service": "tradethrone-platform"}


@app.get("/metrics", tags=["observability"], include_in_schema=False)
async def metrics_endpoint():
    """Prometheus text-format metrics (HTTP volume + runtime state).

    See ``app/core/metrics.py`` for the metric definitions.  Dynamic gauges
    (engine state, broker mode, websocket subscribers) are refreshed just-in-
    time so a scrape always reflects current state.
    """
    from app.core.metrics import broker_mode_live, engine_state, render_metrics, ws_channels
    from app.market_data.manager import ws_manager

    if engine_state is not None:
        engine_state.set(1 if _engine is not None else 0)
    if broker_mode_live is not None:
        broker_mode_live.set(1 if settings.broker_mode == "live" else 0)
    if ws_channels is not None:
        ws_channels.set(sum(ws_manager.channel_counts.values()))
    return render_metrics()


def _sanitize_error(exc: Exception) -> str:
    """First 200 chars of an error with any configured URL redacted.

    Prevents connection strings / credentials leaking into the readiness
    response body (the error surface most likely to be scraped by monitors).
    """
    message = str(exc)
    for configured in (settings.database_url, settings.effective_redis_url):
        if configured:
            message = message.replace(configured, "<redacted>")
    return message[:200]


@app.get("/readyz", tags=["health"])
async def readyz():
    """Readiness probe — verifies PostgreSQL (Supabase) and Redis (Upstash).

    Returns HTTP 200 only when the datastore is reachable.  The cache check
    is mandatory in production; outside production a cold/absent Redis is
    reported transparently without failing readiness so local dev stays
    friction-free.
    """
    import redis.asyncio as aioredis
    from sqlalchemy import text

    from app.db.session import engine as _db_engine

    checks: dict[str, bool] = {"database": False, "cache": False}
    errors: dict[str, str] = {}

    # 1. Database (Supabase PostgreSQL / local SQLite)
    try:
        async with _db_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:
        errors["database"] = _sanitize_error(exc)

    # 2. Cache (Upstash Redis TLS / local Redis)
    if not settings.effective_redis_url:
        # Deterministic diagnostic: no silent localhost fallback when the
        # operator explicitly left Redis unconfigured.
        errors["cache"] = "Redis not configured: set UPSTASH_REDIS_URL or REDIS_URL."
    else:
        try:
            client = aioredis.from_url(
                settings.effective_redis_url,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
                decode_responses=True,
            )
            try:
                await client.ping()
                checks["cache"] = True
            finally:
                await client.aclose()
        except Exception as exc:
            errors["cache"] = _sanitize_error(exc)

    cache_required = settings.environment == "production"
    ready = checks["database"] and (checks["cache"] or not cache_required)

    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else ("degraded" if checks["database"] else "not_ready"),
            "service": "tradethrone-platform",
            "environment": settings.environment,
            "checks": checks,
            **({"errors": errors} if errors else {}),
        },
    )
