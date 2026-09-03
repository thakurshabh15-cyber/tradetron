"""TradeThrone Risk Guard API — Auto-Pilot Kill-Switch control plane.

Exposes live auto-pilot status, runtime threshold tuning and operator
reset of the platform-wide trading kill-switch.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.admin import get_current_admin_user
from app.api.auth import get_current_user
from app.core.logging import get_logger
from app.models.user import UserRecord

logger = get_logger("api.risk_guard")

router = APIRouter(prefix="/api/risk-guard", tags=["risk-guard"])


class AutopilotConfigRequest(BaseModel):
    enabled: bool | None = Field(None, description="Master arm/disarm of the auto-pilot guard")
    max_consecutive_losses: int | None = Field(
        None, ge=0, le=100, description="Halt after N straight losing trades (0 disables rule)"
    )
    max_daily_drawdown_pct: float | None = Field(
        None, ge=0, le=100, description="Halt when intraday giveback from peak P&L hits this %%"
    )


def _get_risk_manager():
    """Fetch the running engine's risk manager without hard import cycles."""
    from app.main import get_engine

    engine = get_engine()
    rm = getattr(engine, "risk_manager", None) if engine else None
    if rm is None:
        raise HTTPException(
            status_code=503,
            detail="Trading engine not running — risk guard unavailable",
        )
    return rm


@router.get("/status", summary="Live auto-pilot kill-switch status")
async def risk_guard_status() -> dict:
    """Return the full auto-pilot snapshot: thresholds, streaks, events."""
    rm = _get_risk_manager()
    status = rm.get_autopilot_status()
    try:
        risk_snapshot = rm.get_status()
        status["risk_snapshot"] = {
            "daily_pnl": str(getattr(risk_snapshot, "daily_pnl", "")),
            "max_daily_loss": str(getattr(risk_snapshot, "max_daily_loss", "")),
            "open_positions": getattr(risk_snapshot, "open_positions", 0),
            "orders_this_minute": getattr(risk_snapshot, "orders_this_minute", 0),
            "circuit_breaker_active": getattr(risk_snapshot, "circuit_breaker_active", False),
        }
    except Exception as exc:  # pragma: no cover - snapshot is best-effort
        logger.debug("Risk snapshot unavailable: %s", exc)
    return status


@router.post("/config", summary="Tune auto-pilot thresholds (admin only)")
async def configure_risk_guard(
    req: AutopilotConfigRequest,
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict:
    """Update consecutive-loss / drawdown limits at runtime.

    The auto-pilot thresholds are engine-global (they gate the shared trading
    engine's risk manager), so tuning them is an operator action.
    """
    rm = _get_risk_manager()
    try:
        res = rm.configure_autopilot(
            enabled=req.enabled,
            max_consecutive_losses=req.max_consecutive_losses,
            max_daily_drawdown_pct=req.max_daily_drawdown_pct,
        )
        logger.info("Risk-guard configured by admin %s: %s", admin.email, req)
        return res
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/reset", summary="Release kill-switch & clear streaks (admin only)")
async def reset_risk_guard(
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict:
    """Operator override: release an auto/manual kill-switch and restart counters.

    Releasing the platform-wide kill-switch re-enables live strategy execution,
    so only an administrator may perform it.
    """
    rm = _get_risk_manager()
    logger.warning("Risk-guard RESET issued via API by admin %s — kill-switch released", admin.email)
    return rm.reset_autopilot()