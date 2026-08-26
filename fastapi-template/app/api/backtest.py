"""TradeThrone Backtesting API.

Endpoints
---------
POST /api/backtest/run            — full truthful backtest report
POST /api/strategies/backtest     — legacy contract used by the web store
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.engine.backtester import run_backtest

router = APIRouter(tags=["backtest"])


class BacktestRequest(BaseModel):
    """Superset payload accepted by both routes."""

    symbols: list[str] | None = Field(None, description="Symbols to test")
    symbol: str | None = Field(None, description="Single-symbol convenience field")
    conditions: list[dict] = Field(..., min_length=1,
                                   description="[{indicator, operator, value, period}]")
    side: str = Field("BUY", pattern="^(BUY|SELL|buy|sell)$")
    quantity: int = Field(1, ge=1)
    timeframe: str = Field("5m", description="1m | 5m | 15m | 30m | 1h | 1d")
    days: int = Field(30, ge=1, le=365)
    capital: float = Field(100_000.0, gt=0)
    product_type: str = Field("INTRADAY", description="INTRADAY | DELIVERY/CNC")
    stop_loss_pct: float | None = Field(None, gt=0, le=100)
    take_profit_pct: float | None = Field(None, gt=0, le=100)
    slippage_pct: float | None = Field(None, gt=0, le=5,
                                       description="Override default 0.1% slippage cost")
    seed: int | None = Field(None, description="Reproduce a prior run exactly")


def _resolve_symbol_list(req: BacktestRequest) -> list[str]:
    symbols = req.symbols or ([req.symbol] if req.symbol else [])
    if not symbols:
        raise HTTPException(422, detail="Provide 'symbols' or 'symbol'")
    return [s.strip().upper() for s in symbols if s and s.strip()]


@router.post("/api/backtest/run", summary="Run a truthful, charge-aware backtest")
async def run_backtest_endpoint(req: BacktestRequest) -> dict:
    try:
        reports = []
        for sym in _resolve_symbol_list(req):
            reports.append(run_backtest(
                symbol=sym,
                conditions=req.conditions,
                side=req.side.upper(),
                quantity=req.quantity,
                timeframe=req.timeframe,
                days=req.days,
                capital=req.capital,
                product_type=req.product_type,
                stop_loss_pct=req.stop_loss_pct,
                take_profit_pct=req.take_profit_pct,
                slippage_pct=req.slippage_pct,
                seed=req.seed,
            ))
        # Single-symbol calls keep the historical flat shape
        if len(reports) == 1:
            return reports[0]
        return {"engine": "TradeThrone Truthful Backtester v1",
                "reports": reports}
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))
    except Exception as exc:  # pragma: no cover
        raise HTTPException(500, detail=f"Backtest failed: {exc}")


@router.post("/api/strategies/backtest",
             summary="Legacy alias used by the TradeThrone web store",
             description="Delegates to /api/backtest/run; accepts an optional "
                         "'strategy' object carrying conditions/side/risk.")
async def strategies_backtest_alias(req: dict) -> dict:
    try:
        strategy = req.get("strategy") or {}
        risk = strategy.get("risk") or {}
        action = strategy.get("action") or {}
        payload = BacktestRequest(
            symbols=req.get("symbols") or (
                [req["symbol"]] if req.get("symbol") else strategy.get("symbols")
            ),
            symbol=req.get("symbol"),
            conditions=req.get("conditions") or strategy.get("conditions") or [],
            side=req.get("side") or action.get("side") or "BUY",
            quantity=req.get("quantity") or action.get("quantity") or 1,
            timeframe=req.get("timeframe") or strategy.get("timeframe") or "5m",
            days=req.get("days", 30),
            capital=req.get("capital", 100_000.0),
            product_type=req.get("product_type", "INTRADAY"),
            stop_loss_pct=req.get("stop_loss_pct") or risk.get("stop_loss_pct"),
            take_profit_pct=req.get("take_profit_pct") or risk.get("take_profit_pct"),
            slippage_pct=req.get("slippage_pct"),
            seed=req.get("seed"),
        )
    except Exception as exc:
        raise HTTPException(422, detail=f"Invalid backtest payload: {exc}")
    return await run_backtest_endpoint(payload)