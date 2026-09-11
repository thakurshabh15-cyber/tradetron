"""User and Authentication ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class UserRecord(Base):
    """User account entity for auth and profiles."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_id
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    phone: Mapped[str | None] = mapped_column(
        String(30), unique=True, nullable=True, index=True
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(100), default="Trader")
    role: Mapped[str] = mapped_column(String(20), default="trader")  # admin, trader, creator
    paper_balance: Mapped[float] = mapped_column(Float, default=1000000.0)
    kyc_status: Mapped[str] = mapped_column(String(20), default="NOT_SUBMITTED")  # NOT_SUBMITTED, PENDING, VERIFIED, REJECTED
    pan_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    id_proof_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    id_proof_doc: Mapped[str | None] = mapped_column(Text, nullable=True)
    kyc_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kyc_rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    profile_photo: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 2FA Authenticator & SMS OTP
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Account Lockout & Security
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class RevokedTokenRecord(Base):
    """Server-side revoked refresh tokens for secure logout and token rotation."""

    __tablename__ = "revoked_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserSetupTaskRecord(Base):
    """Per-user onboarding checklist state (Marketplace / Broker / Subscription).

    Tenant-scoped replacement for the previous process-global ``_SETUP_STATE``
    dict: every row carries the owning ``user_id`` so one trader's checklist
    can never leak into, or be mutated by, another tenant. Status transitions
    are user-initiated overrides persisted in SQL; effective status is derived
    by the API from real per-user data (connected broker accounts, deployed
    strategies, active subscription) and the optional override row.
    """

    __tablename__ = "user_setup_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str] = mapped_column(
        String(40), nullable=False
    )  # marketplace_setup | broker_setup | subscription_setup
    status: Mapped[str] = mapped_column(String(10), default="Pending")  # Complete | Pending
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "task_id", name="uq_user_setup_tasks_user_task"),
    )

