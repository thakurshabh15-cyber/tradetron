"""Production Notification & OTP Dispatch Service.

Dispatches real OTP verification codes and transactional security notices via:
- Resend API (HTTPS transactional email)
- SMTP (SendGrid, AWS SES, Mailgun, Postmark, custom TLS/SSL SMTP)
- SMS (Twilio, MSG91 - optional, non-blocking)
- Operational logging fallback when credentials are not configured.
"""

from __future__ import annotations

import asyncio
import email.message
import json
import smtplib
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.core.logging import get_logger

logger = get_logger("notifications.dispatcher")


async def dispatch_email_resend(
    to_email: str,
    subject: str,
    text_content: str,
    html_content: Optional[str] = None,
) -> tuple[bool, str, Optional[str]]:
    """Send transactional email via Resend HTTP API.
    
    Returns:
        (success: bool, message/error: str, resend_id: Optional[str])
    """
    if not settings.resend_api_key:
        return False, "RESEND_API_KEY is not configured in server environment.", None

    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    from_email = settings.resend_from_email or "onboarding@resend.dev"
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "text": text_content,
    }
    if html_content:
        payload["html"] = html_content

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (200, 201):
                res_json = resp.json()
                msg_id = res_json.get("id")
                logger.info("Resend email sent successfully to %s: %s (id=%s)", to_email, subject, msg_id)
                return True, f"Email delivered via Resend (id={msg_id})", msg_id
            else:
                err_text = resp.text
                try:
                    err_json = resp.json()
                    err_text = err_json.get("message", err_text)
                except Exception:
                    pass
                logger.error("Resend API error (%d): %s", resp.status_code, err_text)
                return False, f"Resend API error ({resp.status_code}): {err_text}", None
    except Exception as exc:
        logger.error("Resend API request exception: %s", exc)
        return False, f"Resend request failed: {str(exc)}", None


async def dispatch_email_smtp(
    to_email: str,
    subject: str,
    text_content: str,
    html_content: Optional[str] = None,
) -> tuple[bool, str]:
    """Send a transactional email using configured SMTP provider."""
    if not settings.smtp_host or not settings.smtp_user:
        return False, "SMTP is not configured."

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
            logger.info("SMTP email dispatched successfully to %s: %s", to_email, subject)
            return True, "Email sent via SMTP"
        except Exception as exc:
            logger.error("Failed to send email to %s via SMTP %s: %s", to_email, settings.smtp_host, exc)
            return False, f"SMTP delivery failed: {str(exc)}"

    return await asyncio.to_thread(_sync_send)


async def dispatch_email(
    to_email: str,
    subject: str,
    text_content: str,
    html_content: Optional[str] = None,
) -> tuple[bool, str, Optional[str]]:
    """Send transactional email using Resend API (primary) or SMTP (fallback)."""
    # 1. Try Resend if configured
    if settings.resend_api_key:
        success, msg, resend_id = await dispatch_email_resend(to_email, subject, text_content, html_content)
        if success:
            return True, msg, resend_id
        # If Resend failed with an explicit error, return that error rather than hiding it
        return False, msg, None

    # 2. Try SMTP if configured
    if settings.smtp_host and settings.smtp_user:
        success, msg = await dispatch_email_smtp(to_email, subject, text_content, html_content)
        if success:
            return True, msg, None
        return False, msg, None

    fallback_msg = f"[EMAIL NOTICE] No active email provider (RESEND_API_KEY or SMTP) configured on server. Recipient: {to_email}"
    logger.warning(fallback_msg)
    return False, fallback_msg, None


async def dispatch_sms_twilio(to_phone: str, message: str) -> bool:
    """Send SMS via Twilio API (optional)."""
    if not settings.twilio_account_sid or not settings.twilio_auth_token or not settings.twilio_from_number:
        return False

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
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, data=data, headers=headers)
            if resp.status_code in (200, 201):
                logger.info("Twilio SMS sent to %s", to_phone)
                return True
            else:
                logger.error("Twilio SMS error (%d): %s", resp.status_code, resp.text)
                return False
    except Exception as exc:
        logger.error("Twilio SMS request failed: %s", exc)
        return False


async def dispatch_sms_msg91(to_phone: str, otp_code: str) -> bool:
    """Send OTP SMS via MSG91 API (optional)."""
    if not settings.msg91_auth_key:
        return False

    clean_phone = to_phone.replace("+", "").replace(" ", "").replace("-", "")
    url = f"https://api.msg91.com/api/v5/otp?template_id={settings.msg91_template_id or 'default'}&mobile={clean_phone}&authkey={settings.msg91_auth_key}&otp={otp_code}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url)
            if resp.status_code == 200:
                logger.info("MSG91 OTP SMS sent to %s", to_phone)
                return True
            else:
                logger.error("MSG91 error (%d): %s", resp.status_code, resp.text)
                return False
    except Exception as exc:
        logger.error("MSG91 request failed: %s", exc)
        return False


async def dispatch_otp(
    identifier: str,
    otp_code: str,
    purpose: str = "registration",
) -> dict[str, Any]:
    """Universal OTP dispatcher routing to Email (Resend/SMTP) or SMS providers."""
    is_email = "@" in identifier
    dispatched = False
    provider_used = "none"
    detail_message = ""
    resend_msg_id = None

    if is_email:
        subject = {
            "registration": "Verify Your TradeThrone Account",
            "login": "Your TradeThrone Login Verification Code",
            "password_reset": "Reset Your TradeThrone Password",
        }.get(purpose, "Your TradeThrone Verification Code")

        text_body = (
            f"Hello,\n\n"
            f"Your TradeThrone verification code is: {otp_code}\n\n"
            f"This code will expire in 15 minutes.\n"
            f"If you did not request this code, please ignore this message.\n\n"
            f"— TradeThrone Security Team"
        )

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: Arial, sans-serif; background-color: #0f172a; color: #f8fafc; padding: 24px;">
          <div style="max-width: 480px; margin: 0 auto; background: #1e293b; border-radius: 12px; padding: 32px; border: 1px solid #334155;">
            <h2 style="color: #38bdf8; margin-top: 0;">TradeThrone Security Verification</h2>
            <p style="color: #94a3b8; font-size: 14px;">Use the verification code below to complete your {purpose.replace('_', ' ')}:</p>
            <div style="background: #0f172a; border-radius: 8px; padding: 18px; text-align: center; margin: 24px 0; border: 1px dashed #0284c7;">
              <span style="font-size: 32px; font-weight: bold; letter-spacing: 6px; color: #38bdf8; font-family: monospace;">{otp_code}</span>
            </div>
            <p style="color: #64748b; font-size: 12px;">This code is valid for 15 minutes. Never share this code with anyone.</p>
            <hr style="border: 0; border-top: 1px solid #334155; margin: 24px 0;" />
            <p style="color: #475569; font-size: 11px; margin: 0;">TradeThrone Algorithmic Trading Platform • Secure Operations</p>
          </div>
        </body>
        </html>
        """

        dispatched, detail_message, resend_msg_id = await dispatch_email(
            to_email=identifier,
            subject=subject,
            text_content=text_body,
            html_content=html_body,
        )
        provider_used = "resend" if settings.resend_api_key else ("smtp" if settings.smtp_host else "unconfigured")

    else:
        # SMS route (optional)
        sms_body = f"Your TradeThrone verification code is {otp_code}. Valid for 15 minutes. Do not share."

        if settings.msg91_auth_key:
            dispatched = await dispatch_sms_msg91(identifier, otp_code)
            provider_used = "msg91" if dispatched else "msg91_failed"
            detail_message = "SMS dispatched via MSG91" if dispatched else "MSG91 dispatch failed"

        if not dispatched and settings.twilio_account_sid:
            dispatched = await dispatch_sms_twilio(identifier, sms_body)
            provider_used = "twilio" if dispatched else "twilio_failed"
            detail_message = "SMS dispatched via Twilio" if dispatched else "Twilio dispatch failed"

        if not dispatched:
            logger.info("[SMS SKIPPED] SMS provider not configured for %s (non-blocking).", identifier)
            provider_used = "sms_skipped"
            detail_message = "SMS provider not configured (non-blocking)"

    return {
        "dispatched": dispatched,
        "provider": provider_used,
        "identifier": identifier,
        "purpose": purpose,
        "message": detail_message,
        "resend_id": resend_msg_id,
    }
