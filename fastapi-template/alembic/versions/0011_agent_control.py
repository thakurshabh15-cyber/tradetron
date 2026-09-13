"""agent_control (Phase 1 Step 4: autonomous operations + control plane)

Revision ID: 0011_agent_control
Revises: 0010_agent_trading_intents
Create Date: 2026-09-13

Creates the two durable entities that complete the agent traceability chain:

``agent_configs``
    Per-tenant autonomous-agent configuration — the CONFIGURABLE backend
    state the frontend console reads/writes.  ``status`` is the server-side
    lifecycle state machine (CAS-protected in app.engine.agent_control);
    ``execution_mode`` is restricted to the canonical persisted modes
    ``PAPER | LIVE`` and ``autonomy_level`` to the 0..3 ladder.  Python-side
    defaults mirror the ORM exactly (no server defaults — the CI parity guard
    compares the raw DDL fingerprint against create_all).

``agent_decisions``
    One durable row per deterministic agent decision (Decision contract
    ``NO_TRADE | TRADE | NEEDS_APPROVAL | REJECTED | FAILED``) with the
    decision inputs, risk/approval/execution outcomes and the task/intent
    linkage: agent_config -> task -> decision -> intent -> order.

Guards (parity with 0009/0010): on databases migrated from a clean baseline,
0001's ``Base.metadata.create_all`` already created these tables, so this
revision creates them only when absent and creates missing indexes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0011_agent_control"
down_revision: Union[str, Sequence[str], None] = "0010_agent_trading_intents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the agent_configs / agent_decisions tables (guarded)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if _CONFIGS not in existing:
        op.create_table(
            _CONFIGS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "user_id", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column(
                "strategy_id", sa.String(length=36),
                sa.ForeignKey("strategies.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("symbols_json", sa.Text(), nullable=False),
            sa.Column("execution_mode", sa.String(length=10), nullable=False),
            sa.Column("autonomy_level", sa.Integer(), nullable=False),
            sa.Column("approval_policy_json", sa.Text(), nullable=False),
            sa.Column("risk_policy_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", name=_UX_CONFIGS_USER),
            sa.CheckConstraint(
                "status IN ('IDLE', 'RUNNING', 'PAUSED', 'STOPPED', 'FAILED')",
                name="ck_agent_configs_status",
            ),
            sa.CheckConstraint(
                "execution_mode IN ('PAPER', 'LIVE')", name="ck_agent_configs_mode"
            ),
            sa.CheckConstraint(
                "autonomy_level IN (0, 1, 2, 3)", name="ck_agent_configs_autonomy"
            ),
        )

    if _DECISIONS not in existing:
        op.create_table(
            _DECISIONS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "agent_config_id", sa.String(length=36),
                sa.ForeignKey("agent_configs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "user_id", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=False,
            ),
            sa.Column(
                "task_id", sa.String(length=36),
                sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("strategy_id", sa.String(length=36), nullable=True),
            sa.Column("intent_id", sa.String(length=36), nullable=True),
            sa.Column("order_id", sa.String(length=36), nullable=True),
            sa.Column("decision", sa.String(length=20), nullable=False),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("side", sa.String(length=10), nullable=True),
            sa.Column("quantity", sa.Integer(), nullable=True),
            sa.Column("mode", sa.String(length=10), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("risk_result", sa.String(length=30), nullable=True),
            sa.Column("risk_reason", sa.Text(), nullable=True),
            sa.Column("approval_result", sa.String(length=30), nullable=True),
            sa.Column("execution_result", sa.String(length=30), nullable=True),
            sa.Column("error_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "decision IN ('NO_TRADE', 'TRADE', 'NEEDS_APPROVAL', 'REJECTED', 'FAILED')",
                name="ck_agent_decisions_decision",
            ),
            sa.CheckConstraint(
                "mode IN ('PAPER', 'LIVE')", name="ck_agent_decisions_mode"
            ),
        )

    # ---- Indexes (guarded against drifting/legacy databases) ----
    for table, indexes in (
        (_CONFIGS, (_IX_CONFIGS_STATUS,)),
        (
            _DECISIONS,
            (
                _IX_DECISIONS_CONFIG,
                _IX_DECISIONS_USER,
                _IX_DECISIONS_TASK,
                _IX_DECISIONS_INTENT,
                _IX_DECISIONS_CREATED,
            ),
        ),
    ):
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(table)}
        for name in indexes:
            if name not in existing_indexes:
                op.create_index(name, table, _INDEX_COLUMNS[name])

def downgrade() -> None:
    """Reverse the schema change (best-effort drop)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if _DECISIONS in existing:
        index_names = {ix["name"] for ix in inspector.get_indexes(_DECISIONS)}
        for index in (
            _IX_DECISIONS_CREATED,
            _IX_DECISIONS_INTENT,
            _IX_DECISIONS_TASK,
            _IX_DECISIONS_USER,
            _IX_DECISIONS_CONFIG,
        ):
            if index in index_names:
                op.drop_index(index, table_name=_DECISIONS)
        op.drop_table(_DECISIONS)

    if _CONFIGS in existing:
        index_names = {ix["name"] for ix in inspector.get_indexes(_CONFIGS)}
        if _IX_CONFIGS_STATUS in index_names:
            op.drop_index(_IX_CONFIGS_STATUS, table_name=_CONFIGS)
        op.drop_table(_CONFIGS)
_CONFIGS = "agent_configs"
_DECISIONS = "agent_decisions"

_UX_CONFIGS_USER = "ux_agent_configs_user_id"
_IX_CONFIGS_STATUS = "ix_agent_configs_status"
_IX_DECISIONS_CONFIG = "ix_agent_decisions_agent_config_id"
_IX_DECISIONS_USER = "ix_agent_decisions_user_id"
_IX_DECISIONS_TASK = "ix_agent_decisions_task_id"
_IX_DECISIONS_INTENT = "ix_agent_decisions_intent_id"
_IX_DECISIONS_CREATED = "ix_agent_decisions_created_at"

_INDEX_COLUMNS = {
    _IX_CONFIGS_STATUS: ["status"],
    _IX_DECISIONS_CONFIG: ["agent_config_id"],
    _IX_DECISIONS_USER: ["user_id"],
    _IX_DECISIONS_TASK: ["task_id"],
    _IX_DECISIONS_INTENT: ["intent_id"],
    _IX_DECISIONS_CREATED: ["created_at"],
}