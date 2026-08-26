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

from fastapi import FastAPI
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
)

# Dynamic CORS Configuration
# Hardened defaults: a wildcard origin is NEVER combined with credentials in
# production — deployments without an explicit ALLOWED_ORIGINS lock to the
# official TradeThrone domains automatically.
_DEFAULT_PROD_ORIGINS = [
    "https://tradethrone.vercel.app",
    "https://tradethron.vercel.app",
    "https://*.vercel.app",
    # Local development frontends — safe to expose in production CORS since
    # attacker pages live on random public domains, never on the victim's
    # own localhost. Keeps `npm run dev` functional against any deployment.
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
]
cors_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
if settings.frontend_url and settings.frontend_url not in cors_origins and "*" not in cors_origins:
    cors_origins.append(settings.frontend_url)

if not cors_origins or cors_origins == ["*"]:
    if settings.environment == "production":
        cors_origins = _DEFAULT_PROD_ORIGINS.copy()
        print(f"[SECURITY] ALLOWED_ORIGINS unset in production — locking CORS to {cors_origins}")
    else:
        cors_origins = ["*"]

# Allow genuine wildcard Vercel preview/deploy subdomains in addition to the
# explicit exact origins. Starlette matches origins exactly (no *. expansion),
# so we also register an origin regex that accepts any *.vercel.app host.
_ALLOW_VERCEL_REGEX = r"https://([a-z0-9-]+\.)*vercel\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=_ALLOW_VERCEL_REGEX,
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
        errors["database"] = str(exc)[:200]

    # 2. Cache (Upstash Redis TLS / local Redis)
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
        errors["cache"] = str(exc)[:200]

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
