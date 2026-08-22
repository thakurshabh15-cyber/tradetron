"""Trade history and statistics endpoints."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_db
from app.models.trading import TradeRecord
from app.schemas.trading import TradeRead, TradeStats

logger = get_logger("api.trades")

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("", response_model=list[TradeRead])
async def list_trades(
    symbol: str | None = Query(None, description="Filter by symbol"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Return paginated trade history, newest first."""
    stmt = select(TradeRecord).order_by(desc(TradeRecord.executed_at))

    if symbol:
        stmt = stmt.where(TradeRecord.symbol == symbol.upper())

    stmt = stmt.offset(offset).limit(limit)
    result = await db.execute(stmt)

    return [
        TradeRead(
            id=r.id,
            order_id=r.order_id,
            strategy_name=r.strategy_name,
            symbol=r.symbol,
            side=r.side,
            quantity=r.quantity,
            price=Decimal(str(r.price)),
            pnl=Decimal(str(r.pnl)) if r.pnl is not None else None,
            executed_at=r.executed_at,
        )
        for r in result.scalars().all()
    ]


@router.get("/stats", response_model=TradeStats)
async def trade_stats(db: AsyncSession = Depends(get_db)):
    """Return aggregated trade statistics."""
    total = await db.scalar(select(func.count(TradeRecord.id)))
    total = total or 0

    if total == 0:
        return TradeStats()

    result = await db.execute(select(TradeRecord))
    trades = result.scalars().all()

    pnl_total = sum(t.pnl or 0 for t in trades)
    winning = sum(1 for t in trades if (t.pnl or 0) > 0)
    losing = sum(1 for t in trades if (t.pnl or 0) < 0)

    return TradeStats(
        total_trades=total,
        winning_trades=winning,
        losing_trades=losing,
        total_pnl=Decimal(str(round(pnl_total, 2))),
        win_rate=round(winning / total * 100, 1) if total else 0,
    )


# ── Manual Order Placement with KYC Check ─────────────────────────────────────
from datetime import datetime, timezone
import uuid
from pydantic import BaseModel, Field
from typing import Optional, Literal
from fastapi import HTTPException
from app.api.auth import get_current_user
from app.models.user import UserRecord


class ManualOrderRequest(BaseModel):
    symbol: str = Field(..., description="NSE/Crypto ticker symbol e.g. NIFTY50, RELIANCE, BTCUSDT")
    side: Literal["BUY", "SELL"]
    quantity: int = Field(..., gt=0)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    price: Optional[float] = None
    mode: Literal["PAPER", "LIVE"] = "PAPER"


@router.post("/order")
async def place_manual_order(
    req: ManualOrderRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Place manual DMA order from Fast Order Panel with strict KYC verification gate for LIVE mode."""
    if req.mode == "LIVE":
        if user.kyc_status != "VERIFIED":
            raise HTTPException(
                status_code=403,
                detail="KYC Verification Required: SEBI regulatory compliance mandates that your KYC status must be VERIFIED before placing live real-money orders.",
            )

    executed_price = req.price or (24850.0 if "NIFTY" in req.symbol else 2950.0)

    trade = TradeRecord(
        id=str(uuid.uuid4()),
        order_id=f"ORD_{int(datetime.now(timezone.utc).timestamp())}",
        strategy_name="Manual Fast Order",
        symbol=req.symbol.upper(),
        side=req.side,
        quantity=req.quantity,
        price=executed_price,
        entry_price=executed_price,
        pnl=0.0,
        mode=req.mode,
        user_id=user.id,
    )
    db.add(trade)
    await db.commit()
    await db.refresh(trade)

    from app.core.audit import log_audit_event
    await log_audit_event(
        db=db,
        action="MANUAL_ORDER_PLACED",
        resource_type="ORDER",
        user_id=user.id,
        status="EXECUTED",
        details={
            "symbol": req.symbol,
            "side": req.side,
            "quantity": req.quantity,
            "price": executed_price,
            "mode": req.mode,
        },
    )

    return {
        "success": True,
        "order_id": trade.order_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "price": trade.price,
        "mode": trade.mode,
        "status": "FILLED",
        "executed_at": trade.executed_at.isoformat() if trade.executed_at else datetime.now(timezone.utc).isoformat(),
    }
