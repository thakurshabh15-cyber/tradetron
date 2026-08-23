"""API endpoints for Broker Session Health Status, Manual Renewal Triggers, and Renewal Audit Logs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.logging import get_logger
from app.db.session import get_db
from app.engine.broker_cron import broker_renewal_engine
from app.models.broker_account import BrokerAccountRecord, BrokerSessionLogRecord
from app.models.user import UserRecord

logger = get_logger("api.broker_cron")

router = APIRouter(prefix="/api/brokers", tags=["broker_cron"])


@router.post("/renew-all")
async def trigger_renew_all_sessions(
    user: UserRecord = Depends(get_current_user),
):
    """Manually trigger immediate session renewal and live TOTP authentication across all active accounts."""
    filter_user_id = None if user.role == "admin" else user.id
    result = await broker_renewal_engine.renew_all_broker_sessions(user_id=filter_user_id)
    return {
        "success": True,
        "message": f"Renewed {result['successful_renewals']}/{result['total_accounts']} broker sessions successfully.",
        **result,
    }


@router.post("/{account_id}/renew")
async def trigger_renew_single_account(
    account_id: str,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger immediate renewal for a specific broker account."""
    acc = await db.get(BrokerAccountRecord, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Broker account not found")
    if acc.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    res = await broker_renewal_engine.renew_single_broker_session(account_id)
    return res


@router.get("/health-status")
async def get_broker_sessions_health_status(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve comprehensive session health status for all linked broker accounts."""
    stmt = select(BrokerAccountRecord).where(BrokerAccountRecord.user_id == user.id)
    res = await db.execute(stmt)
    accounts = res.scalars().all()

    output = []
    now_utc = datetime.now(timezone.utc)
    soon_threshold = now_utc + timedelta(hours=2)

    for acc in accounts:
        # Normalize expiry to timezone-aware UTC
        expiry = acc.token_expires_at
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        # Determine fine-grained health state
        if not acc.is_active:
            health = "INACTIVE"
        elif acc.is_token_expired():
            health = "EXPIRED"
        elif acc.status == "ERROR":
            health = "AUTH_FAILED"
        elif expiry and expiry <= soon_threshold:
            health = "EXPIRING_SOON"
        else:
            health = "ACTIVE"

        # Fetch latest renewal log
        log_stmt = (
            select(BrokerSessionLogRecord)
            .where(BrokerSessionLogRecord.broker_account_id == acc.id)
            .order_by(desc(BrokerSessionLogRecord.renewed_at))
            .limit(1)
        )
        log_res = await db.execute(log_stmt)
        latest_log = log_res.scalar_one_or_none()

        output.append({
            "account_id": acc.id,
            "broker_name": acc.broker_name,
            "account_name": acc.account_name,
            "client_id": acc.client_id,
            "api_key_masked": acc.api_key_masked,
            "health_status": health,
            "is_active": acc.is_active,
            "token_expires_at": acc.token_expires_at.isoformat() if acc.token_expires_at else None,
            "last_synced_at": acc.last_synced_at.isoformat() if acc.last_synced_at else None,
            "has_totp_configured": bool(acc.totp_secret_encrypted),
            "latest_renewal": {
                "status": latest_log.status if latest_log else "PENDING",
                "message": latest_log.message if latest_log else "Awaiting initial daily renewal",
                "renewed_at": latest_log.renewed_at.isoformat() if latest_log and latest_log.renewed_at else None,
                "latency_ms": latest_log.latency_ms if latest_log else None,
            },
        })

    return {
        "total_connected": len(accounts),
        "active_healthy": sum(1 for a in output if a["health_status"] == "ACTIVE"),
        "expiring_soon": sum(1 for a in output if a["health_status"] == "EXPIRING_SOON"),
        "expired_or_failed": sum(1 for a in output if a["health_status"] in ("EXPIRED", "AUTH_FAILED")),
        "next_scheduled_renewal_ist": "08:45 AM IST",
        "accounts": output,
    }


@router.get("/renewal-logs")
async def get_broker_renewal_logs(
    limit: int = Query(25, ge=1, le=100),
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve historical session renewal audit logs."""
    stmt = (
        select(BrokerSessionLogRecord)
        .where(BrokerSessionLogRecord.user_id == user.id)
        .order_by(desc(BrokerSessionLogRecord.renewed_at))
        .limit(limit)
    )
    res = await db.execute(stmt)
    logs = res.scalars().all()

    return [
        {
            "id": l.id,
            "broker_account_id": l.broker_account_id,
            "broker_name": l.broker_name,
            "status": l.status,
            "message": l.message,
            "renewed_at": l.renewed_at.isoformat() if l.renewed_at else None,
            "latency_ms": l.latency_ms,
        }
        for l in logs
    ]
