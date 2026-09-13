"""Admin-gated HTTP surface for governed agent trading intents (Phase 1 Step 3).

Every mutation funnels through ``app.engine.agent_intents.AgentTradingService``
so the deterministic decision contract and the fail-closed gates (feed
freshness, broker-mode, broker truth, approval window, margin, risk) can never
be bypassed.  All routes require an authenticated administrator
(``get_current_admin_user``) — intent rows carry their OWNING user explicitly.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.admin import get_current_admin_user
from app.engine.agent_intents import (
    DECISION_NO_TRADE,
    DECISION_REJECTED,
    DECISION_TRADE,
    DECISION_NEEDS_APPROVAL,
    DECISION_FAILED,
    IntentGateError,
    agent_trading_service,
)
from app.models.user import UserRecord

router = APIRouter(prefix="/api/agents/intents", tags=["agents"])

_DECISIONS = {
    DECISION_NO_TRADE,
    DECISION_TRADE,
    DECISION_NEEDS_APPROVAL,
    DECISION_REJECTED,
    DECISION_FAILED,
}


class IntentEvaluateRequest(BaseModel):
    """Structured decision envelope produced by the agent runtime."""

    agent_task_id: Optional[str] = Field(default=None, max_length=64)
    user_id: str = Field(..., max_length=64)
    broker_account_id: str = Field(..., max_length=64)
    strategy_id: Optional[str] = Field(default=None, max_length=64)
    symbol: str = Field(..., min_length=1, max_length=30)
    side: str = Field(...)
    quantity: int = Field(..., gt=0)
    order_type: str = "MARKET"
    limit_price: Optional[float] = Field(default=None, gt=0)
    trigger_price: Optional[float] = Field(default=None, gt=0)
    stop_loss_price: Optional[float] = Field(default=None, gt=0)
    take_profit_price: Optional[float] = Field(default=None, gt=0)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    reason: str = Field(default="", max_length=1000)
    requested_mode: str = "PAPER"  # PAPER | DEMO | LIVE
    decision: str = DECISION_TRADE
    approval_required: bool = False


class IntentCloseRequest(BaseModel):
    position_id: str = Field(..., max_length=36)


class IntentExecuteResponse(BaseModel):
    ok: bool
    intent_id: Optional[str] = None
    reason: Optional[str] = None
    idempotent: bool = False
    order_id: Optional[str] = None
    position_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    filled_price: Optional[float] = None
    protection_state: Optional[str] = None


@router.post("", status_code=201)
async def evaluate_intent(
    req: IntentEvaluateRequest,
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Run every gate and persist a durable intent for TRADE /
    NEEDS_APPROVAL decisions.  All other decisions journal + return idempotent
    ``intent_id=None`` — NEVER silent and NEVER a PAPER fallback for LIVE."""
    try:
        return await agent_trading_service.evaluate(
            agent_task_id=req.agent_task_id,
            user_id=req.user_id,
            broker_account_id=req.broker_account_id,
            strategy_id=req.strategy_id,
            symbol=req.symbol.strip().upper(),
            side=req.side.upper(),
            quantity=int(req.quantity),
            order_type=req.order_type.upper(),
            limit_price=req.limit_price,
            trigger_price=req.trigger_price,
            stop_loss_price=req.stop_loss_price,
            take_profit_price=req.take_profit_price,
            confidence=req.confidence,
            reason=req.reason,
            requested_mode=req.requested_mode.upper(),
            decision=req.decision.upper(),
            approval_required=req.approval_required,
        )
    except IntentGateError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.post("/{intent_id}/execute")
async def execute_intent(
    intent_id: str,
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Idempotent governed execution of a durable intent.

    Replays observe ``ok=True, idempotent=True`` and never double-place.
    """
    outcome = await agent_trading_service.execute_intent(
        intent_id,
        authorized_user_id=admin.id,
    )
    if not outcome.get("ok") and outcome.get("reason") is None:
        # Only a truly unknown intent is a 404; gate/margin rejections are
        # legitimate deterministic results with persisted reasons.
        raise HTTPException(status_code=404, detail="intent not found")
    return outcome


@router.post("/{intent_id}/close")
async def close_intent_position(
    intent_id: str,
    req: IntentCloseRequest,
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Canonical CAS close of the position booked by an intent."""
    outcome = await agent_trading_service.close_position(
        intent_id,
        req.position_id,
        authorized_user_id=admin.id,
    )
    if not outcome.get("ok") and outcome.get("idempotent") is False:
        raise HTTPException(status_code=409, detail=outcome.get("reason"))
    return outcome


@router.get("/{intent_id}")
async def get_intent(
    intent_id: str,
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    intent = await agent_trading_service.get_intent(intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="intent not found")
    return intent


@router.get("")
async def list_intents(
    user_id: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    admin: UserRecord = Depends(get_current_admin_user),
) -> list[dict[str, Any]]:
    return await agent_trading_service.list_intents(user_id, limit=limit)