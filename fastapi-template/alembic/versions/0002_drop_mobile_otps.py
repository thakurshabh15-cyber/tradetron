"""drop legacy mobile_otps table

Revision ID: 0002_drop_mobile_otps
Revises: 0001_baseline
Create Date: 2026-09-04

The ``mobile_otps`` table was created by an early (removed) SMS-OTP
implementation. It has no corresponding ORM model and is unused by the
current codebase. This migration removes it from any database that still
carries it (e.g. databases bootstrapped by the legacy ``init_db()``
automatic table-creation path instead of through Alembic).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_drop_mobile_otps"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the orphaned mobile_otps table if it exists."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mobile_otps" in inspector.get_table_names():
        op.drop_table("mobile_otps")


def downgrade() -> None:
    """Recreate mobile_otps (best-effort; only needed for a rollback)."""
    op.create_table(
        "mobile_otps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("phone_number", sa.String(length=30), nullable=False),
        sa.Column("otp_code", sa.String(length=6), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
