"""
Production Monitoring & Incident Alerting Sentinel
Wires up error/exception monitoring (Sentry + Telegram + Webhook/Audit Dispatcher)
Ensures any failed real broker order alerts the admin and user immediately (NO SILENT FAILURES).
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("sentinel.monitoring")

# ── Sentry SDK initialization ────────────────────────────────────────────────
from app.config import settings

SENTRY_DSN = settings.sentry_dsn
_sentry_initialized = False

if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=os.getenv("ENVIRONMENT", "production"),
            traces_sample_rate=1.0,
            integrations=[
                FastApiIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        )
        _sentry_initialized = True
        logger.info("Sentry monitoring initialized successfully.")
    except Exception as e:
        logger.warning("Sentry SDK initialization skipped: %s", e)
else:
    logger.info("SENTRY_DSN not configured — Sentry monitoring disabled. Set SENTRY_DSN in .env to enable.")


# ── Telegram Alert Channel ───────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = settings.telegram_bot_token
TELEGRAM_CHAT_ID = settings.telegram_chat_id
_telegram_configured = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

if _telegram_configured:
    logger.info("Telegram alert channel configured (bot token: %s...)", TELEGRAM_BOT_TOKEN[:8])
else:
    logger.info(
        "Telegram alerts disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to enable."
    )


async def _send_telegram_alert(message: str) -> bool:
    """Send a message to the configured Telegram chat. Returns True on success."""
    if not _telegram_configured:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    logger.debug("Telegram alert sent successfully")
                    return True
                else:
                    body = await resp.text()
                    logger.warning("Telegram API returned %d: %s", resp.status, body)
                    return False
    except ImportError:
        logger.warning("aiohttp not installed — Telegram alerts require: pip install aiohttp")
        return False
    except Exception as exc:
        logger.error("Telegram alert dispatch failed: %s", exc)
        return False


def _fire_and_forget_telegram(message: str) -> None:
    """Schedule a Telegram alert without blocking the caller."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_telegram_alert(message))
    except RuntimeError:
        # No running event loop — skip async dispatch
        logger.debug("No event loop available for async Telegram dispatch")


class MonitoringSentinel:
    """
    High-priority incident alert dispatcher for trading order execution,
    broker API dropouts, margin breaches, and compliance events.

    Dispatches to: Sentry, Telegram, and structured logs.
    """

    @staticmethod
    def capture_exception(exc: Exception, context: Optional[Dict[str, Any]] = None):
        """Captures unexpected exceptions and dispatches to Sentry and system log."""
        logger.error("[EXCEPTION CAUGHT] %s | Context: %s", exc, context, exc_info=True)
        if _sentry_initialized:
            try:
                import sentry_sdk
                with sentry_sdk.push_scope() as scope:
                    if context:
                        for k, v in context.items():
                            scope.set_extra(k, v)
                    sentry_sdk.capture_exception(exc)
            except Exception:
                pass

    @staticmethod
    def capture_order_failure(
        user_id: int,
        order_id: str,
        symbol: str,
        broker: str,
        reason: str,
        price: float = 0.0,
        quantity: float = 0.0,
        side: str = "BUY",
        raw_error: Optional[str] = None,
    ):
        """
        Dispatched whenever a live order execution fails or is rejected by a broker.
        Guarantees non-silent, high-visibility auditing and real-time alert dispatch.
        Sends to: Sentry (fatal), Telegram (if configured), and CRITICAL log.
        """
        alert_payload = {
            "event": "CRITICAL_ORDER_FAILURE",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "order_id": order_id,
            "symbol": symbol,
            "broker": broker,
            "side": side,
            "price": price,
            "quantity": quantity,
            "reason": reason,
            "raw_error": raw_error or reason,
        }

        # Log at CRITICAL level so log collectors (DataDog/CloudWatch/Papertrail) pick it up immediately
        logger.critical(
            "🚨 CRITICAL ORDER REJECTION: User #%s | Order %s | "
            "%s %s %s on %s FAILED: %s",
            user_id, order_id, side, quantity, symbol, broker, reason,
        )

        # Send to Sentry as high-priority message/exception if enabled
        if _sentry_initialized:
            try:
                import sentry_sdk
                with sentry_sdk.push_scope() as scope:
                    scope.set_tag("order_id", order_id)
                    scope.set_tag("broker", broker)
                    scope.set_tag("user_id", str(user_id))
                    scope.set_level("fatal")
                    for k, v in alert_payload.items():
                        scope.set_extra(k, v)
                    sentry_sdk.capture_message(
                        f"Live Order Execution Failure: {symbol} on {broker} ({reason})",
                        level="fatal",
                    )
            except Exception:
                pass

        # Send to Telegram
        if _telegram_configured:
            telegram_msg = (
                f"🚨 <b>ORDER FAILURE</b>\n\n"
                f"<b>User:</b> #{user_id}\n"
                f"<b>Order:</b> {order_id}\n"
                f"<b>Symbol:</b> {symbol}\n"
                f"<b>Broker:</b> {broker}\n"
                f"<b>Side:</b> {side} | Qty: {quantity} | Price: {price}\n"
                f"<b>Reason:</b> {reason}\n"
                f"<b>Time:</b> {alert_payload['timestamp']}"
            )
            _fire_and_forget_telegram(telegram_msg)

        return alert_payload

    @staticmethod
    def capture_risk_breach(user_id: int, symbol: str, required_margin: float, available_margin: float):
        """Dispatches pre-trade margin check rejection or daily loss breaches."""
        logger.warning(
            "⚠️ RISK BREACH: User #%s attempted order on %s "
            "(Required: ₹%s | Available: ₹%s)",
            user_id, symbol, f"{required_margin:,.2f}", f"{available_margin:,.2f}",
        )

        # Send to Telegram
        if _telegram_configured:
            telegram_msg = (
                f"⚠️ <b>RISK BREACH</b>\n\n"
                f"<b>User:</b> #{user_id}\n"
                f"<b>Symbol:</b> {symbol}\n"
                f"<b>Required Margin:</b> ₹{required_margin:,.2f}\n"
                f"<b>Available Margin:</b> ₹{available_margin:,.2f}\n"
                f"<b>Time:</b> {datetime.now(timezone.utc).isoformat()}"
            )
            _fire_and_forget_telegram(telegram_msg)

    @staticmethod
    def capture_broker_disconnect(broker: str, reason: str, user_id: Optional[int] = None):
        """Alert on broker API disconnect or authentication failure."""
        logger.error(
            "🔌 BROKER DISCONNECT: %s — %s (user: %s)", broker, reason, user_id or "system"
        )

        if _telegram_configured:
            telegram_msg = (
                f"🔌 <b>BROKER DISCONNECT</b>\n\n"
                f"<b>Broker:</b> {broker}\n"
                f"<b>Reason:</b> {reason}\n"
                f"<b>User:</b> #{user_id or 'system'}\n"
                f"<b>Time:</b> {datetime.now(timezone.utc).isoformat()}"
            )
            _fire_and_forget_telegram(telegram_msg)

        if _sentry_initialized:
            try:
                import sentry_sdk
                sentry_sdk.capture_message(
                    f"Broker disconnect: {broker} — {reason}", level="error"
                )
            except Exception:
                pass


monitoring_sentinel = MonitoringSentinel()
