"""Autonomous-agent system ORM models (Phase 1 Step 1: database foundation).

These three tables are the durable backbone for the autonomous-agent runtime
(scheduler / agents / API land in later steps — Step 1 deliberately ships the
schema only, with no runtime code):

``agents``
    Registry of every deployable agent type.  ``agent_type`` is the stable
    unique identifier used by the scheduler and by ``agent_tasks``; each agent
    declares its capabilities (``capabilities_json``) and its safety envelope
    (``readonly``, ``max_autonomy_level``), which gate how much unsupervised
    authority a given agent is ever allowed to exercise.

``agent_tasks``
    Durable work queue of agent executions: one row per (idempotent) task with
    retry accounting, JSON request/response/error payloads, approval gating
    (``requires_approval`` + ``approved_by``/``approved_at``/
    ``approval_expires_at``) and execution timestamps.  ``idempotency_key``
    makes re-dispatch safe; ``heartbeat_at`` lets the future runtime detect
    crashed workers.

``agent_runtime_config``
    Global singleton (enforced via ``CHECK(id = 1)``) carrying the system-wide
    autonomous-operation switch and autonomy ceiling.  The future runtime
    consults this row before granting any agent autonomous execution.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class AgentRecord(Base):
    """Registered autonomous-agent definition (one row per agent type)."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    # Stable, unique runtime key referenced by agent_tasks and the scheduler.
    agent_type: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    capabilities_json: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. ["READ", "ANALYZE"]
    # Safety envelope: readonly agents may never mutate state; max_autonomy_level
    # caps how much unsupervised authority this agent can be granted.
    # Python-side defaults mirror the migration server_defaults below (the
    # broker_state / trading house pattern) so ORM create_all and the 0009
    # migration produce identical DDL on both SQLite and PostgreSQL.
    readonly: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    max_autonomy_level: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class AgentTaskRecord(Base):
    """Durable autonomous-agent task (one row per task dispatch)."""

    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    agent_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    task_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="PENDING", server_default="PENDING", index=True
    )
    input_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    # Approval gating: a task that requires approval must not start autonomously
    # until a human (users.id) approves before approval_expires_at.
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    approved_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[str] = mapped_column(
        String(20), default="user", server_default="user"
    )  # user | agent | system
    timeout_seconds: Mapped[float] = mapped_column(Float, default=30.0, server_default="30")
    # Retry / liveness bookkeeping.
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Hot queue pick-up: PENDING/retryable rows by (status, approval window).
        Index("ix_agent_tasks_status_approval_expires", "status", "approval_expires_at"),
    )


class AgentRuntimeConfigRecord(Base):
    """Global singleton switch for autonomous-agent mode.

    Exactly one row is allowed (id = 1, enforced by the CHECK constraint).
    ``autonomous_mode_enabled`` is the master kill-switch and
    ``global_autonomy_level`` the platform-wide ceiling the runtime consults
    before any agent is permitted to act without a human in the loop.
    """

    __tablename__ = "agent_runtime_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    autonomous_mode_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    global_autonomy_level: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)  # users.id
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_agent_runtime_config_singleton"),
    )


__all__ = ["AgentRecord", "AgentTaskRecord", "AgentRuntimeConfigRecord"]