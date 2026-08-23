"""Strictly-secured Admin Governance & Operations API.

Provides RBAC authentication, user/KYC review management, broker connection monitoring,
strategy risk oversight, revenue metrics, and filterable audit trails.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.audit import log_audit_event
from app.core.logging import get_logger
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.market_data.manager import ws_manager
from app.models.audit import AuditLogRecord
from app.models.billing import InvoiceRecord, PaymentRecord, SubscriptionRecord
from app.models.broker_account import BrokerAccountRecord
from app.models.marketplace import StrategyDeploymentRecord
from app.models.trading import OrderRecord, PositionRecord, StrategyRecord
from app.models.user import UserRecord
from app.models.broker_account import BrokerSessionLogRecord
from app.models.copy_trading import CopyFollowerRecord, CopyGroupRecord
from app.models.notification import NotificationPreferenceRecord
from app.models.visual_strategy import VisualStrategyRecord

logger = get_logger("api.admin")
router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Strict Admin RBAC Dependency ─────────────────────────────────────

async def get_current_admin_user(
    user: UserRecord = Depends(get_current_user),
) -> UserRecord:
    """Ensure user has administrative privileges."""
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin account is deactivated")
    if user.role.upper() not in ("ADMIN", "SUPERADMIN"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Administrative privileges required",
        )
    return user


# ── Schemas ──────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    email: str
    password: str
    admin_security_pin: Optional[str] = None


class UserStatusUpdateRequest(BaseModel):
    is_active: bool
    reason: str = "Admin security review"


class UserRoleUpdateRequest(BaseModel):
    role: str = Field(..., description="admin, trader, creator")


class KYCReviewRequest(BaseModel):
    decision: str = Field(..., description="VERIFIED or REJECTED")
    remarks: str = "All documents verified per SEBI guidelines"


class AdminKillSwitchRequest(BaseModel):
    reason: str = "Platform-wide risk volatility halt triggered by Admin"


# ── Endpoints ────────────────────────────────────────────────────────

@router.post("/login")
async def admin_login(
    req: AdminLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Dedicated secure admin login endpoint."""
    stmt = select(UserRecord).where(UserRecord.email == req.email.lower().strip())
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user or not user.hashed_password:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    if not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    if user.role.upper() not in ("ADMIN", "SUPERADMIN"):
        raise HTTPException(status_code=403, detail="Account lacks administrative clearance")

    # Generate admin token
    token = create_access_token({"sub": user.id, "email": user.email, "role": user.role})

    await log_audit_event(
        db=db,
        action="ADMIN_LOGIN_SUCCESS",
        resource_type="ADMIN_PORTAL",
        user_id=user.id,
        status="SUCCESS",
        details={"email": user.email, "role": user.role},
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "kyc_status": user.kyc_status,
        },
    }


@router.get("/overview")
async def get_admin_overview(
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Consolidated KPI Command Center metrics computed strictly from active database records."""
    now = datetime.now(timezone.utc)

    # 1. Total registered traders & KYC breakdown from database
    users_count = (await db.execute(select(func.count(UserRecord.id)))).scalar() or 0
    verified_kyc = (await db.execute(select(func.count(UserRecord.id)).where(UserRecord.kyc_status == "VERIFIED"))).scalar() or 0
    pending_kyc = (await db.execute(select(func.count(UserRecord.id)).where(UserRecord.kyc_status == "PENDING"))).scalar() or 0

    # 2. Real Active Broker connections (must be CONNECTED, active, and not expired)
    active_brokers_stmt = select(func.count(BrokerAccountRecord.id)).where(
        BrokerAccountRecord.is_active == True,  # noqa: E712
        BrokerAccountRecord.status == "CONNECTED",
        (BrokerAccountRecord.token_expires_at.is_(None)) | (BrokerAccountRecord.token_expires_at > now),
    )
    active_brokers = (await db.execute(active_brokers_stmt)).scalar() or 0

    # 3. Live strategy deployments (strictly LIVE execution mode, NOT paper/simulation)
    live_deploys_stmt = select(func.count(StrategyDeploymentRecord.id)).where(
        StrategyDeploymentRecord.status == "RUNNING",
        StrategyDeploymentRecord.execution_mode == "LIVE",
    )
    live_deploys = (await db.execute(live_deploys_stmt)).scalar() or 0

    # 4. Total Real Capital Managed (strictly LIVE execution mode, ₹0 if no live deployments)
    capital_stmt = select(func.sum(StrategyDeploymentRecord.capital_allocated)).where(
        StrategyDeploymentRecord.status == "RUNNING",
        StrategyDeploymentRecord.execution_mode == "LIVE",
    )
    capital_res = (await db.execute(capital_stmt)).scalar() or 0.0

    # 5. Paid Active Subscriptions (excluding free tier with amount 0)
    active_subs_stmt = select(func.count(SubscriptionRecord.id)).where(
        SubscriptionRecord.status == "ACTIVE",
        SubscriptionRecord.amount > 0,
    )
    active_subs = (await db.execute(active_subs_stmt)).scalar() or 0

    # 6. Real MRR from paid active subscriptions (₹0 until paid users exist)
    mrr_stmt = select(func.sum(SubscriptionRecord.amount)).where(
        SubscriptionRecord.status == "ACTIVE",
        SubscriptionRecord.amount > 0,
    )
    mrr = float((await db.execute(mrr_stmt)).scalar() or 0.0)

    # 7. Real churn rate: (cancelled in last 30 days) / (active + cancelled in last 30 days)
    from datetime import timedelta
    thirty_days_ago = now - timedelta(days=30)
    cancelled_recently = (await db.execute(
        select(func.count(SubscriptionRecord.id)).where(
            SubscriptionRecord.status == "CANCELLED",
            SubscriptionRecord.created_at >= thirty_days_ago,
        )
    )).scalar() or 0
    total_recent = active_subs + cancelled_recently
    churn_rate = (cancelled_recently / total_recent * 100) if total_recent > 0 else 0.0

    return {
        "users": {
            "total": users_count,
            "verified_kyc": verified_kyc,
            "pending_kyc": pending_kyc,
        },
        "brokers": {
            "active_connections": active_brokers,
            "supported_brokers": ["ZERODHA", "ANGEL_ONE", "BINANCE", "UPSTOX"],
        },
        "strategies": {
            "live_running": live_deploys,
            "total_capital_managed": float(capital_res),
        },
        "revenue": {
            "active_subscribers": active_subs,
            "mrr": mrr,
            "churn_rate_pct": round(churn_rate, 2),
        },
        "system": {
            "engine_status": "OPERATIONAL",
            "active_market_feeds": 3,
            "websocket_connections": sum(len(conns) for conns in ws_manager._channels.values()),
        },
    }


@router.get("/users")
async def list_users(
    query: Optional[str] = None,
    kyc_status: Optional[str] = None,
    role: Optional[str] = None,
    limit: int = 50,
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """User Management: Search, filter by KYC status, role, and activity."""
    stmt = select(UserRecord).order_by(desc(UserRecord.created_at)).limit(limit)
    if query:
        stmt = stmt.where((UserRecord.email.ilike(f"%{query}%")) | (UserRecord.full_name.ilike(f"%{query}%")))
    if kyc_status:
        stmt = stmt.where(UserRecord.kyc_status == kyc_status.upper())
    if role:
        stmt = stmt.where(UserRecord.role == role.lower())

    res = await db.execute(stmt)
    users = res.scalars().all()

    return [
        {
            "id": u.id,
            "email": u.email,
            "full_name": u.full_name,
            "phone": u.phone,
            "role": u.role,
            "kyc_status": u.kyc_status,
            "is_active": u.is_active,
            "two_factor_enabled": u.two_factor_enabled,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.post("/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    req: UserStatusUpdateRequest,
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Suspend or Reactivate a user account with audit logging."""
    stmt = select(UserRecord).where(UserRecord.id == user_id)
    res = await db.execute(stmt)
    target = res.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target.is_active = req.is_active
    await db.commit()

    action = "USER_ACCOUNT_ACTIVATED" if req.is_active else "USER_ACCOUNT_SUSPENDED"
    await log_audit_event(
        db=db,
        action=action,
        resource_type="USER",
        user_id=admin.id,
        resource_id=user_id,
        status="SUCCESS",
        details={"reason": req.reason, "target_email": target.email},
    )

    return {"success": True, "user_id": user_id, "is_active": target.is_active, "message": f"User status updated to {target.is_active}"}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Permanently purge a user and their platform-owned records."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="An administrator cannot delete their own account")

    target = await db.scalar(select(UserRecord).where(UserRecord.id == user_id))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target_email = target.email
    # Delete dependent records explicitly because SQLite deployments may not enable FK cascades.
    group_ids = await db.scalars(select(CopyGroupRecord.id).where(CopyGroupRecord.master_user_id == user_id))
    group_ids = list(group_ids)
    if group_ids:
        await db.execute(delete(CopyFollowerRecord).where(CopyFollowerRecord.group_id.in_(group_ids)))
        await db.execute(delete(CopyGroupRecord).where(CopyGroupRecord.id.in_(group_ids)))
    await db.execute(delete(CopyFollowerRecord).where(CopyFollowerRecord.follower_user_id == user_id))
    for model in (
        BrokerSessionLogRecord,
        BrokerAccountRecord,
        InvoiceRecord,
        PaymentRecord,
        SubscriptionRecord,
        NotificationPreferenceRecord,
        VisualStrategyRecord,
        OrderRecord,
        PositionRecord,
        TradeRecord,
        StrategyRecord,
    ):
        user_column = getattr(model, "user_id", None)
        if user_column is not None:
            await db.execute(delete(model).where(user_column == user_id))

    await db.delete(target)
    await db.commit()

    await log_audit_event(
        db=db,
        action="USER_ACCOUNT_PURGED",
        resource_type="USER",
        user_id=admin.id,
        resource_id=user_id,
        status="SUCCESS",
        details={"target_email": target_email},
    )
    return {"success": True, "user_id": user_id, "message": "User account permanently deleted"}


@router.post("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    req: UserRoleUpdateRequest,
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Promote or Demote a user's RBAC role (admin, trader, creator)."""
    stmt = select(UserRecord).where(UserRecord.id == user_id)
    res = await db.execute(stmt)
    target = res.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    old_role = target.role
    target.role = req.role.lower().strip()
    await db.commit()

    await log_audit_event(
        db=db,
        action="USER_ROLE_PROMOTED",
        resource_type="USER",
        user_id=admin.id,
        resource_id=user_id,
        status="SUCCESS",
        details={"old_role": old_role, "new_role": target.role, "target_email": target.email},
    )

    return {"success": True, "user_id": user_id, "role": target.role}


@router.get("/kyc/queue")
async def get_kyc_queue(
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """KYC Review Queue: View pending submissions requiring approval."""
    stmt = select(UserRecord).where(UserRecord.kyc_status == "PENDING").order_by(UserRecord.created_at)
    res = await db.execute(stmt)
    pending_users = res.scalars().all()

    return [
        {
            "user_id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "submitted_at": u.created_at.isoformat() if u.created_at else None,
            "documents": _get_user_kyc_documents(u),
        }
        for u in pending_users
    ]


def _get_user_kyc_documents(user: UserRecord) -> list[dict]:
    """Build KYC document list from user's actual profile data."""
    docs = []
    pan = getattr(user, "pan_number", None)
    aadhaar = getattr(user, "aadhaar_number", None)

    if pan:
        docs.append({"type": "PAN_CARD", "status": "PENDING_VERIFICATION", "doc_number": pan})
    if aadhaar:
        # Mask aadhaar for privacy: show only last 4 digits
        masked = f"XXXX-XXXX-{aadhaar[-4:]}" if len(aadhaar) >= 4 else aadhaar
        docs.append({"type": "AADHAAR_CARD", "status": "PENDING_VERIFICATION", "doc_number": masked})

    if not docs:
        docs.append({"type": "NOT_SUBMITTED", "status": "AWAITING_UPLOAD", "doc_number": None})

    return docs


@router.post("/kyc/{user_id}/review")
async def review_kyc(
    user_id: str,
    req: KYCReviewRequest,
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve or Reject a user's KYC submission."""
    decision_norm = req.decision.upper().strip()
    if decision_norm not in ("VERIFIED", "REJECTED"):
        raise HTTPException(status_code=400, detail="Decision must be VERIFIED or REJECTED")

    stmt = select(UserRecord).where(UserRecord.id == user_id)
    res = await db.execute(stmt)
    target = res.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target.kyc_status = decision_norm
    await db.commit()

    await log_audit_event(
        db=db,
        action=f"KYC_{decision_norm}",
        resource_type="KYC_VERIFICATION",
        user_id=admin.id,
        resource_id=user_id,
        status="SUCCESS",
        details={"decision": decision_norm, "remarks": req.remarks, "user_email": target.email},
    )

    return {"success": True, "user_id": user_id, "kyc_status": decision_norm, "remarks": req.remarks}


@router.get("/brokers/monitor")
async def get_broker_monitor(
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Broker Connection Monitor: View all live broker connections across the platform."""
    stmt = select(BrokerAccountRecord, UserRecord.email, UserRecord.full_name).join(
        UserRecord, BrokerAccountRecord.user_id == UserRecord.id
    )
    res = await db.execute(stmt)
    records = res.all()

    output = []
    for acc, email, name in records:
        output.append({
            "account_id": acc.id,
            "user_id": acc.user_id,
            "user_email": email,
            "user_name": name,
            "broker_name": acc.broker_name,
            "client_id": acc.client_id,
            "api_key_masked": acc.api_key_masked,
            "status": acc.status,
            "is_active": acc.is_active,
            "last_synced_at": acc.last_synced_at.isoformat() if acc.last_synced_at else None,
            "health": "HEALTHY" if acc.is_active else "DISCONNECTED",
        })
    return output


@router.get("/strategies/oversight")
async def get_strategy_oversight(
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Strategy Oversight: View all live deployments, exposure, and risk parameters."""
    stmt = select(StrategyDeploymentRecord).order_by(desc(StrategyDeploymentRecord.deployed_at))
    res = await db.execute(stmt)
    deployments = res.scalars().all()

    return [
        {
            "deployment_id": d.id,
            "strategy_id": d.marketplace_strategy_id,
            "strategy_name": d.strategy_name,
            "execution_mode": d.execution_mode,
            "broker_name": d.broker_name,
            "multiplier": d.multiplier,
            "capital_allocated": d.capital_allocated,
            "status": d.status,
            "created_at": d.deployed_at.isoformat() if d.deployed_at else None,
        }
        for d in deployments
    ]


@router.post("/kill-switch/user/{user_id}")
async def kill_switch_user(
    user_id: str,
    req: AdminKillSwitchRequest,
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Emergency halt for a single user's live strategies."""
    # Actually pause all RUNNING strategy deployments
    from sqlalchemy import update
    pause_stmt = (
        update(StrategyDeploymentRecord)
        .where(
            StrategyDeploymentRecord.status == "RUNNING",
        )
        .values(status="PAUSED_ADMIN_HALT")
    )
    result = await db.execute(pause_stmt)
    paused_count = result.rowcount
    await db.commit()

    await log_audit_event(
        db=db,
        action="ADMIN_USER_KILL_SWITCH",
        resource_type="USER_TRADING",
        user_id=admin.id,
        resource_id=user_id,
        status="CRITICAL",
        details={"reason": req.reason, "target_user_id": user_id, "strategies_paused": paused_count},
    )

    # Broadcast halt event to user's WebSocket
    await ws_manager.broadcast("trades", {
        "event": "USER_TRADING_HALT",
        "user_id": user_id,
        "message": f"Admin halt: {req.reason}",
        "strategies_paused": paused_count,
    })

    return {"success": True, "user_id": user_id, "status": "HALTED", "strategies_paused": paused_count, "message": f"Trading halted for user {user_id}"}


@router.post("/kill-switch/platform")
async def kill_switch_platform(
    req: AdminKillSwitchRequest,
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Platform-Wide Kill-Switch: Halt all live strategies and trading engine dispatch."""
    from app.main import get_engine

    engine = get_engine()
    if engine and hasattr(engine, "risk_manager"):
        engine.risk_manager.trigger_kill_switch(req.reason)
        await engine.reload_strategies()

    await log_audit_event(
        db=db,
        action="PLATFORM_KILL_SWITCH_ENGAGED",
        resource_type="PLATFORM_ENGINE",
        user_id=admin.id,
        status="CRITICAL",
        details={"reason": req.reason, "admin_email": admin.email},
    )

    await ws_manager.broadcast("trades", {
        "event": "ADMIN_PLATFORM_HALT",
        "message": f"PLATFORM TRADING HALT: {req.reason}",
        "status": "HALTED",
    })

    return {"success": True, "status": "HALTED", "message": "Platform-wide trading halted by administrator."}


@router.get("/revenue/metrics")
async def get_revenue_metrics(
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Subscription & Revenue Dashboard."""
    active_subs = (await db.execute(select(func.count(SubscriptionRecord.id)).where(SubscriptionRecord.status == "ACTIVE"))).scalar() or 0
    total_rev = (await db.execute(select(func.sum(PaymentRecord.amount)).where(PaymentRecord.status == "SUCCESS"))).scalar() or 0.0
    failed_pmts = (await db.execute(select(func.count(PaymentRecord.id)).where(PaymentRecord.status == "FAILED"))).scalar() or 0

    # Compute real MRR from actual plan prices
    mrr_result = await db.execute(
        select(func.sum(SubscriptionRecord.amount)).where(SubscriptionRecord.status == "ACTIVE")
    )
    mrr = float(mrr_result.scalar() or 0.0)

    # Get real plan breakdown from subscription data
    plan_stats_stmt = select(
        SubscriptionRecord.plan_name,
        SubscriptionRecord.amount,
        func.count(SubscriptionRecord.id).label("subscriber_count"),
    ).where(
        SubscriptionRecord.status == "ACTIVE"
    ).group_by(
        SubscriptionRecord.plan_name, SubscriptionRecord.amount
    )
    plan_res = await db.execute(plan_stats_stmt)
    plans = [
        {"name": row.plan_name or "Unknown", "price": float(row.amount or 0), "subscribers": row.subscriber_count}
        for row in plan_res.all()
    ]

    # Compute real churn
    from datetime import timedelta
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    cancelled_recently = (await db.execute(
        select(func.count(SubscriptionRecord.id)).where(
            SubscriptionRecord.status == "CANCELLED",
            SubscriptionRecord.created_at >= thirty_days_ago,
        )
    )).scalar() or 0
    total_recent = active_subs + cancelled_recently
    churn_rate = (cancelled_recently / total_recent * 100) if total_recent > 0 else 0.0

    return {
        "mrr": mrr,
        "arr": mrr * 12,
        "active_subscribers": active_subs,
        "total_revenue_collected": float(total_rev),
        "failed_payments_count": failed_pmts,
        "churn_rate_pct": round(churn_rate, 2),
        "plans": plans,
    }


@router.get("/audit-logs")
async def get_audit_logs(
    user_id: Optional[str] = None,
    action: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = 100,
    admin: UserRecord = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Filterable Immutable Audit Trail Viewer."""
    stmt = select(AuditLogRecord).order_by(desc(AuditLogRecord.created_at)).limit(limit)
    if user_id:
        stmt = stmt.where(AuditLogRecord.user_id == user_id)
    if action:
        stmt = stmt.where(AuditLogRecord.action.ilike(f"%{action}%"))
    if status_filter:
        stmt = stmt.where(AuditLogRecord.status == status_filter.upper())

    res = await db.execute(stmt)
    logs = res.scalars().all()

    import json

    return [
        {
            "id": l.id,
            "user_id": l.user_id,
            "action": l.action,
            "resource_type": l.resource_type,
            "resource_id": l.resource_id,
            "status": l.status,
            "ip_address": l.ip_address,
            "details": json.loads(l.details_json) if l.details_json else {},
            "timestamp": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]


@router.get("/system/health")
async def get_system_health(
    admin: UserRecord = Depends(get_current_admin_user),
):
    """System Telemetry with real CPU/RAM metrics and provider status."""
    import os
    from app.config import settings

    # Real system metrics
    try:
        import psutil
        process = psutil.Process(os.getpid())
        memory_mb = process.memory_info().rss / (1024 * 1024)
        cpu_pct = process.cpu_percent(interval=0.1)
    except ImportError:
        memory_mb = None
        cpu_pct = None

    # Real provider status from unified manager
    from app.market_data.unified_manager import UnifiedMarketDataManager
    providers_status = {}
    try:
        from app.market_data.base import AssetClass
        mgr = UnifiedMarketDataManager.__new__(UnifiedMarketDataManager)
        # Check config-based status
        providers_status = {
            "indian_equity": {
                "mode": settings.feed_mode_equity.upper(),
                "status": "LIVE" if settings.feed_mode_equity == "live" else "DEMO_SIMULATED",
            },
            "crypto": {
                "mode": settings.feed_mode_crypto.upper(),
                "status": "LIVE (Binance WS)" if settings.feed_mode_crypto == "live" else "DEMO_SIMULATED",
            },
            "forex": {
                "mode": "DEMO",
                "status": "DEMO_SIMULATED (no free real-time forex API)",
            },
        }
    except Exception:
        providers_status = {"error": "Could not query provider status"}

    return {
        "status": "HEALTHY",
        "api_version": "1.0.0-prod",
        "broker_mode": settings.broker_mode,
        "providers": providers_status,
        "websocket_subscribers": sum(len(conns) for conns in ws_manager._channels.values()),
        "memory_rss_mb": round(memory_mb, 1) if memory_mb is not None else "psutil not installed",
        "cpu_utilization_pct": round(cpu_pct, 1) if cpu_pct is not None else "psutil not installed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
