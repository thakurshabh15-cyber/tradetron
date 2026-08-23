"""Broker Account ORM models with encrypted credentials and token lifecycle management."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.core.crypto import encrypt_secret, decrypt_secret, mask_secret


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class BrokerAccountRecord(Base):
    """Linked live or simulated broker account with AES-256 encrypted secrets at rest."""

    __tablename__ = "broker_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    broker_name: Mapped[str] = mapped_column(String(50), nullable=False)  # ZERODHA, UPSTOX, ANGEL_ONE, BINANCE, SIMULATED
    account_name: Mapped[str] = mapped_column(String(100), default="Primary Trading Account")
    client_id: Mapped[str | None] = mapped_column(String(100), default="CLIENT_01", nullable=True)

    # Encrypted fields (never stored in plaintext)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Token Expiry Lifecycle (Daily token refresh tracking)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="CONNECTED")  # CONNECTED, DISCONNECTED, EXPIRED, ERROR
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_broker_accounts_user_status", "user_id", "status"),
    )

    def set_api_key(self, raw_key: str) -> None:
        self.api_key_encrypted = encrypt_secret(raw_key)

    def get_api_key(self) -> str:
        return decrypt_secret(self.api_key_encrypted)

    def set_api_secret(self, raw_secret: str) -> None:
        self.api_secret_encrypted = encrypt_secret(raw_secret) if raw_secret else None

    def get_api_secret(self) -> str:
        return decrypt_secret(self.api_secret_encrypted) if self.api_secret_encrypted else ""

    def set_access_token(self, raw_token: str) -> None:
        self.access_token_encrypted = encrypt_secret(raw_token) if raw_token else None

    def get_access_token(self) -> str:
        return decrypt_secret(self.access_token_encrypted) if self.access_token_encrypted else ""

    def set_totp_secret(self, raw_totp: str) -> None:
        self.totp_secret_encrypted = encrypt_secret(raw_totp) if raw_totp else None

    def get_totp_secret(self) -> str:
        return decrypt_secret(self.totp_secret_encrypted) if self.totp_secret_encrypted else ""

    def set_refresh_token(self, raw_refresh: str) -> None:
        self.refresh_token_encrypted = encrypt_secret(raw_refresh) if raw_refresh else None

    def get_refresh_token(self) -> str:
        return decrypt_secret(self.refresh_token_encrypted) if self.refresh_token_encrypted else ""

    def set_credentials(
        self,
        api_key: str,
        api_secret: str = "",
        access_token: str = "",
        totp_secret: str = "",
        refresh_token: str = "",
    ) -> None:
        self.set_api_key(api_key)
        if api_secret:
            self.set_api_secret(api_secret)
        if access_token:
            self.set_access_token(access_token)
        if totp_secret:
            self.set_totp_secret(totp_secret)
        if refresh_token:
            self.set_refresh_token(refresh_token)

    @property
    def api_key_masked(self) -> str:
        raw = self.get_api_key()
        return mask_secret(raw)

    def is_token_expired(self) -> bool:
        """Check if broker token has reached daily expiry threshold."""
        if not self.access_token_encrypted:
            return True
        if self.token_expires_at:
            expiry = self.token_expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= expiry
        return False


class BrokerSessionLogRecord(Base):
    """Audit log for automated and manual broker session renewals."""

    __tablename__ = "broker_session_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    broker_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("broker_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    broker_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)  # SUCCESS, FAILED, TOTP_INVALID, EXPIRED
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_broker_session_logs_acc_status", "broker_account_id", "status"),
        Index("ix_broker_session_logs_time", "renewed_at"),
    )

