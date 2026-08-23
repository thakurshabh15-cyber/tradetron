"""Resend.com email delivery for authentication OTPs."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.core.logging import get_logger

logger = get_logger("engine.email")


def _otp_email(otp_code: str) -> tuple[str, str]:
    subject = "Your Tradetron Login Verification Code"
    text = (
        "Hello,\n\n"
        f"Your Tradetron verification code is: {otp_code}\n\n"
        "This code will expire in 15 minutes.\n"
        "If you did not request this code, please ignore this email.\n\n"
        "Tradetron Security Team"
    )
    return subject, text


async def send_otp_email(to_email: str, otp_code: str) -> dict[str, Any]:
    """Send a login OTP through Resend.com and return delivery metadata."""
    if not settings.resend_api_key:
        return {"dispatched": False, "provider": "unconfigured", "message": "RESEND_API_KEY is not configured."}

    subject, text = _otp_email(otp_code)
    payload = {
        "from": settings.emails_from_email or settings.resend_from_email,
        "to": [to_email],
        "subject": subject,
        "text": text,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
            response.raise_for_status()
        resend_id = response.json().get("id")
        logger.info("OTP email sent to %s through Resend (id=%s)", to_email, resend_id)
        return {"dispatched": True, "provider": "resend", "resend_id": resend_id}
    except httpx.HTTPError as exc:
        logger.error("Resend OTP delivery failed for %s: %s", to_email, exc)
        return {"dispatched": False, "provider": "resend_failed", "message": str(exc)}