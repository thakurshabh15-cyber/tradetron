"""SQLAlchemy ORM models for the trading platform, orders, positions, trades, and strategy execution modes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class StrategyRecord(Base):
    """Persisted strategy configuration with execution mode (PAPER vs LIVE) and broker routing."""

    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_id
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    symbols_json: Mapped[str] = mapped_column(
        Text, nullable=False, doc="JSON array of symbol strings"
    )
    conditions_json: Mapped[str] = mapped_column(
        Text, nullable=False, doc="JSON array of Condition dicts"
    )
    action_json: Mapped[str] = mapped_column(
        Text, nullable=False, doc="JSON dict of Action fields"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    execution_mode: Mapped[str] = mapped_column(String(20), default="PAPER")  # "PAPER" | "LIVE"
    broker_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True
    )
    capital_allocated: Mapped[float] = mapped_column(Float, default=10000.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_strategies_user_id", "user_id"),
        Index("ix_strategies_mode", "execution_mode"),
        Index("ix_strategies_broker_account", "broker_account_id"),
    )


class OrderRecord(Base):
    """Every order placed by the engine with user, broker, execution mode, and status tracking."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_id
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    strategy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    broker_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), default="MARKET")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING, OPEN, FILLED, REJECTED, CANCELLED
    mode: Mapped[str] = mapped_column(String(20), default="PAPER")  # "PAPER" | "LIVE"
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc=(
            "Optional caller-supplied idempotency key. Durable per-user unique "
            "(see ux_orders_user_client_order_id partial index). A request "
            "carrying one of these is claimed as PENDING before broker dispatch "
            "so a retry can never double-execute."
        ),
    )
    signal_key: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc=(
            "Tenant-less durable idempotency key for signal-created orders "
            "(TradeThrone webhook path, see ux_orders_signal_key partial "
            "index). GLOBALLY unique — webhook signals have no owning user — "
            "and distinct from the per-user client_order_id, so a tenant-less "
            "signal can never collide with a user-scoped order or another "
            "tenant's identical key string."
        ),
    )
    position_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        doc="Position row created for this order (idempotent-replay linkage).",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        Index("ix_orders_symbol", "symbol"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_mode", "mode"),
        Index("ix_orders_user_created", "user_id", "created_at"),
        # Durable financial idempotency invariant: an idempotency key is unique
        # per user. Partial so legacy unkeyed rows (NULL client_order_id) never
        # collide, and NULL user_id rows (FK SET NULL) never collide either.
        Index(
            "ux_orders_user_client_order_id",
            "user_id",
            "client_order_id",
            unique=True,
            sqlite_where=text("client_order_id IS NOT NULL"),
            postgresql_where=text("client_order_id IS NOT NULL"),
        ),
        # Tenant-less signal idempotency (P0-2): a webhook signal claim must be
        # unique GLOBALLY because signals carry no owning user.  Partial so
        # legacy rows (NULL signal_key) never collide.  Deliberately SEPARATE
        # from the per-user client_order_id index: the same key string used by
        # two different users (or by a user as their DMA key) stays legal while
        # the tenant-less key space is unambiguous.
        Index(
            "ux_orders_signal_key",
            "signal_key",
            unique=True,
            sqlite_where=text("signal_key IS NOT NULL"),
            postgresql_where=text("signal_key IS NOT NULL"),
        ),
    )


class PositionRecord(Base):
    """Real-time active open/closed positions with live mark-to-market and SL/TP bounds."""

    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    strategy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    broker_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # LONG, SHORT
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="PAPER")  # "PAPER" | "LIVE"
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN, CLOSED
    # ── Phase 15C: exchange-level protection lifecycle state ─────────────────
    # LIVE:   UNPROTECTED | PROTECTION_PENDING | PROTECTED | PROTECTION_FAILED
    #         | STOP_TRIGGERED | TARGET_TRIGGERED (broker-reported terminal leg)
    # PAPER:  PAPER (engine-simulated SL/TP) when SL/TP configured, else
    #         UNPROTECTED.  CLOSED after position close / protection teardown.
    # PROTECTED is set ONLY after genuine broker protective-order placement —
    # never fabricated, never assumed from engine monitoring.
    protection_state: Mapped[str] = mapped_column(
        String(20), default="UNPROTECTED", server_default="UNPROTECTED"
    )
    protection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    protected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_positions_user_symbol", "user_id", "symbol"),
        Index("ix_positions_status", "status"),
        Index("ix_positions_mode", "mode"),
    )


class TradeRecord(Base):
    """Executed trade fills with audit parameters and PnL metrics."""

    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_id
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    strategy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    strategy_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="PAPER")  # "PAPER" | "LIVE"
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        Index("ix_trades_symbol", "symbol"),
        Index("ix_trades_executed", "executed_at"),
        Index("ix_trades_mode", "mode"),
        Index("ix_trades_user_executed", "user_id", "executed_at"),
    )
