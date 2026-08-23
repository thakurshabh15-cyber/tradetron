"""Subscription Plan Limit Enforcement Engine.

Single source of truth for all feature-access decisions:
  - FREE  : 1 broker, 1 algo, no copy trading
  - PRO   : 3 brokers, 5 algos, copy trading allowed  (₹1,999/mo)
  - INSTITUTIONAL: unlimited brokers, unlimited algos, copy trading (₹4,999/mo)

Usage:
    from app.engine.subscription import subscription_engine
    await subscription_engine.verify_access(user.id, "broker_link", db, current_count=2)
    # raises HTTP 402 if over plan limit
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.subscription import SubscriptionRecord

logger = get_logger("engine.subscription")

# ── Plan definitions ──────────────────────────────────────────────────────────

PLAN_LIMITS: dict[str, dict[str, Any]] = {
    "FREE": {
        "display_name": "Free",
        "price_monthly": 0,
        "price_yearly": 0,
        "max_brokers": 1,
        "max_algos": 1,
        "copy_trading_allowed": False,
        "tick_speed": "1s",
        "historical_candles": "15m",
        "priority_support": False,
        "vip_vps": False,
        "api_access": False,
        "trade_execution_allowed": True,
        "required_plan_for_brokers": "PRO",
        "required_plan_for_algos": "PRO",
        "required_plan_for_copy_trading": "PRO",
    },
    "PRO": {
        "display_name": "Pro",
        "price_monthly": 1999,
        "price_yearly": 19190,  # ~20% discount
        "max_brokers": 3,
        "max_algos": 5,
        "copy_trading_allowed": True,
        "tick_speed": "100ms",
        "historical_candles": "1m",
        "priority_support": True,
        "vip_vps": False,
        "api_access": True,
        "trade_execution_allowed": True,
        "required_plan_for_brokers": "INSTITUTIONAL",
        "required_plan_for_algos": "INSTITUTIONAL",
        "required_plan_for_copy_trading": None,
    },
    "INSTITUTIONAL": {
        "display_name": "Institutional",
        "price_monthly": 4999,
        "price_yearly": 47990,  # ~20% discount
        "max_brokers": 99,
        "max_algos": 99,
        "copy_trading_allowed": True,
        "tick_speed": "10ms",
        "historical_candles": "1m",
        "priority_support": True,
        "vip_vps": True,
        "api_access": True,
        "trade_execution_allowed": True,
        "required_plan_for_brokers": None,
        "required_plan_for_algos": None,
        "required_plan_for_copy_trading": None,
    },
    # Legacy alias
    "ELITE": {
        "display_name": "Institutional",
        "price_monthly": 4999,
        "price_yearly": 47990,
        "max_brokers": 99,
        "max_algos": 99,
        "copy_trading_allowed": True,
        "tick_speed": "10ms",
        "historical_candles": "1m",
        "priority_support": True,
        "vip_vps": True,
        "api_access": True,
        "trade_execution_allowed": True,
        "required_plan_for_brokers": None,
        "required_plan_for_algos": None,
        "required_plan_for_copy_trading": None,
    },
}

# Feature → plan limit key mapping
FEATURE_MAP: dict[str, str] = {
    "broker_link": "max_brokers",
    "strategy_create": "max_algos",
    "copy_trading": "copy_trading_allowed",
    "trade_execution": "trade_execution_allowed",
}

# Feature → human-readable description for error messages
FEATURE_LABELS: dict[str, str] = {
    "broker_link": "broker account",
    "strategy_create": "algo strategy",
    "copy_trading": "copy trading",
}

# Feature → which plan unlocks it (for upgrade_required hints)
FEATURE_UPGRADE_PLAN: dict[str, dict[str, str]] = {
    "broker_link": {"FREE": "PRO", "PRO": "INSTITUTIONAL"},
    "strategy_create": {"FREE": "PRO", "PRO": "INSTITUTIONAL"},
    "copy_trading": {"FREE": "PRO", "PRO": None},
}


class SubscriptionEngine:
    """Centralised plan-limit enforcement engine."""

    async def get_user_plan(
        self, user_id: str, db: AsyncSession
    ) -> tuple[str, dict[str, Any]]:
        """Return (plan_name, limits_dict) for the user's active subscription.

        Falls back to FREE if no active subscription found.
        """
        stmt = (
            select(SubscriptionRecord)
            .where(
                SubscriptionRecord.user_id == user_id,
                SubscriptionRecord.status == "ACTIVE",
            )
            .order_by(SubscriptionRecord.created_at.desc())
        )
        res = await db.execute(stmt)
        sub = res.scalars().first()

        plan_name = "FREE"
        if sub and (sub.plan_code or sub.plan_name):
            candidate = (sub.plan_code or sub.plan_name).upper().strip()
            if candidate == "ELITE":
                candidate = "INSTITUTIONAL"
            if candidate in PLAN_LIMITS:
                plan_name = candidate
            else:
                logger.warning("Unknown plan name '%s' for user %s — defaulting to FREE", candidate, user_id)

        return plan_name, PLAN_LIMITS[plan_name]

    async def verify_feature_access(
        self,
        db: AsyncSession,
        user_id: str,
        feature: str,
        current_count: int | None = None,
    ) -> dict[str, Any]:
        """Public database-first access check used by API guards."""
        if current_count is None and feature in {"broker_link", "strategy_create"}:
            from app.models.broker_account import BrokerAccountRecord
            from app.models.trading import StrategyRecord

            model = BrokerAccountRecord if feature == "broker_link" else StrategyRecord
            current_count = await db.scalar(
                select(func.count(model.id)).where(model.user_id == user_id)
            ) or 0
        return await self.verify_access(user_id, feature, db, current_count or 0)

    async def verify_access(
        self,
        user_id: str,
        feature: str,
        db: AsyncSession,
        current_count: int = 0,
    ) -> dict[str, Any]:
        """Check whether user's plan allows the requested feature at the given usage count.

        Args:
            user_id: The authenticated user's ID.
            feature: One of 'broker_link', 'strategy_create', 'copy_trading'.
            db: Active async SQLAlchemy session.
            current_count: Current number of resources already owned (for count-based limits).

        Returns:
            Dict with plan name and entitlements if access is permitted.

        Raises:
            HTTPException(402): If the plan limit is exceeded. Response body includes
                                 upgrade_required=True and the required plan name.
        """
        plan_name, limits = await self.get_user_plan(user_id, db)
        limit_key = FEATURE_MAP.get(feature)

        if not limit_key:
            logger.warning("Unknown feature '%s' passed to verify_access — allowing by default", feature)
            return {"plan_name": plan_name, "limits": limits}

        label = FEATURE_LABELS.get(feature, feature)

        # Boolean feature (e.g. copy_trading_allowed)
        if limit_key == "copy_trading_allowed":
            allowed = limits.get("copy_trading_allowed", False)
            if not allowed:
                required_plan = FEATURE_UPGRADE_PLAN.get(feature, {}).get(plan_name, "PRO")
                logger.info(
                    "Access denied: user %s on %s plan attempted to use %s",
                    user_id, plan_name, label,
                )
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={
                        "message": f"Copy Trading is not available on the {PLAN_LIMITS[plan_name]['display_name']} plan.",
                        "feature": feature,
                        "upgrade_required": True,
                        "current_plan": plan_name,
                        "required_plan": required_plan,
                        "upgrade_url": "/pricing",
                    },
                )
            return {"plan_name": plan_name, "limits": limits}

        # Count-based feature (e.g. max_brokers, max_algos)
        max_allowed: int = limits.get(limit_key, 0)
        if current_count >= max_allowed:
            required_plan = FEATURE_UPGRADE_PLAN.get(feature, {}).get(plan_name, "PRO")
            logger.info(
                "Limit exceeded: user %s on %s plan has %d/%d %ss",
                user_id, plan_name, current_count, max_allowed, label,
            )
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "message": (
                        f"You have reached the maximum of {max_allowed} {label}(s) on the "
                        f"{PLAN_LIMITS[plan_name]['display_name']} plan. "
                        f"Upgrade to {required_plan or 'a higher plan'} to add more."
                    ),
                    "feature": feature,
                    "upgrade_required": True,
                    "current_plan": plan_name,
                    "current_count": current_count,
                    "max_allowed": max_allowed,
                    "required_plan": required_plan,
                    "upgrade_url": "/pricing",
                },
            )

        logger.debug(
            "Access granted: user %s on %s plan — %s (%d/%d used)",
            user_id, plan_name, label, current_count, max_allowed,
        )
        return {"plan_name": plan_name, "limits": limits}

    async def get_entitlements(
        self, user_id: str, db: AsyncSession
    ) -> dict[str, Any]:
        """Return full entitlement summary for a user (for UI display)."""
        plan_name, limits = await self.get_user_plan(user_id, db)
        return {
            "plan_name": plan_name,
            "display_name": limits["display_name"],
            "max_brokers": limits["max_brokers"],
            "max_algos": limits["max_algos"],
            "copy_trading_allowed": limits["copy_trading_allowed"],
            "tick_speed": limits.get("tick_speed", "1s"),
            "historical_candles": limits.get("historical_candles", "15m"),
            "priority_support": limits.get("priority_support", False),
            "vip_vps": limits.get("vip_vps", False),
            "api_access": limits.get("api_access", False),
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
subscription_engine = SubscriptionEngine()
