"""strategy_deployments owner column (tenant scoping)

Revision ID: 0005_strategy_deployments_owner
Revises: 0004_signal_durable_claim
Create Date: 2026-09-09

Phase 17 P1 fix — cross-tenant operational.  ``strategy_deployments`` had NO
owner column, so admin / copy surfaces (``/strategies/oversight``, per-user
halts) could not be tenant-scoped.  Adds one nullable column:

* ``owner_user_id`` — the user (derived server-side from the bearer token in
  ``POST /api/strategies/{id}/deploy``) who deployed the strategy.  Nullable
  so every pre-Phase-17 legacy row is untouched and the Postgres ALTER is a
  non-blocking online add (DBRE rule: no long table lock).

Plus a plain index on ``owner_user_id`` for the per-user lookups issued by
oversight / per-user halts.

From this point forward, a deployment carries a durable owner identity, so a
per-user halt / oversight query can be scoped with
``StrategyDeploymentRecord.owner_user_id == <user_id>``.

NOTE: migration 0001_baseline delegates to ``Base.metadata.create_all``, so a
FRESH database already carries this column/index by the time this migration
runs.  All operations here are therefore conditional on what the inspector
actually finds (same guard pattern as 0003/0004): existing columns/index are
left untouched, missing ones are added.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_strategy_deployments_owner"
down_revision: Union[str, Sequence[str], None] = "0004_signal_durable_claim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IDX_NAME = "ix_strategy_deployments_owner_user_id"


def upgrade() -> None:
    """Add the nullable owner_user_id column + index."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Guard for table existence: the startup-migration tests exercise the
    # upgrade chain against a minimal DB that only contains ``orders``.
    # A missing ``strategy_deployments`` table must be a graceful no-op
    # (the table will be created with the column on a fresh DB), never a
    # NoSuchTableError that aborts the whole migration chain.
    if "strategy_deployments" not in inspector.get_table_names():
        return

    columns = {c["name"] for c in inspector.get_columns("strategy_deployments")}
    if "owner_user_id" not in columns:
        op.add_column(
            "strategy_deployments",
            sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        )

    index_names = {ix["name"] for ix in inspector.get_indexes("strategy_deployments")}
    if _IDX_NAME not in index_names:
        op.create_index(
            _IDX_NAME,
            "strategy_deployments",
            ["owner_user_id"],
        )


def downgrade() -> None:
    """Reverse the schema change (best-effort; SQLite needs batch mode)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "strategy_deployments" not in inspector.get_table_names():
        return

    index_names = {ix["name"] for ix in inspector.get_indexes("strategy_deployments")}
    if _IDX_NAME in index_names:
        op.drop_index(_IDX_NAME, table_name="strategy_deployments")

    columns = {c["name"] for c in inspector.get_columns("strategy_deployments")}
    if "owner_user_id" in columns:
        with op.batch_alter_table("strategy_deployments") as batch_op:
            batch_op.drop_column("owner_user_id")
