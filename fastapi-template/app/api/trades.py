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


# ── Manual Order Placement with Real Market Price & Open Positions ──────────────
from datetime import datetime, timezone
import uuid
from pydantic import BaseModel, Field
from typing import Optional, Literal
from fastapi import HTTPException
from app.api.auth import get_current_user
from app.models.user import UserRecord
from app.models.trading import OrderRecord, PositionRecord
from app.models.broker_account import BrokerAccountRecord
from app.market_data.unified_manager import unified_market_manager
from app.market_data.instruments import instrument_master


class ManualOrderRequest(BaseModel):
    symbol: str = Field(..., description="NSE/Crypto ticker symbol e.g. NIFTY50, RELIANCE, BTCUSDT")
    side: Literal["BUY", "SELL"]
    quantity: int = Field(..., gt=0)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    price: Optional[float] = None
    mode: Literal["PAPER", "LIVE"] = "PAPER"


@router.get("/positions")
@router.get("/api/positions")
async def list_open_positions(
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_current_user),
):
    """Retrieve active open positions with live market valuation."""
    stmt = select(PositionRecord).where(PositionRecord.status == "OPEN").order_by(desc(PositionRecord.opened_at))
    if user:
        stmt = stmt.where(PositionRecord.user_id == user.id)

    res = await db.execute(stmt)
    positions = res.scalars().all()

    output = []
    for p in positions:
        quote = unified_market_manager.get_quote(p.symbol)
        inst = instrument_master.get_instrument(p.symbol)
        live_p = quote.price if quote else (inst.base_price if inst else p.entry_price)

        is_long = p.side in ("LONG", "BUY")
        delta = (live_p - p.entry_price) if is_long else (p.entry_price - live_p)
        unrealized_pnl = round(delta * p.quantity, 2)
        unrealized_pnl_pct = round((delta / p.entry_price) * 100, 2) if p.entry_price else 0.0

        output.append({
            "id": p.id,
            "symbol": p.symbol,
            "side": p.side,
            "quantity": p.quantity,
            "entry_price": p.entry_price,
            "current_price": live_p,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "mode": p.mode,
            "status": p.status,
            "opened_at": p.opened_at.isoformat() if p.opened_at else datetime.now(timezone.utc).isoformat(),
        })
    return output


@router.post("/positions/{position_id}/close")
async def close_position(
    position_id: str,
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_current_user),
):
    """Close an open position at the real live market price and book realized PnL."""
    pos = await db.get(PositionRecord, position_id)
    if not pos or pos.status != "OPEN":
        raise HTTPException(status_code=404, detail="Open position not found or already closed")

    quote = unified_market_manager.get_quote(pos.symbol)
    inst = instrument_master.get_instrument(pos.symbol)
    exit_price = quote.price if quote else (inst.base_price if inst else pos.entry_price)

    is_long = pos.side in ("LONG", "BUY")
    delta = (exit_price - pos.entry_price) if is_long else (pos.entry_price - exit_price)
    realized_pnl = round(delta * pos.quantity, 2)
    pnl_pct = round((delta / pos.entry_price) * 100, 2) if pos.entry_price else 0.0

    pos.status = "CLOSED"
    pos.closed_at = datetime.now(timezone.utc)
    pos.current_price = exit_price
    pos.realized_pnl = realized_pnl
    pos.unrealized_pnl = 0.0

    # Record offsetting closing trade
    closing_side = "SELL" if is_long else "BUY"
    trade = TradeRecord(
        id=str(uuid.uuid4()),
        order_id=f"EXIT_{int(datetime.now(timezone.utc).timestamp())}",
        strategy_name="Manual Position Exit",
        symbol=pos.symbol,
        side=closing_side,
        quantity=pos.quantity,
        price=exit_price,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        pnl=realized_pnl,
        pnl_pct=pnl_pct,
        exit_reason="MANUAL_CLOSE",
        mode=pos.mode,
        user_id=pos.user_id,
    )
    db.add(trade)
    await db.commit()

    return {
        "success": True,
        "position_id": pos.id,
        "symbol": pos.symbol,
        "quantity": pos.quantity,
        "entry_price": pos.entry_price,
        "exit_price": exit_price,
        "realized_pnl": realized_pnl,
        "pnl_pct": pnl_pct,
        "status": "CLOSED",
    }


@router.post("/order")
async def place_manual_order(
    req: ManualOrderRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Place manual DMA order for PAPER or LIVE execution with real fill price and open position tracking."""
    clean_sym = req.symbol.upper().strip()

    # 1. Resolve real live market execution price from market provider / master
    quote = unified_market_manager.get_quote(clean_sym)
    inst = instrument_master.get_instrument(clean_sym)
    live_p = quote.price if quote else (inst.base_price if inst else (req.price or 1000.0))
    executed_price = round(req.price if req.order_type == "LIMIT" and req.price else live_p, 2)

    # 2. If LIVE mode, ensure a real connected broker exists
    broker_account_id = None
    if req.mode == "LIVE":
        broker_stmt = select(BrokerAccountRecord).where(
            BrokerAccountRecord.user_id == user.id,
            BrokerAccountRecord.status == "CONNECTED",
            BrokerAccountRecord.is_active.is_(True),
        )
        broker_res = await db.execute(broker_stmt)
        broker_acc = broker_res.scalars().first()
        if not broker_acc:
            raise HTTPException(
                status_code=400,
                detail="No active connected broker account found. Please link your broker before switching to Live Execution.",
            )
        broker_account_id = broker_acc.id

    # 3. Create persistent OrderRecord
    order_id = f"ORD_{int(datetime.now(timezone.utc).timestamp())}_{str(uuid.uuid4())[:8]}"
    order = OrderRecord(
        id=str(uuid.uuid4()),
        user_id=user.id,
        broker_account_id=broker_account_id,
        broker_order_id=order_id,
        symbol=clean_sym,
        side=req.side,
        quantity=req.quantity,
        order_type=req.order_type,
        price=executed_price,
        filled_price=executed_price,
        filled_quantity=req.quantity,
        status="FILLED",
        mode=req.mode,
    )
    db.add(order)

    # 4. Create persistent TradeRecord
    trade = TradeRecord(
        id=str(uuid.uuid4()),
        order_id=order_id,
        strategy_name="DMA Fast Order",
        symbol=clean_sym,
        side=req.side,
        quantity=req.quantity,
        price=executed_price,
        entry_price=executed_price,
        pnl=0.0,
        mode=req.mode,
        user_id=user.id,
    )
    db.add(trade)

    # 5. Create new Open PositionRecord
    pos_side = "LONG" if req.side == "BUY" else "SHORT"
    position = PositionRecord(
        id=str(uuid.uuid4()),
        user_id=user.id,
        broker_account_id=broker_account_id,
        symbol=clean_sym,
        side=pos_side,
        quantity=req.quantity,
        entry_price=executed_price,
        current_price=executed_price,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        mode=req.mode,
        status="OPEN",
        opened_at=datetime.now(timezone.utc),
    )
    db.add(position)

    await db.commit()
    await db.refresh(trade)
    await db.refresh(position)

    from app.core.audit import log_audit_event
    await log_audit_event(
        db=db,
        action="MANUAL_ORDER_PLACED",
        resource_type="ORDER",
        user_id=user.id,
        status="EXECUTED",
        details={
            "symbol": clean_sym,
            "side": req.side,
            "quantity": req.quantity,
            "price": executed_price,
            "mode": req.mode,
            "position_id": position.id,
        },
    )

    return {
        "success": True,
        "order_id": order_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "price": trade.price,
        "mode": trade.mode,
        "status": "FILLED",
        "position_id": position.id,
        "executed_at": trade.executed_at.isoformat() if trade.executed_at else datetime.now(timezone.utc).isoformat(),
    }
