"""Persisted no-code option strategy definitions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VisualStrategyRecord(Base):
    """A visual option strategy with JSON condition and multi-leg definitions."""

    __tablename__ = "visual_strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    underlying: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    entry_conditions: Mapped[str] = mapped_column(Text, default="[]")
    exit_conditions: Mapped[str] = mapped_column(Text, default="{}")
    legs: Mapped[str] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(20), default="PAPER")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (Index("ix_visual_strategies_user_active", "user_id", "is_active"),)