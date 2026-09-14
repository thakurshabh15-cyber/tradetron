"""subscription cancel fields

Revision ID: 0012_subscription_cancel_fields
Revises: 0011_agent_control
Create Date: 2026-09-14

Adds the two nullable ``subscriptions`` columns consumed by the Razorpay
billing webhook (``subscription.cancelled`` — see
``app/webhooks/handlers/billing.py``):

* ``cancelled_at`` — when the subscription was cancelled.
* ``cancellation_reason`` — the provider event type that cancelled it.

These columns also exist on the ORM ``SubscriptionRecord`` model.  As with the
other guarded migrations: a FRESH database already carries them because
0001_baseline delegates to ``Base.metadata.create_all``; an EXISTING database
at an earlier head gets them via ALTER TABLE here.  Every step is therefore
guarded by an inspector look-up so the migration is idempotent on both paths.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0012_subscription_cancel_fields"
down_revision: Union[str, Sequence[str], None] = "0011_agent_control"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the two nullable subscription columns when they are missing."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "subscriptions" not in set(inspector.get_table_names()):
        return

    columns = {c["name"] for c in inspector.get_columns("subscriptions")}
    if "cancelled_at" not in columns:
        op.add_column(
            "subscriptions",
            sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "cancellation_reason" not in columns:
        op.add_column(
            "subscriptions",
            sa.Column("cancellation_reason", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    """Reverse the schema change (best-effort; SQLite needs batch mode)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "subscriptions" not in set(inspector.get_table_names()):
        return

    columns = {c["name"] for c in inspector.get_columns("subscriptions")}
    drop_targets = [c for c in ("cancelled_at", "cancellation_reason") if c in columns]
    if drop_targets:
        with op.batch_alter_table("subscriptions") as batch_op:
            for col in drop_targets:
                batch_op.drop_column(col)