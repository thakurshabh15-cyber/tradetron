"""Autonomous-agent trading-intent ORM model (Phase 1 Step 3).

``trading_intents`` is the structured, durable contract an agent decision uses
to request trade execution — the ONLY bridge between the agent runtime and the
existing governed execution pipeline.  Every field a frontend or operator
might want to override server-side is validated again at execution time by
``app.engine.agent_intents``; this row is the durable record, never a
wish-list.

Lifecycle vocabulary (mirrors the SQL CHECK constraints in migration 0010):
  decision ... TRADE | NEEDS_APPROVAL | NO_TRADE | REJECTED | FAILED
                (NO_TRADE / REJECTED / FAILED decisions are recorded in the
                 agent task output — only TRADE / NEEDS_APPROVAL create a row)
  status .... CREATED → SENT_FOR_EXECUTION → EXECUTED | REJECTED | FAILED
                (SENT_FOR_EXECUTION is claimed atomically from CREATED so a
                 concurrent worker can never dispatch the same intent twice)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class TradingIntentRecord(Base):
    """One durable, idempotent trading intent produced by an agent task."""

    __tablename__ = "trading_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    # Exactly one intent per (winning) agent decision.
    agent_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_tasks.id", ondelete="SET NULL"),
        nullable=False, index=True, unique=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=False, index=True,
    )
    strategy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    broker_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True
    )

    # ── Trade request (the intent) ─────────────────────────────────────
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY | SELL
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), default="MARKET")
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Decision / lifecycle / mode ────────────────────────────────────
    decision: Mapped[str] = mapped_column(String(20), default="TRADE")
    status: Mapped[str] = mapped_column(String(20), default="CREATED", index=True)
    requested_mode: Mapped[str] = mapped_column(String(10), default="PAPER")  # PAPER | LIVE
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Persisted gate outcomes (honest rejection/execution trail) ─────
    market_data_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    market_data_age_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    risk_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    margin_required: Mapped[float | None] = mapped_column(Float, nullable=True)
    execution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Result linkage (agent_task → intent → order → position) ────────
    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    position_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    protective_order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_intent_side"),
        CheckConstraint(
            "decision IN ('TRADE', 'NEEDS_APPROVAL', 'NO_TRADE', 'REJECTED', 'FAILED')",
            name="ck_intent_decision",
        ),
        CheckConstraint(
            "status IN ('CREATED', 'SENT_FOR_EXECUTION', 'EXECUTED', 'REJECTED', 'FAILED', 'CLOSED')",
            name="ck_intent_status",
        ),
        CheckConstraint("requested_mode IN ('PAPER', 'LIVE')", name="ck_intent_mode"),
        CheckConstraint("order_type IN ('MARKET', 'LIMIT')", name="ck_intent_order_type"),
    )


__all__ = ["TradingIntentRecord"]