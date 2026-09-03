"""baseline schema — full TradeThrone ORM schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-04

This is the authoritative initial migration. It creates every table defined in
the TradeThrone ORM models by delegating to the application's declarative Base
metadata (``app.db.session.Base.metadata.create_all``). This guarantees the
schema always matches the current models.

Any pre-existing database previously created by the legacy
``app.db.session.init_db()`` automatic bootstrapper has no real Alembic history.
For those, STAMP the baseline (do not migrate) because the schema was built
imperatively:

    alembic stamp 0001_baseline

NOTE: The deprecated ``mobile_otps`` table (a legacy SMS-OTP table with no
corresponding ORM model, unused by the current codebase) is intentionally NOT
recreated here. Drop it on existing databases via a follow-up migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create every table declared by the current ORM metadata.

    We import Base (which, on import, already triggers all model modules via
    ``app.db.session.Base`` imports) and run create_all so the baseline is
    always in lock-step with the models.
    """
    from app.db.session import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind)


def downgrade() -> None:
    """Drop every table known to the ORM metadata (reverse of upgrade)."""
    from app.db.session import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind)
