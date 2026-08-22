"""Reports and Analytics API Endpoints."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_db
from app.models.trading import TradeRecord

logger = get_logger("api.reports")
router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/performance")
async def get_performance_report(
    db: AsyncSession = Depends(get_db),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    strategy_id: Optional[str] = None,
):
    """Aggregate comprehensive trading performance metrics and strategy breakdown."""
    stmt = select(TradeRecord).order_by(TradeRecord.executed_at.desc())
    if strategy_id:
        stmt = stmt.where(TradeRecord.strategy_id == strategy_id)

    res = await db.execute(stmt)
    trades = res.scalars().all()

    total_trades = len(trades)
    closed_trades = [t for t in trades if t.pnl is not None]
    winning_trades = [t for t in closed_trades if t.pnl > 0]
    losing_trades = [t for t in closed_trades if t.pnl < 0]
    breakeven_trades = [t for t in closed_trades if t.pnl == 0]

    gross_profit = round(sum(t.pnl for t in winning_trades), 2)
    gross_loss = round(abs(sum(t.pnl for t in losing_trades)), 2)
    total_realized_pnl = round(sum(t.pnl for t in closed_trades), 2)

    total_evaluated = len(winning_trades) + len(losing_trades)
    win_rate = round((len(winning_trades) / total_evaluated * 100), 1) if total_evaluated > 0 else 0.0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 1.0)
    avg_trade_pnl = round(total_realized_pnl / len(closed_trades), 2) if closed_trades else 0.0

    largest_win = max([t.pnl for t in winning_trades], default=0.0)
    largest_loss = min([t.pnl for t in losing_trades], default=0.0)

    # Strategy breakdown
    strat_map: dict[str, dict[str, Any]] = {}
    for t in trades:
        name = t.strategy_name or "Manual / Core"
        if name not in strat_map:
            strat_map[name] = {"strategy_name": name, "trades_count": 0, "total_pnl": 0.0, "wins": 0, "losses": 0}
        strat_map[name]["trades_count"] += 1
        if t.pnl is not None:
            strat_map[name]["total_pnl"] = round(strat_map[name]["total_pnl"] + t.pnl, 2)
            if t.pnl > 0:
                strat_map[name]["wins"] += 1
            elif t.pnl < 0:
                strat_map[name]["losses"] += 1

    strategy_breakdown = []
    for s in strat_map.values():
        evaluated = s["wins"] + s["losses"]
        s["win_rate"] = round((s["wins"] / evaluated * 100), 1) if evaluated > 0 else 0.0
        strategy_breakdown.append(s)

    # Symbol breakdown
    sym_map: dict[str, dict[str, Any]] = {}
    for t in trades:
        sym = t.symbol
        if sym not in sym_map:
            sym_map[sym] = {"symbol": sym, "trades_count": 0, "total_quantity": 0, "total_pnl": 0.0}
        sym_map[sym]["trades_count"] += 1
        sym_map[sym]["total_quantity"] += t.quantity
        if t.pnl is not None:
            sym_map[sym]["total_pnl"] = round(sym_map[sym]["total_pnl"] + t.pnl, 2)

    symbol_breakdown = list(sym_map.values())

    return {
        "summary": {
            "total_trades": total_trades,
            "closed_trades": len(closed_trades),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "breakeven_trades": len(breakeven_trades),
            "win_rate_pct": win_rate,
            "total_realized_pnl": total_realized_pnl,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": profit_factor,
            "avg_trade_pnl": avg_trade_pnl,
            "largest_win": largest_win,
            "largest_loss": largest_loss,
        },
        "strategy_breakdown": strategy_breakdown,
        "symbol_breakdown": symbol_breakdown,
    }


@router.get("/trades/summary")
async def get_trades_summary(
    db: AsyncSession = Depends(get_db),
    period: Literal["daily", "weekly", "all"] = "all",
):
    """Return aggregated trade counts, volume by side, and execution velocity."""
    stmt = select(TradeRecord).order_by(TradeRecord.executed_at.desc())
    res = await db.execute(stmt)
    trades = res.scalars().all()

    buy_count = sum(1 for t in trades if t.side.upper() == "BUY")
    sell_count = sum(1 for t in trades if t.side.upper() == "SELL")
    total_volume_traded = sum(t.quantity * t.price for t in trades)
    total_shares = sum(t.quantity for t in trades)

    # Date-based grouping
    daily_groups: dict[str, dict[str, Any]] = {}
    for t in trades:
        date_key = t.executed_at.strftime("%Y-%m-%d") if t.executed_at else "Today"
        if date_key not in daily_groups:
            daily_groups[date_key] = {"date": date_key, "trades": 0, "volume": 0.0, "pnl": 0.0}
        daily_groups[date_key]["trades"] += 1
        daily_groups[date_key]["volume"] = round(daily_groups[date_key]["volume"] + (t.quantity * t.price), 2)
        if t.pnl is not None:
            daily_groups[date_key]["pnl"] = round(daily_groups[date_key]["pnl"] + t.pnl, 2)

    return {
        "total_trades": len(trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_shares": total_shares,
        "total_volume_usd": round(total_volume_traded, 2),
        "timeline": list(daily_groups.values()),
    }


@router.get("/export")
async def export_trades_report(
    db: AsyncSession = Depends(get_db),
    format: Literal["csv", "json"] = "csv",
):
    """Export complete trade log as downloadable CSV or structured JSON."""
    stmt = select(TradeRecord).order_by(TradeRecord.executed_at.desc())
    res = await db.execute(stmt)
    trades = res.scalars().all()

    if format == "json":
        return [
            {
                "trade_id": t.id,
                "order_id": t.order_id,
                "strategy_name": t.strategy_name,
                "symbol": t.symbol,
                "side": t.side,
                "quantity": t.quantity,
                "price": t.price,
                "pnl": t.pnl,
                "executed_at": t.executed_at.isoformat() if t.executed_at else None,
            }
            for t in trades
        ]

    # Generate CSV Stream
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Trade ID",
        "Order ID",
        "Strategy Name",
        "Symbol",
        "Side",
        "Quantity",
        "Execution Price ($)",
        "Realized PnL ($)",
        "Executed At (UTC)",
    ])

    for t in trades:
        writer.writerow([
            t.id,
            t.order_id or "",
            t.strategy_name or "Standard",
            t.symbol,
            t.side,
            t.quantity,
            f"{t.price:.2f}",
            f"{t.pnl:.2f}" if t.pnl is not None else "",
            t.executed_at.strftime("%Y-%m-%d %H:%M:%S") if t.executed_at else "",
        ])

    csv_content = output.getvalue()
    filename = f"tradetron_trades_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
