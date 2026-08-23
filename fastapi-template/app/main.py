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
cors_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
if settings.frontend_url and settings.frontend_url not in cors_origins and "*" not in cors_origins:
    cors_origins.append(settings.frontend_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins if cors_origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
from app.api import admin, alerts, auth, auth_otp, billing, brokers, broker_cron, copy_trading, dashboard, market_data, reports, strategies, subscriptions, trades, user, visual_strategies, watchlist, websocket  # noqa: E402

app.include_router(auth.router)
app.include_router(auth_otp.router)
app.include_router(admin.router)
app.include_router(billing.router)
app.include_router(subscriptions.router)
app.include_router(alerts.router)
app.include_router(visual_strategies.router)
app.include_router(brokers.router)
app.include_router(broker_cron.router)
app.include_router(copy_trading.router)
app.include_router(dashboard.router)
app.include_router(strategies.router)
app.include_router(trades.router)
app.include_router(market_data.router)
app.include_router(watchlist.router)
app.include_router(websocket.router)
app.include_router(user.router)
app.include_router(reports.router)


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
