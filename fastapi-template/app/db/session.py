"""Async SQLAlchemy engine and session factory for SQLite and PostgreSQL.

Supports:
  - Local SQLite (default) via ``aiosqlite``
  - Hosted PostgreSQL (Neon, Supabase, Railway, Render, etc.) via ``asyncpg``
  - Automatic connection string normalization (postgres://, postgresql:// -> postgresql+asyncpg://)
  - Resilient, synchronous table creation and idempotent schema synchronization
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.core.logging import get_logger

logger = get_logger("db")


def normalize_database_url(raw_url: str) -> str:
    """Normalize raw database URLs from various cloud hosts for SQLAlchemy AsyncEngine."""
    if not raw_url or raw_url.strip() == "":
        return f"sqlite+aiosqlite:///{settings.BASE_DIR / 'trading.db'}"

    url = raw_url.strip()

    # Convert common postgres uri schemes to postgresql+asyncpg://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and not url.startswith("postgresql+"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # Handle query parameters for asyncpg (e.g. sslmode -> ssl)
    if "postgresql+asyncpg://" in url:
        parsed = urlsplit(url)
        if parsed.query:
            query_params = dict(parse_qsl(parsed.query))
            # asyncpg accepts ssl=require or ssl=True
            if "sslmode" in query_params:
                ssl_val = query_params.pop("sslmode")
                if ssl_val in ("require", "verify-ca", "verify-full"):
                    query_params["ssl"] = "require"
            new_query = urlencode(query_params)
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))

    return url


NORMALIZED_DB_URL = normalize_database_url(settings.database_url)
IS_SQLITE = NORMALIZED_DB_URL.startswith("sqlite")


def create_engine_instance() -> AsyncEngine:
    """Create and configure AsyncEngine with optimized parameters per database dialect."""
    if IS_SQLITE:
        return create_async_engine(
            NORMALIZED_DB_URL,
            echo=False,
            connect_args={"check_same_thread": False},
        )
    else:
        # PostgreSQL with asyncpg connection pooling
        return create_async_engine(
            NORMALIZED_DB_URL,
            echo=False,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=300,
        )


engine = create_engine_instance()

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


async def init_db() -> None:
    """Create all tables if they don't exist and run non-breaking schema additions."""
    from app.models.user import UserRecord, RevokedTokenRecord  # noqa: F401
    from app.models.broker_account import BrokerAccountRecord  # noqa: F401
    from app.models.trading import (  # noqa: F401
        OrderRecord,
        PositionRecord,
        StrategyRecord,
        TradeRecord,
    )
    from app.models.marketplace import (  # noqa: F401
        MarketplaceStrategyRecord,
        StrategyDeploymentRecord,
    )
    from app.models.watchlist import (  # noqa: F401
        WatchlistRecord,
        PriceAlertRecord,
    )
    from app.models.notification import (  # noqa: F401
        NotificationPreferenceRecord,
    )
    from app.models.billing import (  # noqa: F401
        SubscriptionRecord,
        PaymentRecord,
        InvoiceRecord,
        PlanRecord,
    )
    from app.models.audit import AuditLogRecord  # noqa: F401

    logger.info("Initializing database tables on %s...", "SQLite" if IS_SQLITE else "PostgreSQL")

    # 1. Create all tables reliably before any queries are made
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        bool_default_false = "0" if IS_SQLITE else "FALSE"
        bool_default_true = "1" if IS_SQLITE else "TRUE"

        # Incremental migration safeguards for pre-existing tables
        migrations = [
            ("users", "profile_photo TEXT"),
            ("users", "phone VARCHAR(30)"),
            ("users", "kyc_status VARCHAR(20) DEFAULT 'PENDING'"),
            ("users", f"two_factor_enabled BOOLEAN DEFAULT {bool_default_false}"),
            ("users", "failed_login_attempts INTEGER DEFAULT 0"),
            ("users", "locked_until TIMESTAMP"),
            ("users", f"is_verified BOOLEAN DEFAULT {bool_default_true}"),
            ("orders", "user_id VARCHAR(36)"),
            ("orders", "broker_account_id VARCHAR(36)"),
            ("orders", "price FLOAT"),
            ("orders", "filled_price FLOAT"),
            ("orders", "filled_quantity INTEGER DEFAULT 0"),
            ("orders", "mode VARCHAR(20) DEFAULT 'PAPER'"),
            ("orders", "error_message TEXT"),
            ("trades", "user_id VARCHAR(36)"),
            ("trades", "pnl_pct FLOAT"),
            ("trades", "exit_reason VARCHAR(50)"),
            ("trades", "entry_price FLOAT"),
            ("trades", "exit_price FLOAT"),
            ("trades", "mode VARCHAR(20) DEFAULT 'PAPER'"),
            ("strategies", "user_id VARCHAR(36)"),
            ("strategies", "execution_mode VARCHAR(20) DEFAULT 'PAPER'"),
            ("strategies", "broker_account_id VARCHAR(36)"),
            ("strategies", "capital_allocated FLOAT DEFAULT 10000.0"),
            ("positions", "user_id VARCHAR(36)"),
            ("positions", "strategy_id VARCHAR(36)"),
            ("positions", "broker_account_id VARCHAR(36)"),
            ("positions", "mode VARCHAR(20) DEFAULT 'PAPER'"),
            ("broker_accounts", "token_expires_at TIMESTAMP"),
            ("subscriptions", "razorpay_subscription_id VARCHAR(100)"),
            ("subscriptions", "razorpay_customer_id VARCHAR(100)"),
            ("payments", "order_id VARCHAR(100)"),
            ("payments", "method VARCHAR(50)"),
            ("payments", "error_reason VARCHAR(255)"),
            ("invoices", "plan_name VARCHAR(50) DEFAULT 'PRO'"),
            ("invoices", "gstin VARCHAR(50)"),
            ("invoices", "billing_address TEXT"),
        ]

        for table, col_def in migrations:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))
            except Exception:
                pass

    # 2. Seed Default Plans, Strategies & Watchlist if not present
    async with SessionLocal() as session:
        try:
            existing_plans = (await session.execute(select(PlanRecord))).scalars().all()
            if not existing_plans:
                default_plans = [
                    PlanRecord(
                        name="FREE",
                        display_name="Free Starter",
                        description="Essential tools for paper trading and strategy testing",
                        price_monthly=0.0,
                        price_yearly=0.0,
                        currency="INR",
                        features_json=json.dumps({
                            "max_live_strategies": 1,
                            "max_brokers": 1,
                            "tick_speed": "1s",
                            "historical_candles": "15m",
                            "priority_support": False,
                            "vip_vps": False,
                        }),
                    ),
                    PlanRecord(
                        name="PRO",
                        display_name="Pro Trader",
                        description="Full multi-broker execution, 10 live strategies, and 1m real candles",
                        price_monthly=1999.0,
                        price_yearly=19990.0,
                        currency="INR",
                        features_json=json.dumps({
                            "max_live_strategies": 10,
                            "max_brokers": 5,
                            "tick_speed": "realtime",
                            "historical_candles": "1m",
                            "priority_support": True,
                            "vip_vps": False,
                        }),
                    ),
                    PlanRecord(
                        name="ELITE",
                        display_name="Elite Institutional",
                        description="Unlimited strategies, dedicated VIP execution VPS, and 24/7 hotline",
                        price_monthly=4999.0,
                        price_yearly=49990.0,
                        currency="INR",
                        features_json=json.dumps({
                            "max_live_strategies": 999,
                            "max_brokers": 99,
                            "tick_speed": "sub-millisecond",
                            "historical_candles": "tick",
                            "priority_support": True,
                            "vip_vps": True,
                        }),
                    ),
                ]
                session.add_all(default_plans)
                await session.commit()
                logger.info("Default subscription plans seeded (FREE, PRO, ELITE)")

            # Seed Default Trading Strategies if DB is empty
            existing_strats = (await session.execute(select(StrategyRecord))).scalars().all()
            if not existing_strats:
                default_strategies = [
                    StrategyRecord(
                        name="Golden Cross SMA Trend 50/200",
                        symbols_json=json.dumps(["AAPL", "NVDA", "MSFT"]),
                        conditions_json=json.dumps([
                            {"indicator": "sma_fast", "operator": "gt", "threshold": 0.0}
                        ]),
                        action_json=json.dumps({"side": "BUY", "quantity": 10}),
                        enabled=True,
                        execution_mode="PAPER",
                        capital_allocated=15000.0,
                    ),
                    StrategyRecord(
                        name="RSI 14 Oversold Mean Reversion",
                        symbols_json=json.dumps(["GOOGL", "AMZN", "AAPL"]),
                        conditions_json=json.dumps([
                            {"indicator": "rsi", "operator": "lt", "threshold": 30.0}
                        ]),
                        action_json=json.dumps({"side": "BUY", "quantity": 5}),
                        enabled=True,
                        execution_mode="PAPER",
                        capital_allocated=10000.0,
                    ),
                    StrategyRecord(
                        name="Bollinger Band Volatility Breakout",
                        symbols_json=json.dumps(["NVDA", "MSFT"]),
                        conditions_json=json.dumps([
                            {"indicator": "price", "operator": "gt", "threshold": 100.0}
                        ]),
                        action_json=json.dumps({"side": "BUY", "quantity": 15}),
                        enabled=True,
                        execution_mode="PAPER",
                        capital_allocated=20000.0,
                    ),
                ]
                session.add_all(default_strategies)
                await session.commit()
                logger.info("Default quantitative strategies seeded (Golden Cross, RSI Mean Reversion, BB Breakout)")

            # Seed Watchlist if DB is empty
            existing_watchlist = (await session.execute(select(WatchlistRecord))).scalars().all()
            if not existing_watchlist:
                default_symbols = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]
                for sym in default_symbols:
                    session.add(WatchlistRecord(symbol=sym, notes="Core equity index constituent"))
                await session.commit()
                logger.info("Default watchlist seeded (AAPL, MSFT, NVDA, GOOGL, AMZN)")

        except Exception as exc:
            logger.warning("Notice on default data seeding: %s", exc)

    logger.info("Database initialized successfully on %s", "SQLite" if IS_SQLITE else "PostgreSQL")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a scoped async session."""
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
