"""orders idempotency columns + per-user partial-unique client_order_id

Revision ID: 0003_orders_idempotency
Revises: 0002_drop_mobile_otps
Create Date: 2026-09-06

P0 — DMA/manual order idempotency. Adds two nullable columns to ``orders``:

* ``client_order_id`` — the caller-supplied idempotency key (nullable so
  every legacy unkeyed row is untouched).
* ``position_id`` — the position row created for the order, persisted so an
  idempotent replay can return the exact previous result.

Plus a PARTIAL UNIQUE INDEX on ``(user_id, client_order_id)`` restricted to
``client_order_id IS NOT NULL`` — the durable financial invariant that an
idempotency key can be claimed exactly once per authenticated user.  Legacy
rows (NULL key) and NULL-user rows are never candidates, so the index cannot
reject valid legacy data.

NOTE: migration 0001_baseline delegates to ``Base.metadata.create_all``, so a
FRESH database already carries these columns/index by the time this migration
runs.  All operations here are therefore conditional on what the inspector
actually finds (same pattern as 0002_drop_mobile_otps): existing columns/index
are left untouched, missing ones are added.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003_orders_idempotency"
down_revision: Union[str, Sequence[str], None] = "0002_drop_mobile_otps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IDX_NAME = "ux_orders_user_client_order_id"


def upgrade() -> None:
    """Add the two nullable order columns + the per-user partial-unique index.

    Existing databases (bootstrapped before this migration) get the columns
    via ALTER TABLE; databases created from 0001's ``create_all`` already have
    them, so every step is guarded by an inspector look-up.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    orders_columns = {c["name"] for c in inspector.get_columns("orders")}
    if "client_order_id" not in orders_columns:
        op.add_column(
            "orders",
            sa.Column("client_order_id", sa.String(length=64), nullable=True),
        )
    if "position_id" not in orders_columns:
        op.add_column(
            "orders",
            sa.Column("position_id", sa.String(length=36), nullable=True),
        )

    index_names = {ix["name"] for ix in inspector.get_indexes("orders")}
    if _IDX_NAME not in index_names:
        op.create_index(
            _IDX_NAME,
            "orders",
            ["user_id", "client_order_id"],
            unique=True,
            sqlite_where=sa.text("client_order_id IS NOT NULL"),
            postgresql_where=sa.text("client_order_id IS NOT NULL"),
        )


def downgrade() -> None:
    """Reverse the schema change (best-effort; SQLite needs batch mode)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    index_names = {ix["name"] for ix in inspector.get_indexes("orders")}
    if _IDX_NAME in index_names:
        op.drop_index(_IDX_NAME, table_name="orders")

    orders_columns = {c["name"] for c in inspector.get_columns("orders")}
    drop_targets = [c for c in ("client_order_id", "position_id") if c in orders_columns]
    if drop_targets:
        with op.batch_alter_table("orders") as batch_op:
            for col in drop_targets:
                batch_op.drop_column(col)