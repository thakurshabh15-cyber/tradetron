"""tenant-less durable signal claims on orders

Revision ID: 0004_signal_durable_claim
Revises: 0003_orders_idempotency
Create Date: 2026-09-07

P0-2 — TradeThrone signal webhook durability.  The webhook path must persist a
durable order claim before broker dispatch, keyed by a TENANT-LESS signal key
(webhook signals carry no owning user, so the per-user
``ux_orders_user_client_order_id`` index cannot provide the dedup invariant).

Adds one nullable column to ``orders``:

* ``signal_key`` — the deterministic tenant-less idempotency key derived from
  ``provider:strategy_name:symbol:side:quantity:coarse-second-ts``.  Nullable
  so every legacy and user-scoped row is untouched.

Plus a GLOBAL PARTIAL UNIQUE INDEX on ``signal_key`` restricted to
``signal_key IS NOT NULL`` — the durable financial invariant that a signal can
be claimed exactly once across ALL tenants.  This is deliberately separate from
``ux_orders_user_client_order_id`` so a user's per-user DMA key can never
conflict with (or be conflicted by) the tenant-less signal key space.

NOTE: migration 0001_baseline delegates to ``Base.metadata.create_all``, so a
FRESH database already carries this column/index by the time this migration
runs.  All operations here are therefore conditional on what the inspector
actually finds (same pattern as 0003_orders_idempotency): existing columns/index
are left untouched, missing ones are added.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_signal_durable_claim"
down_revision: Union[str, Sequence[str], None] = "0003_orders_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IDX_NAME = "ux_orders_signal_key"


def upgrade() -> None:
    """Add the nullable signal_key column + the global partial-unique index."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    orders_columns = {c["name"] for c in inspector.get_columns("orders")}
    if "signal_key" not in orders_columns:
        op.add_column(
            "orders",
            sa.Column("signal_key", sa.String(length=64), nullable=True),
        )

    index_names = {ix["name"] for ix in inspector.get_indexes("orders")}
    if _IDX_NAME not in index_names:
        op.create_index(
            _IDX_NAME,
            "orders",
            ["signal_key"],
            unique=True,
            sqlite_where=sa.text("signal_key IS NOT NULL"),
            postgresql_where=sa.text("signal_key IS NOT NULL"),
        )


def downgrade() -> None:
    """Reverse the schema change (best-effort; SQLite needs batch mode)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    index_names = {ix["name"] for ix in inspector.get_indexes("orders")}
    if _IDX_NAME in index_names:
        op.drop_index(_IDX_NAME, table_name="orders")

    orders_columns = {c["name"] for c in inspector.get_columns("orders")}
    if "signal_key" in orders_columns:
        with op.batch_alter_table("orders") as batch_op:
            batch_op.drop_column("signal_key")