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
