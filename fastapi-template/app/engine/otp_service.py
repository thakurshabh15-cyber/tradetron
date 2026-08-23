"""Free mobile OTP generation and WhatsApp/Telegram dispatch service."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.models.mobile_otp import MobileOTPRecord

logger = get_logger("engine.otp")


def normalize_phone(phone_number: str) -> str:
    """Keep an international phone identifier stable for storage and lookup."""
    clean = "".join(character for character in phone_number.strip() if character.isdigit() or character == "+")
    if clean.startswith("00"):
        clean = "+" + clean[2:]
    if not clean.startswith("+"):
        clean = "+" + clean
    return clean


def generate_mobile_otp() -> str:
    return f"{secrets.randbelow(900000) + 100000:06d}"


async def _send_whatsapp(phone_number: str, otp_code: str) -> bool:
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return False
    url = f"https://graph.facebook.com/v20.0/{settings.whatsapp_phone_number_id}/messages"
    if settings.whatsapp_otp_template_name:
        payload = {
            "messaging_product": "whatsapp",
            "to": phone_number.lstrip("+"),
            "type": "template",
            "template": {"name": settings.whatsapp_otp_template_name, "language": {"code": "en_US"}, "components": [{"type": "body", "parameters": [{"type": "text", "text": otp_code}]}]},
        }
    else:
        payload = {"messaging_product": "whatsapp", "to": phone_number.lstrip("+"), "type": "text", "text": {"body": f"Your Tradetron OTP is {otp_code}. It expires in 5 minutes."}}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"})
            response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("WhatsApp OTP dispatch failed: %s", exc)
        return False


async def _send_telegram_fallback(otp_code: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json={"chat_id": settings.telegram_chat_id, "text": f"Tradetron mobile OTP: {otp_code} (expires in 5 minutes)"})
            response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("Telegram OTP fallback failed: %s", exc)
        return False


async def create_and_dispatch_otp(db: AsyncSession, phone_number: str) -> MobileOTPRecord:
    phone = normalize_phone(phone_number)
    record = MobileOTPRecord(phone_number=phone, otp_code=generate_mobile_otp(), expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    db.add(record)
    await db.commit()
    await db.refresh(record)

    delivered = await _send_whatsapp(phone, record.otp_code)
    if not delivered:
        delivered = await _send_telegram_fallback(record.otp_code)
    if settings.environment.lower() in {"development", "dev", "test"}:
        logger.info("Mobile OTP for %s: %s (delivered=%s)", phone, record.otp_code, delivered)
    elif not delivered:
        logger.warning("No mobile OTP provider delivered a code for %s", phone)
    return record


async def verify_mobile_otp(db: AsyncSession, phone_number: str, otp_code: str) -> bool:
    phone = normalize_phone(phone_number)
    result = await db.execute(select(MobileOTPRecord).where(MobileOTPRecord.phone_number == phone, MobileOTPRecord.is_verified.is_(False)).order_by(MobileOTPRecord.created_at.desc()))
    record = result.scalars().first()
    expires_at = record.expires_at if record and record.expires_at.tzinfo else record.expires_at.replace(tzinfo=timezone.utc) if record else None
    if not record or expires_at < datetime.now(timezone.utc) or not secrets.compare_digest(record.otp_code, otp_code.strip()):
        return False
    record.is_verified = True
    await db.commit()
    return True