"""agent_runtime (Phase 1 Step 1: autonomous-agent system database foundation)

Revision ID: 0009_agent_runtime
Revises: 0008_protective_orders
Create Date: 2026-09-13

Creates the three autonomous-agent backbone tables (``agents``,
``agent_tasks``, ``agent_runtime_config``) with their indexes, foreign keys and
the singleton CHECK constraint, then seeds the ``engineering_monitor`` agent and
the singleton runtime-config row.

Guards (parity with 0007_broker_state / 0008_protective_orders): the 0001
baseline delegates to ``Base.metadata.create_all``, so on databases migrated
from a clean baseline all three tables already exist before this revision runs.
When a table is already present it is left untouched and only missing indexes
are created; seed rows are inserted idempotently (never duplicated).  This keeps
the migration safe for the SQLite development/testing environment and for
PostgreSQL production alike.
"""
from datetime import datetime, timezone
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0009_agent_runtime"
down_revision: Union[str, Sequence[str], None] = "0008_protective_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AGENTS = "agents"
_AGENT_TASKS = "agent_tasks"
_RUNTIME_CFG = "agent_runtime_config"

_IX_AGENT_TASKS_STATUS = "ix_agent_tasks_status"
_IX_AGENT_TASKS_AGENT_TYPE = "ix_agent_tasks_agent_type"
_IX_AGENT_TASKS_CREATED_AT = "ix_agent_tasks_created_at"
_IX_AGENT_TASKS_STATUS_APPROVAL = "ix_agent_tasks_status_approval_expires"

_AGENT_TYPE_SEED = "engineering_monitor"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upgrade() -> None:
    """Create the three agent tables (guarded) and seed the initial rows."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    # ---- agents ----
    if _AGENTS not in existing:
        op.create_table(
            _AGENTS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("agent_type", sa.String(length=60), nullable=False, unique=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("capabilities_json", sa.Text(), nullable=False),
            sa.Column("readonly", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("max_autonomy_level", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    # ---- agent_tasks ----
    if _AGENT_TASKS not in existing:
        op.create_table(
            _AGENT_TASKS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("agent_type", sa.String(length=60), nullable=False),
            sa.Column("task_kind", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
            sa.Column("input_json", sa.Text(), nullable=False),
            sa.Column("output_json", sa.Text(), nullable=True),
            sa.Column("error_json", sa.Text(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("idempotency_key", sa.String(length=64), nullable=True, unique=True),
            sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "approved_by", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "requested_by", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("created_by", sa.String(length=20), nullable=False, server_default="user"),
            sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="30"),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(_IX_AGENT_TASKS_STATUS, _AGENT_TASKS, ["status"])
        op.create_index(_IX_AGENT_TASKS_AGENT_TYPE, _AGENT_TASKS, ["agent_type"])
        op.create_index(_IX_AGENT_TASKS_CREATED_AT, _AGENT_TASKS, ["created_at"])
        op.create_index(_IX_AGENT_TASKS_STATUS_APPROVAL, _AGENT_TASKS, ["status", "approval_expires_at"])
    else:
        # Table exists (e.g. local create_all bootstrap); only missing indexes
        # are created — JSON/unique constraints and CHECKs cannot be added to an
        # existing table on SQLite via ALTER, so they are skipped (0008 pattern).
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_AGENT_TASKS)}
        for name, columns in (
            (_IX_AGENT_TASKS_STATUS, ["status"]),
            (_IX_AGENT_TASKS_AGENT_TYPE, ["agent_type"]),
            (_IX_AGENT_TASKS_CREATED_AT, ["created_at"]),
            (_IX_AGENT_TASKS_STATUS_APPROVAL, ["status", "approval_expires_at"]),
        ):
            if name not in existing_indexes:
                op.create_index(name, _AGENT_TASKS, columns)

    # ---- agent_runtime_config ----
    if _RUNTIME_CFG not in existing:
        op.create_table(
            _RUNTIME_CFG,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("autonomous_mode_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("global_autonomy_level", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_by", sa.String(length=36), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("id = 1", name="ck_agent_runtime_config_singleton"),
        )

    # ---- Seed data (idempotent) ----
    _seed_agent(bind)
    _seed_runtime_config(bind)


def _seed_agent(bind) -> None:
    """Insert the engineering_monitor agent once (skipped if already present)."""
    existing_count = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {_AGENTS} WHERE agent_type = :agent_type"),
        {"agent_type": _AGENT_TYPE_SEED},
    ).scalar()
    if existing_count:
        return

    now = _utcnow()
    bind.execute(
        sa.text(
            f"INSERT INTO {_AGENTS} "
            "(id, agent_type, name, description, capabilities_json, readonly, "
            " max_autonomy_level, enabled, created_at, updated_at) "
            "VALUES (:id, :agent_type, :name, :description, :capabilities_json, "
            ":readonly, :max_autonomy_level, :enabled, :created_at, :updated_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_type": _AGENT_TYPE_SEED,
            "name": "Engineering Monitor",
            "description": None,
            "capabilities_json": '["READ", "ANALYZE"]',
            "readonly": True,
            "max_autonomy_level": 1,
            "enabled": False,
            "created_at": now,
            "updated_at": now,
        },
    )


def _seed_runtime_config(bind) -> None:
    """Insert the singleton runtime-config row (id=1) once, if absent."""
    existing_count = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {_RUNTIME_CFG} WHERE id = 1")
    ).scalar()
    if existing_count:
        return

    now = _utcnow()
    bind.execute(
        sa.text(
            f"INSERT INTO {_RUNTIME_CFG} "
            "(id, autonomous_mode_enabled, global_autonomy_level, updated_by, updated_at) "
            "VALUES (:id, :autonomous_mode_enabled, :global_autonomy_level, :updated_by, :updated_at)"
        ),
        {
            "id": 1,
            "autonomous_mode_enabled": False,
            "global_autonomy_level": 0,
            "updated_by": None,
            "updated_at": now,
        },
    )


def downgrade() -> None:
    """Reverse the schema change (best-effort drop of the agent tables)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    for table in (_RUNTIME_CFG, _AGENT_TASKS, _AGENTS):
        if table not in table_names:
            continue
        index_names = {ix["name"] for ix in inspector.get_indexes(table)}
        for index in (
            _IX_AGENT_TASKS_STATUS,
            _IX_AGENT_TASKS_AGENT_TYPE,
            _IX_AGENT_TASKS_CREATED_AT,
            _IX_AGENT_TASKS_STATUS_APPROVAL,
        ):
            if index in index_names:
                op.drop_index(index, table_name=table)
        op.drop_table(table)