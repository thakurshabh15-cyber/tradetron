"""broker_state (Phase 15B: persisted broker-truth snapshot)

Revision ID: 0007_broker_state
Revises: 0006_user_setup_tasks
Create Date: 2026-09-11

Phase 15B introduces ``broker_state`` — exactly one freshness-tracked,
account-scoped, normalized snapshot row per linked broker account.  The table
is used by the broker-state sync engine and the LIVE risk gate.

Guard: if the table already exists (from a local ``create_all`` bootstrap)
it is left untouched; only missing tables/indexes/constraints are created.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0007_broker_state"
down_revision: Union[str, Sequence[str], None] = "0006_user_setup_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "broker_state"
_UQ_NAME = "uq_broker_state_broker_account"
_IX_ACCOUNT = "ix_broker_state_broker_account_id"
_IX_USER_STATUS = "ix_broker_state_user_status"


def upgrade() -> None:
    """Create the broker_state table (guarded)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing = set(inspector.get_table_names())
    if _TABLE in existing:
        # Table exists (local create_all); ensure indexes are present.
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
        if _IX_ACCOUNT not in existing_indexes:
            op.create_index(_IX_ACCOUNT, _TABLE, ["broker_account_id"])
        if _IX_USER_STATUS not in existing_indexes:
            op.create_index(_IX_USER_STATUS, _TABLE, ["user_id", "status"])
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "broker_account_id",
            sa.String(length=36),
            sa.ForeignKey("broker_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="UNAVAILABLE"),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="BROKER"),
        sa.Column("positions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("positions_hash", sa.String(length=64), nullable=True),
        sa.Column("available_cash", sa.Float(), nullable=True),
        sa.Column("utilized_margin", sa.Float(), nullable=True),
        sa.Column("total_collateral", sa.Float(), nullable=True),
        sa.Column("unrealized_pnl", sa.Float(), nullable=True),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.Column("total_equity", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=12), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_good_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_message", sa.Text(), nullable=True),
        sa.Column("sync_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("broker_account_id", name=_UQ_NAME),
    )
    op.create_index(_IX_ACCOUNT, _TABLE, ["broker_account_id"])
    op.create_index(_IX_USER_STATUS, _TABLE, ["user_id", "status"])


def downgrade() -> None:
    """Reverse the schema change (best-effort drop of the table)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in inspector.get_table_names():
        return

    index_names = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    if _IX_USER_STATUS in index_names:
        op.drop_index(_IX_USER_STATUS, table_name=_TABLE)
    if _IX_ACCOUNT in index_names:
        op.drop_index(_IX_ACCOUNT, table_name=_TABLE)
    op.drop_table(_TABLE)