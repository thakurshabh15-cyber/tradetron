"""Production Notification & OTP Dispatch Service.

Dispatches real OTP verification codes and transactional security notices via:
- SMTP (SendGrid, AWS SES, Mailgun, Postmark, custom TLS/SSL SMTP)
- SMS (Twilio, MSG91)
- Fallback to high-visibility operational logging when credentials are not configured.
"""

from __future__ import annotations

import asyncio
import email.message
import json
import smtplib
from typing import Any, Optional
from urllib.parse import urlencode

from app.config import settings
from app.core.logging import get_logger

logger = get_logger("notifications.dispatcher")


async def dispatch_email(
    to_email: str,
    subject: str,
    text_content: str,
    html_content: Optional[str] = None,
) -> bool:
    """Send a transactional email using configured SMTP provider."""
    if not settings.smtp_host or not settings.smtp_user:
        logger.warning(
            "[EMAIL DISPATCH SKIPPED] SMTP_HOST or SMTP_USER not configured. "
            "To send real emails, configure SMTP_HOST/SMTP_USER/SMTP_PASSWORD in .env. "
            "Recipient: %s | Subject: %s",
            to_email,
            subject,
        )
        return False

    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_email
    msg["To"] = to_email
    msg.set_content(text_content)

    if html_content:
        msg.add_alternative(html_content, subtype="html")

    def _sync_send():
        try:
            if settings.smtp_tls:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                    server.starttls()
                    if settings.smtp_password:
                        server.login(settings.smtp_user, settings.smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                    if settings.smtp_password:
                        server.login(settings.smtp_user, settings.smtp_password)
                    server.send_message(msg)
            logger.info("Email dispatched successfully to %s: %s", to_email, subject)
            return True
        except Exception as exc:
            logger.error("Failed to send email to %s via %s: %s", to_email, settings.smtp_host, exc)
            return False

    return await asyncio.to_thread(_sync_send)


async def dispatch_sms_twilio(to_phone: str, message: str) -> bool:
    """Send SMS via Twilio API."""
    if not settings.twilio_account_sid or not settings.twilio_auth_token or not settings.twilio_from_number:
        return False

    import aiohttp
    import base64

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    auth_str = f"{settings.twilio_account_sid}:{settings.twilio_auth_token}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "From": settings.twilio_from_number,
        "To": to_phone,
        "Body": message,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 201):
                    logger.info("Twilio SMS sent to %s", to_phone)
                    return True
                else:
                    body = await resp.text()
                    logger.error("Twilio SMS error (%d): %s", resp.status, body)
                    return False
    except Exception as exc:
        logger.error("Twilio SMS request failed: %s", exc)
        return False


async def dispatch_sms_msg91(to_phone: str, otp_code: str) -> bool:
    """Send OTP SMS via MSG91 API (India)."""
    if not settings.msg91_auth_key:
        return False

    import aiohttp

    clean_phone = to_phone.replace("+", "").replace(" ", "").replace("-", "")
    url = f"https://api.msg91.com/api/v5/otp?template_id={settings.msg91_template_id or 'default'}&mobile={clean_phone}&authkey={settings.msg91_auth_key}&otp={otp_code}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    logger.info("MSG91 OTP SMS sent to %s", to_phone)
                    return True
                else:
                    body = await resp.text()
                    logger.error("MSG91 error (%d): %s", resp.status, body)
                    return False
    except Exception as exc:
        logger.error("MSG91 request failed: %s", exc)
        return False


async def dispatch_otp(
    identifier: str,
    otp_code: str,
    purpose: str = "registration",
) -> dict[str, Any]:
    """Universal OTP dispatcher routing to Email or SMS providers based on identifier type."""
    is_email = "@" in identifier
    dispatched = False
    provider_used = "none"

    if is_email:
        subject = {
            "registration": "Verify Your Tradetron Account",
            "login": "Your Tradetron Login Verification Code",
            "password_reset": "Reset Your Tradetron Password",
        }.get(purpose, "Your Tradetron Verification Code")

        text_body = (
            f"Hello,\n\n"
            f"Your Tradetron verification code is: {otp_code}\n\n"
            f"This code will expire in 10 minutes.\n"
            f"If you did not request this code, please ignore this message.\n\n"
            f"— Tradetron Security Team"
        )

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: Arial, sans-serif; background-color: #0f172a; color: #f8fafc; padding: 24px;">
          <div style="max-width: 480px; margin: 0 auto; background: #1e293b; border-radius: 12px; padding: 32px; border: 1px solid #334155;">
            <h2 style="color: #38bdf8; margin-top: 0;">Tradetron Security Verification</h2>
            <p style="color: #94a3b8; font-size: 14px;">Use the verification code below to complete your {purpose.replace('_', ' ')}:</p>
            <div style="background: #0f172a; border-radius: 8px; padding: 18px; text-align: center; margin: 24px 0; border: 1px dashed #0284c7;">
              <span style="font-size: 32px; font-weight: bold; letter-spacing: 6px; color: #38bdf8; font-family: monospace;">{otp_code}</span>
            </div>
            <p style="color: #64748b; font-size: 12px;">This code is valid for 10 minutes. Never share this code with anyone.</p>
            <hr style="border: 0; border-top: 1px solid #334155; margin: 24px 0;" />
            <p style="color: #475569; font-size: 11px; margin: 0;">Tradetron Algorithmic Trading Platform • Secure Operations</p>
          </div>
        </body>
        </html>
        """

        dispatched = await dispatch_email(
            to_email=identifier,
            subject=subject,
            text_content=text_body,
            html_content=html_body,
        )
        provider_used = "smtp" if dispatched else "smtp_unconfigured"

    else:
        # SMS route
        sms_body = f"Your Tradetron verification code is {otp_code}. Valid for 10 minutes. Do not share."

        # Try MSG91 first if in India or MSG91 configured
        if settings.msg91_auth_key:
            dispatched = await dispatch_sms_msg91(identifier, otp_code)
            provider_used = "msg91" if dispatched else "msg91_failed"

        # Fallback to Twilio
        if not dispatched and settings.twilio_account_sid:
            dispatched = await dispatch_sms_twilio(identifier, sms_body)
            provider_used = "twilio" if dispatched else "twilio_failed"

        if not dispatched:
            logger.warning(
                "[SMS DISPATCH SKIPPED] No active SMS provider configured (Twilio/MSG91). Phone: %s",
                identifier,
            )
            provider_used = "sms_unconfigured"

    return {
        "dispatched": dispatched,
        "provider": provider_used,
        "identifier": identifier,
        "purpose": purpose,
    }
