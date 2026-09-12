"""protective_orders (Phase 15C: exchange-level SL/TP protective orders)

Revision ID: 0008_protective_orders
Revises: 0007_broker_state
Create Date: 2026-09-12

Phase 15C introduces the per-position, per-leg durable ledger for broker-side
protective orders (``protective_orders``) plus the honest protection-lifecycle
columns on ``positions`` (``protection_state``, ``protection_error``,
``protected_at``).

Guards (parity with 0007_broker_state): if the table/columns already exist
(from a local ``create_all`` bootstrap) they are left untouched; only missing
objects are created.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_protective_orders"
down_revision: Union[str, Sequence[str], None] = "0007_broker_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PROTECTIVE_TABLE = "protective_orders"
_UQ_POSITION_LEG = "ux_protective_orders_position_leg"
_IX_ACC_STATUS = "ix_protective_orders_acc_status"
_IX_BROKER_REF = "ix_protective_orders_broker_ref"
_IX_USER = "ix_protective_orders_user_id"
_IX_ACC = "ix_protective_orders_broker_account_id"
_IX_POSITION = "ix_protective_orders_position_id"

_POSITION_COLUMNS = (
    ("protection_state", sa.String(length=20), "UNPROTECTED"),
    ("protection_error", sa.Text(), None),
    ("protected_at", sa.DateTime(timezone=True), None),
)


def upgrade() -> None:
    """Create the protective_orders table and position protection columns."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing = set(inspector.get_table_names())
    if _PROTECTIVE_TABLE in existing:
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_PROTECTIVE_TABLE)}
        if _UQ_POSITION_LEG not in existing_indexes:
            op.create_index(_UQ_POSITION_LEG, _PROTECTIVE_TABLE, ["position_id", "leg"], unique=True)
        if _IX_ACC_STATUS not in existing_indexes:
            op.create_index(_IX_ACC_STATUS, _PROTECTIVE_TABLE, ["broker_account_id", "status"])
        if _IX_BROKER_REF not in existing_indexes:
            op.create_index(_IX_BROKER_REF, _PROTECTIVE_TABLE, ["broker_protective_order_id"])
    else:
        op.create_table(
            _PROTECTIVE_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("broker_account_id", sa.String(length=36), sa.ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True),
            sa.Column("position_id", sa.String(length=36), sa.ForeignKey("positions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("mode", sa.String(length=20), nullable=False, server_default="LIVE"),
            sa.Column("leg", sa.String(length=20), nullable=False),
            sa.Column("side", sa.String(length=10), nullable=False),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("trigger_price", sa.Float(), nullable=False),
            sa.Column("limit_price", sa.Float(), nullable=True),
            sa.Column("order_type", sa.String(length=20), nullable=False),
            sa.Column("broker_protective_order_id", sa.String(length=100), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING_PLACEMENT"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("broker_reported_status", sa.String(length=20), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("position_id", "leg", name=_UQ_POSITION_LEG),
        )
        op.create_index(_IX_ACC_STATUS, _PROTECTIVE_TABLE, ["broker_account_id", "status"])
        op.create_index(_IX_BROKER_REF, _PROTECTIVE_TABLE, ["broker_protective_order_id"])
        op.create_index(_IX_USER, _PROTECTIVE_TABLE, ["user_id"])
        op.create_index(_IX_ACC, _PROTECTIVE_TABLE, ["broker_account_id"])
        op.create_index(_IX_POSITION, _PROTECTIVE_TABLE, ["position_id"])

    # Guards: add the position protection columns only when missing.  Baselines
    # stamped past 0001 (the ORM-bootstrap revision) may lack the "positions"
    # table entirely (e.g. a minimal legacy DB) — skip the ALTERs in that case
    # so the upgrade chain stays applyable on every baseline.
    if "positions" in existing:
        positions_columns = {col["name"] for col in inspector.get_columns("positions")}
        for col_name, col_type, server_default in _POSITION_COLUMNS:
            if col_name in positions_columns:
                continue
            kwargs: dict = {}
            if server_default is not None:
                kwargs["server_default"] = server_default
            op.add_column("positions", sa.Column(col_name, col_type, nullable=True, **kwargs))


def downgrade() -> None:
    """Reverse the schema change (best-effort)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    table_names = set(inspector.get_table_names())
    if _PROTECTIVE_TABLE in table_names:
        index_names = {ix["name"] for ix in inspector.get_indexes(_PROTECTIVE_TABLE)}
        for index in (
            _IX_ACC_STATUS,
            _IX_BROKER_REF,
            _IX_USER,
            _IX_ACC,
            _IX_POSITION,
            _UQ_POSITION_LEG,
        ):
            if index in index_names:
                op.drop_index(index, table_name=_PROTECTIVE_TABLE)
        op.drop_table(_PROTECTIVE_TABLE)

    if "positions" in table_names:
        positions_columns = {col["name"] for col in inspector.get_columns("positions")}
        for col_name, _col_type, _server_default in _POSITION_COLUMNS:
            if col_name in positions_columns:
                op.drop_column("positions", col_name)