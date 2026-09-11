"""user_setup_tasks (tenant-scoped onboarding checklist)

Revision ID: 0006_user_setup_tasks
Revises: 0005_strategy_deployments_owner
Create Date: 2026-09-10

Final hardening P1 — tenant isolation + data integrity.  The onboard setup
status endpoints (``GET/PATCH /api/user/setup-status``) previously used a
process-global in-memory dict: unauthenticated, shared across every tenant,
and hardcoding "Complete" for Marketplace/Broker regardless of reality.
This migration introduces ``user_setup_tasks`` — a per-user, keyed
(user_id, task_id) table the API reads/writes instead.

NOTE: migration 0001_baseline delegates to ``Base.metadata.create_all``, so a
FRESH database already carries this table by the time this migration runs.
The guard mirrors 0003/0004/0005: if the table already exists it is left
untouched; only missing tables/indexes are created.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006_user_setup_tasks"
down_revision: Union[str, Sequence[str], None] = "0005_strategy_deployments_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "user_setup_tasks"
_UQ_NAME = "uq_user_setup_tasks_user_task"
_IX_NAME = "ix_user_setup_tasks_user_id"


def upgrade() -> None:
    """Create the per-user setup-task table (guarded)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing = set(inspector.get_table_names())
    if _TABLE in existing:
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="Pending"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "task_id", name=_UQ_NAME),
    )
    op.create_index(_IX_NAME, _TABLE, ["user_id"])


def downgrade() -> None:
    """Reverse the schema change (best-effort drop of the table)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in inspector.get_table_names():
        return

    index_names = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    if _IX_NAME in index_names:
        op.drop_index(_IX_NAME, table_name=_TABLE)

    op.drop_table(_TABLE)