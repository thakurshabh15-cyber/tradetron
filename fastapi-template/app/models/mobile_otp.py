"""Database-backed mobile OTP records."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MobileOTPRecord(Base):
    __tablename__ = "mobile_otps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    phone_number: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    otp_code: Mapped[str] = mapped_column(String(6), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_mobile_otps_phone_active", "phone_number", "is_verified", "expires_at"),)