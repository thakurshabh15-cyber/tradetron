"""Telegram alert preferences and test delivery endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.db.session import get_db
from app.engine.alerts import send_telegram_alert
from app.models.alerts import UserNotificationSettings
from app.models.user import UserRecord

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertSettingsPatch(BaseModel):
    chat_id: str | None = Field(None, max_length=100)
    telegram_chat_id: str | None = Field(None, max_length=100)
    telegram_alerts_enabled: bool | None = None
    order_fills_enabled: bool | None = None
    sl_tp_enabled: bool | None = None
    margin_calls_enabled: bool | None = None


def _settings_payload(settings: UserNotificationSettings) -> dict[str, Any]:
    return {
        "user_id": settings.user_id,
        "telegram_chat_id": settings.telegram_chat_id,
        "chat_id": settings.telegram_chat_id,
        "telegram_alerts_enabled": settings.telegram_alerts_enabled,
        "order_fills_enabled": settings.order_fills_enabled,
        "sl_tp_enabled": settings.sl_tp_enabled,
        "margin_calls_enabled": settings.margin_calls_enabled,
    }


async def _get_or_create_settings(user: UserRecord, db: AsyncSession) -> UserNotificationSettings:
    result = await db.execute(
        select(UserNotificationSettings).where(UserNotificationSettings.user_id == user.id)
    )
    settings = result.scalar_one_or_none()
    if not settings:
        settings = UserNotificationSettings(user_id=user.id, telegram_chat_id="")
        db.add(settings)
        await db.flush()
    return settings


@router.get("/settings")
async def get_alert_settings(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings = await _get_or_create_settings(user, db)
    await db.commit()
    return _settings_payload(settings)


@router.patch("/settings")
async def update_alert_settings(
    patch: AlertSettingsPatch,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings = await _get_or_create_settings(user, db)
    values = patch.model_dump(exclude_unset=True)
    chat_id = values.pop("chat_id", None)
    if chat_id is not None:
        values["telegram_chat_id"] = chat_id
    for key, value in values.items():
        setattr(settings, key, value)
    await db.commit()
    await db.refresh(settings)
    return _settings_payload(settings)


@router.post("/telegram/test")
async def send_test_alert(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings = await _get_or_create_settings(user, db)
    await db.commit()
    if not settings.telegram_chat_id:
        raise HTTPException(status_code=400, detail="Configure a Telegram chat ID first")
    delivered = await send_telegram_alert(
        settings.telegram_chat_id,
        "*Tradetron Test Alert*\nTelegram trade notifications are connected.",
    )
    if not delivered:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram alert could not be delivered. Check the bot token and chat ID.",
        )
    return {"success": True, "message": "Test alert sent"}