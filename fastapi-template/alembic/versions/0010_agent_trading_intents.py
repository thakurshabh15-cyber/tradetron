"""agent_trading_intents (Phase 1 Step 3: governed agent → trading pipeline)

Revision ID: 0010_agent_trading_intents
Revises: 0009_agent_runtime
Create Date: 2026-09-13

Creates the durable ``trading_intents`` table — the structured, persisted
trading-intent contract produced by an agent decision — plus the
``orders.agent_intent_id`` traceability column and the ``trading_agent``
registry row (enabled=False, fail-closed default).

An intent is the ONLY way an autonomous agent requests trade execution: it
carries symbol/side/quantity/order_type/prices/SL-TP, the requested mode, the
deterministic decision (TRADE | NEEDS_APPROVAL), an idempotent lifecycle
status (CREATED | SENT_FOR_EXECUTION | EXECUTED | REJECTED | FAILED), and
persisted gate outcomes (feed freshness, risk, margin) so every rejection is
recorded honestly.  Exactly one intent may exist per ``agent_task_id``, and
``orders.agent_intent_id`` chains the resulting order back to the intent for
full observability (agent_task → intent → order → position → protection).

Persistence model:
  - one intent row per (winning) agent decision; intents never fabricate
    prices/fills — the fill comes from the broker adapter,
  - ``order_id`` / ``position_id`` / ``protective_order_id`` link the intent to
    the canonical orders/positions/protective_order rows,
  - status CAS transitions are the concurrency backstop (SENT_FOR_EXECUTION is
    claimed atomically from CREATED; a concurrent worker cannot double-dispatch).

Guards (parity with 0007/0008/0009): on databases migrated from a clean
baseline, 0001's ``Base.metadata.create_all`` already created these tables, so
this revision creates them only when absent, adds the ``orders`` column only
when missing, creates missing indexes, and seeds the registry row
idempotently.
"""
from datetime import datetime, timezone
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0010_agent_trading_intents"
down_revision: Union[str, Sequence[str], None] = "0009_agent_runtime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INTENTS = "trading_intents"
_AGENTS = "agents"
_ORDERS = "orders"

_IX_INTENTS_STATUS = "ix_trading_intents_status"
_IX_INTENTS_USER = "ix_trading_intents_user_id"
_IX_INTENTS_TASK = "ix_trading_intents_agent_task_id"
_IX_ORDERS_AGENT_INTENT = "ix_orders_agent_intent_id"

_AGENT_TYPE_SEED = "trading_agent"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
def upgrade() -> None:
    """Create the trading_intents table (guarded), add the orders link column,
    and seed the trading_agent registry row idempotently."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    # ---- trading_intents ----
    if _INTENTS not in existing:
        op.create_table(
            _INTENTS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "agent_task_id", sa.String(length=36),
                sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"),
                nullable=False,
            ),
            sa.Column(
                "user_id", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=False,
            ),
            sa.Column(
                "strategy_id", sa.String(length=36),
                sa.ForeignKey("strategies.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "broker_account_id", sa.String(length=36),
                sa.ForeignKey("broker_accounts.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("side", sa.String(length=10), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("order_type", sa.String(length=10), nullable=False, server_default="MARKET"),
            sa.Column("limit_price", sa.Float(), nullable=True),
            sa.Column("trigger_price", sa.Float(), nullable=True),
            sa.Column("stop_loss_price", sa.Float(), nullable=True),
            sa.Column("take_profit_price", sa.Float(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("decision", sa.String(length=20), nullable=False, server_default="TRADE"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="CREATED"),
            sa.Column("requested_mode", sa.String(length=10), nullable=False, server_default="PAPER"),
            sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "approved_by", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("market_data_status", sa.String(length=20), nullable=True),
            sa.Column("market_data_age_seconds", sa.Float(), nullable=True),
            sa.Column("risk_status", sa.String(length=20), nullable=True),
            sa.Column("risk_reason", sa.Text(), nullable=True),
            sa.Column("margin_required", sa.Float(), nullable=True),
            sa.Column("execution_reason", sa.Text(), nullable=True),
            sa.Column(
                "order_id", sa.String(length=36),
                sa.ForeignKey("orders.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "position_id", sa.String(length=36),
                sa.ForeignKey("positions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("protective_order_id", sa.String(length=36), nullable=True),
            sa.Column("error_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "side IN ('BUY', 'SELL')", name="ck_intent_side"
            ),
            sa.CheckConstraint(
                "decision IN ('TRADE', 'NEEDS_APPROVAL', 'NO_TRADE', 'REJECTED', 'FAILED')",
                name="ck_intent_decision",
            ),
            sa.CheckConstraint(
                "status IN ('CREATED', 'SENT_FOR_EXECUTION', 'EXECUTED', 'REJECTED', 'FAILED', 'CLOSED')",
                name="ck_intent_status",
            ),
            sa.CheckConstraint(
                "requested_mode IN ('PAPER', 'LIVE')", name="ck_intent_mode"
            ),
            sa.CheckConstraint(
                "order_type IN ('MARKET', 'LIMIT')", name="ck_intent_order_type"
            ),
        )
        op.create_index(_IX_INTENTS_STATUS, _INTENTS, ["status"])
        op.create_index(_IX_INTENTS_USER, _INTENTS, ["user_id"])
        op.create_index(
            _IX_INTENTS_TASK, _INTENTS, ["agent_task_id"], unique=True
        )
    else:
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_INTENTS)}
        for name, columns, unique in (
            (_IX_INTENTS_STATUS, ["status"], False),
            (_IX_INTENTS_USER, ["user_id"], False),
            (_IX_INTENTS_TASK, ["agent_task_id"], True),
        ):
            if name not in existing_indexes:
                op.create_index(name, _INTENTS, columns, unique=unique)

    # ---- orders.agent_intent_id (observability chain; no FK to avoid a
    # circular dependency with trading_intents.order_id) ----
    if _ORDERS in existing:
        order_columns = {c["name"] for c in inspector.get_columns(_ORDERS)}
        if "agent_intent_id" not in order_columns:
            op.add_column(
                _ORDERS,
                sa.Column("agent_intent_id", sa.String(length=36), nullable=True),
            )
        existing_order_indexes = {ix["name"] for ix in inspector.get_indexes(_ORDERS)}
        if _IX_ORDERS_AGENT_INTENT not in existing_order_indexes:
            op.create_index(_IX_ORDERS_AGENT_INTENT, _ORDERS, ["agent_intent_id"])

    # ---- Seed data (idempotent) ----
    _seed_agent(bind)
def _seed_agent(bind) -> None:
    """Insert the trading_agent row once (skipped if already present).

    Fail-closed defaults: enabled=False, autonomy ceiling 0, capabilities
    READ/ANALYZE/WRITE/EXECUTE (the DB row may only ever be TIGHTENED by an
    operator, never widened beyond the code declaration).
    """
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
            "name": "Trading Agent",
            "description": None,
            "capabilities_json": '["READ", "ANALYZE", "WRITE", "EXECUTE"]',
            "readonly": False,
            "max_autonomy_level": 0,
            "enabled": False,
            "created_at": now,
            "updated_at": now,
        },
    )


def downgrade() -> None:
    """Reverse the schema change (best-effort drop)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if _INTENTS in existing:
        index_names = {ix["name"] for ix in inspector.get_indexes(_INTENTS)}
        for index in (
            _IX_INTENTS_STATUS,
            _IX_INTENTS_USER,
            _IX_INTENTS_TASK,
        ):
            if index in index_names:
                op.drop_index(index, table_name=_INTENTS)
        op.drop_table(_INTENTS)

    if _ORDERS in existing:
        order_columns = {c["name"] for c in inspector.get_columns(_ORDERS)}
        if "agent_intent_id" in order_columns:
            order_indexes = {ix["name"] for ix in inspector.get_indexes(_ORDERS)}
            if _IX_ORDERS_AGENT_INTENT in order_indexes:
                op.drop_index(_IX_ORDERS_AGENT_INTENT, table_name=_ORDERS)
            op.drop_column(_ORDERS, "agent_intent_id")
