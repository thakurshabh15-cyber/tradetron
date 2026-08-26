"""Asynchronous Telegram trade alert delivery."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.session import SessionLocal
from app.models.alerts import UserNotificationSettings


async def send_telegram_alert(chat_id: str, message: str) -> bool:
    """Send one Markdown message to Telegram, returning False on delivery failure."""
    if not settings.telegram_bot_token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


async def dispatch_user_alert(
    user_id: str,
    message: str,
    setting: str = "order_fills_enabled",
) -> bool:
    """Deliver an alert when the user's channel and event preference allow it."""
    async with SessionLocal() as db:
        result = await db.execute(
            select(UserNotificationSettings).where(
                UserNotificationSettings.user_id == user_id
            )
        )
        preferences = result.scalar_one_or_none()

    if not preferences or not preferences.telegram_alerts_enabled:
        return False
    if not getattr(preferences, setting, False):
        return False
    return await send_telegram_alert(preferences.telegram_chat_id or "", message)


def format_trade_alert(
    *,
    event: str,
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    mode: str,
    pnl: float | None = None,
) -> str:
    """Build a compact Markdown message shared by API and engine fills."""
    lines = [
        f"*TradeThrone {event}*",
        f"*{side}* `{quantity}` x `{symbol}` @ `₹{price:.2f}`",
        f"Mode: `{mode}`",
    ]
    if pnl is not None:
        lines.append(f"PnL: `₹{pnl:+.2f}`")
    return "\n".join(lines)


async def notify_trade_fill(user_id: str | None, **trade: Any) -> None:
    if user_id:
        await dispatch_user_alert(
            user_id,
            format_trade_alert(event="Order Filled", **trade),
            "order_fills_enabled",
        )


async def notify_sl_tp(user_id: str | None, **trade: Any) -> None:
    if user_id:
        await dispatch_user_alert(
            user_id,
            format_trade_alert(event=trade.pop("event", "SL/TP Triggered"), **trade),
            "sl_tp_enabled",
        )