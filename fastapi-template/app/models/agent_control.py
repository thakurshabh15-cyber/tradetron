"""Autonomous-agent control-plane ORM models (Phase 1 Step 4).

Two durable entities complete the agent traceability chain:

``agent_configs``
    One per-tenant autonomous-agent configuration (the Agent Console's durable
    state).  The browser is a CONTROL PLANE: every field is validated
    server-side and the row is the single authority the evaluation loop reads.
    ``status`` is a CAS-protected lifecycle state machine (``IDLE → RUNNING ⇄
    PAUSED → STOPPED``, ``FAILED`` set only by the server on evaluation
    failure).  ``execution_mode`` is restricted to the canonical persisted
    modes ``PAPER | LIVE`` (CHECK); ``LIVE`` configs are refused by the API
    unless the broker is already in live mode (fail closed at the control
    plane, never widened at execution).

``agent_decisions``
    One durable row per deterministic agent decision (Decision contract:
    ``NO_TRADE | TRADE | NEEDS_APPROVAL | REJECTED | FAILED``), carrying the
    decision inputs, the risk/approval/execution outcomes and the task/intent
    linkage:  agent_config → task → decision → intent → order → position.

These rows mirror migration 0011 exactly (both on SQLite and PostgreSQL); the
CI alembic drift guard compares the create_all schema with the migrated
schema, so every column here is authoritative.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Lifecycle status vocabulary (durable; STARTING/STOPPING + activity states
# ── EXECUTING/WAITING_APPROVAL are derived at read time from in-flight
# ── control requests and the newest task/intent/decision rows) ─────────────
AGENT_STATUS_IDLE = "IDLE"
AGENT_STATUS_RUNNING = "RUNNING"
AGENT_STATUS_PAUSED = "PAUSED"
AGENT_STATUS_STOPPED = "STOPPED"
AGENT_STATUS_FAILED = "FAILED"
DURABLE_STATUSES = (
    AGENT_STATUS_IDLE,
    AGENT_STATUS_RUNNING,
    AGENT_STATUS_PAUSED,
    AGENT_STATUS_STOPPED,
    AGENT_STATUS_FAILED,
)

# ── Autonomy vocabulary (Phase G ladder; never widened by the browser) ─────
AUTONOMY_OBSERVE = 0      # observe only — records decisions, never trades
AUTONOMY_APPROVAL = 1     # decisions + human approval required
AUTONOMY_PAPER = 2        # PAPER autonomous execution
AUTONOMY_LIVE = 3         # LIVE autonomous execution (server gates required)
AUTONOMY_LEVELS = (AUTONOMY_OBSERVE, AUTONOMY_APPROVAL, AUTONOMY_PAPER, AUTONOMY_LIVE)

# ── Decision contract (Phase F) ────────────────────────────────────────────
DECISION_NO_TRADE = "NO_TRADE"
DECISION_TRADE = "TRADE"
DECISION_NEEDS_APPROVAL = "NEEDS_APPROVAL"
DECISION_REJECTED = "REJECTED"
DECISION_FAILED = "FAILED"
DECISIONS = (
    DECISION_NO_TRADE,
    DECISION_TRADE,
    DECISION_NEEDS_APPROVAL,
    DECISION_REJECTED,
    DECISION_FAILED,
)

# ── Persisted execution modes ───────────────────────────────────────────────
MODE_PAPER = "PAPER"
MODE_LIVE = "LIVE"
PERSISTED_MODES = (MODE_PAPER, MODE_LIVE)
class AgentConfigRecord(Base):
    """Per-tenant autonomous-agent configuration (Agent Console state)."""

    __tablename__ = "agent_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    symbols_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    execution_mode: Mapped[str] = mapped_column(String(10), default=MODE_PAPER)
    autonomy_level: Mapped[int] = mapped_column(Integer, default=AUTONOMY_OBSERVE)
    approval_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    risk_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Lifecycle state machine (CAS-protected by the control service).
    status: Mapped[str] = mapped_column(String(20), default=AGENT_STATUS_IDLE)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", name="ux_agent_configs_user_id"),
        Index("ix_agent_configs_status", "status"),
        CheckConstraint(
            "status IN ('IDLE', 'RUNNING', 'PAUSED', 'STOPPED', 'FAILED')",
            name="ck_agent_configs_status",
        ),
        CheckConstraint(
            "execution_mode IN ('PAPER', 'LIVE')", name="ck_agent_configs_mode"
        ),
        CheckConstraint(
            "autonomy_level IN (0, 1, 2, 3)", name="ck_agent_configs_autonomy"
        ),
    )
class AgentDecisionRecord(Base):
    """One durable, deterministic decision produced by an agent evaluation."""

    __tablename__ = "agent_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    agent_config_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_configs.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_tasks.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Link to the governed intent/order (set after execution, never fabricated).
    intent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    side: Mapped[str | None] = mapped_column(String(10), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str] = mapped_column(String(10), default=MODE_PAPER)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Persisted gate outcomes (honest decision trail).
    risk_result: Mapped[str | None] = mapped_column(String(30), nullable=True)
    risk_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_result: Mapped[str | None] = mapped_column(String(30), nullable=True)
    execution_result: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_agent_decisions_created_at", "created_at"),
        CheckConstraint(
            "decision IN ('NO_TRADE', 'TRADE', 'NEEDS_APPROVAL', 'REJECTED', 'FAILED')",
            name="ck_agent_decisions_decision",
        ),
        CheckConstraint("mode IN ('PAPER', 'LIVE')", name="ck_agent_decisions_mode"),
    )


__all__ = [
    "AgentConfigRecord",
    "AgentDecisionRecord",
    "AGENT_STATUS_IDLE",
    "AGENT_STATUS_RUNNING",
    "AGENT_STATUS_PAUSED",
    "AGENT_STATUS_STOPPED",
    "AGENT_STATUS_FAILED",
    "DURABLE_STATUSES",
    "AUTONOMY_OBSERVE",
    "AUTONOMY_APPROVAL",
    "AUTONOMY_PAPER",
    "AUTONOMY_LIVE",
    "AUTONOMY_LEVELS",
    "DECISION_NO_TRADE",
    "DECISION_TRADE",
    "DECISION_NEEDS_APPROVAL",
    "DECISION_REJECTED",
    "DECISION_FAILED",
    "DECISIONS",
    "MODE_PAPER",
    "MODE_LIVE",
    "PERSISTED_MODES",
]