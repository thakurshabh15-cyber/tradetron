"""Notification preferences ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class UserNotificationSettings(Base):
    """Notification preferences for channels and event triggers."""

    __tablename__ = "notification_preferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    email_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    order_executed_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    trade_closed_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    sl_tp_trigger_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    price_alert_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    margin_calls_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    telegram_alerts_enabled = synonym("telegram_enabled")
    order_fills_enabled = synonym("order_executed_notify")
    sl_tp_enabled = synonym("sl_tp_trigger_notify")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


NotificationPreferenceRecord = UserNotificationSettings
