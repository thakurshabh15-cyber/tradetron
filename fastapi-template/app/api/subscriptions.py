"""Read-only subscription status and plan entitlement endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.db.session import get_db
from app.engine.subscription import PLAN_LIMITS, subscription_engine
from app.models.user import UserRecord

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


@router.get("/plans")
async def list_subscription_plans():
    """Return the canonical plan catalogue and limits used by the access engine."""
    return [
        {
            "code": code,
            "name": limits["display_name"],
            "price_monthly": limits["price_monthly"],
            "price_yearly": limits["price_yearly"],
            "max_brokers": limits["max_brokers"],
            "copy_trading_allowed": limits["copy_trading_allowed"],
            "max_algos": limits["max_algos"],
            "limits": {
                "max_brokers": limits["max_brokers"],
                "max_strategies": limits["max_algos"],
                "copy_trading_allowed": limits["copy_trading_allowed"],
            },
        }
        for code, limits in PLAN_LIMITS.items()
        if code != "ELITE"
    ]


@router.get("/current")
async def current_subscription(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the authenticated user's active plan and effective entitlements."""
    plan_code, limits = await subscription_engine.get_user_plan(user.id, db)
    entitlements = await subscription_engine.get_entitlements(user.id, db)
    return {
        "plan_code": plan_code,
        "plan_name": limits["display_name"],
        "status": "ACTIVE",
        "limits": {
            "max_brokers": limits["max_brokers"],
            "max_strategies": limits["max_algos"],
            "copy_trading_allowed": limits["copy_trading_allowed"],
        },
        "entitlements": entitlements,
    }