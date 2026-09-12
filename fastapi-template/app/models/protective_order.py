"""Protective-order ORM model (Phase 15C: exchange-level SL/TP protection).

A per-leg, per-position durable ledger of broker-side protective orders
(stop-loss and take-profit).  The row is the single source of truth for the
protective-order lifecycle — placement idempotency, broker order references,
rejection/trigger handling and crash recovery.

Lifecycle rules (enforced by ``app/engine/protective_orders.py``):

  * Idempotency: at most ONE live row per ``(position_id, leg)`` — enforced by
    a partial unique index.  A retry CAS-reclaims a FAILED/CANCELLED row;
    PENDING_PLACEMENT/PLACED rows are never re-dispatched.
  * Broker truth: ``broker_protective_order_id`` is stored in its own commit
    IMMEDIATELY after broker acceptance (crash-window hardening), mirroring
    the durable-claim pattern for entry orders.
  * NO FABRICATION: a row may only reach PLACED/COMPLETE with a genuine broker
    order reference.  A placement that fails, or a crash before the reference
    was persisted, MUST leave the row FAILED/PENDING so the position is
    honestly PROTECTION_PENDING / PROTECTION_FAILED — never claimed PROTECTED.
  * Tenant isolation: every row carries ``user_id`` + ``broker_account_id``
    and every operation re-validates position ownership.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class ProtectiveOrderRecord(Base):
    """One broker-side protective order leg targeting one open position."""

    __tablename__ = "protective_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    broker_account_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("broker_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    position_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("positions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(20), default="LIVE")  # LIVE (never PAPER)
    leg: Mapped[str] = mapped_column(String(20), nullable=False)  # STOP_LOSS | TAKE_PROFIT
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY | SELL (closing side)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Trigger price (the SL/TP level).  For TP_LIMIT / SL_LIMIT legs the limit
    # price is stored separately; for SL_MARKET legs it is None.
    trigger_price: Mapped[float] = mapped_column(Float, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Broker order-type literal actually dispatched (documented per adapter):
    # SL_MARKET | SL_LIMIT | TP_LIMIT
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Broker-assigned reference — ``None`` until the broker ACKs the placement.
    broker_protective_order_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    # Local lifecycle: PENDING_PLACEMENT | PLACED | COMPLETE | CANCELLED |
    # FAILED | RESOLVED
    status: Mapped[str] = mapped_column(String(20), default="PENDING_PLACEMENT", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Broker-reported status at last read (from get_order_status / postback):
    # OPEN | FILLED | CANCELLED | REJECTED | EXPIRED | UNKNOWN | None
    broker_reported_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        # One live row per (position, leg): the idempotency backstop.  A retry
        # CAS-reclaims the existing row instead of inserting a duplicate.
        Index("ux_protective_orders_position_leg", "position_id", "leg", unique=True),
        Index("ix_protective_orders_acc_status", "broker_account_id", "status"),
        Index("ix_protective_orders_broker_ref", "broker_protective_order_id"),
    )


__all__ = ["ProtectiveOrderRecord"]