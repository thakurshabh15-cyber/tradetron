"""SQLAlchemy ORM models for Copy Trading, Master groups, and Follower accounts."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _new_invite_code() -> str:
    """Generate an alphanumeric 8-character unique invite code (e.g. TRAD-7X9K)."""
    return f"CPY-{secrets.token_hex(3).upper()}"


class CopyGroupRecord(Base):
    """A master trading group that followers can subscribe to for trade mirroring."""

    __tablename__ = "copy_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    master_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    profit_share_pct: Mapped[float] = mapped_column(Float, default=20.0)  # e.g. 20.0 = 20%
    invite_code: Mapped[str] = mapped_column(
        String(20), unique=True, default=_new_invite_code, index=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    min_capital: Mapped[float] = mapped_column(Float, default=10000.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_copy_groups_master", "master_user_id"),
        Index("ix_copy_groups_invite", "invite_code"),
    )


class CopyFollowerRecord(Base):
    """Subscription record binding a follower user and broker account to a master copy group."""

    __tablename__ = "copy_followers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("copy_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    follower_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    broker_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("broker_accounts.id", ondelete="SET NULL"), nullable=True
    )
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)  # 0.5x, 1.0x, 2.0x
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE, PAUSED, STOPPED
    max_allocation: Mapped[float] = mapped_column(Float, default=50000.0)  # Max risk capital per order/pos
    mode: Mapped[str] = mapped_column(String(20), default="PAPER")  # PAPER | LIVE
    total_copied_trades: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_copy_followers_group", "group_id"),
        Index("ix_copy_followers_user", "follower_user_id"),
        Index("ix_copy_followers_status", "status"),
        Index("ix_copy_followers_unique_sub", "group_id", "follower_user_id", unique=True),
    )
