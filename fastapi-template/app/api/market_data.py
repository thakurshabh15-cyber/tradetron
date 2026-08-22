"""REST endpoints for Unified Market Data: Indian Equities, F&O, Crypto, Forex, and Feed Status."""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from app.core.logging import get_logger
from app.market_data.unified_manager import unified_market_manager

logger = get_logger("api.market")
router = APIRouter(prefix="/api", tags=["market-data"])


@router.get("/market-data")
async def get_market_data(
    asset_class: Optional[str] = Query("ALL", description="Filter by asset class: EQUITY, FNO, CRYPTO, FOREX, COMMODITY, ALL")
):
    """Return unified market data quotes across all or filtered asset classes."""
    quotes = unified_market_manager.get_snapshot(asset_class_filter=asset_class)
    # If unified manager is warming up, fallback to legacy simulator snapshot
    if not quotes:
        from app.main import get_simulator
        sim = get_simulator()
        if sim:
            return {"timestamp": None, "market": sim.get_snapshot()}
    return {"timestamp": None, "market": quotes}


@router.get("/market-data/quote/{symbol}")
async def get_single_quote(symbol: str):
    """Fetch real-time normalized quote for a specific ticker symbol."""
    quote = unified_market_manager.get_quote(symbol)
    if not quote:
        raise HTTPException(status_code=404, detail=f"No real-time market data available for {symbol}")
    return quote


@router.get("/market-data/candles")
@router.get("/market/candles")
async def get_historical_candles(
    symbol: str = Query("NIFTY50", description="Ticker symbol e.g. NIFTY50, RELIANCE, BTCUSDT, USDINR"),
    timeframe: str = Query("5m", description="Timeframe interval: 1m, 5m, 15m, 1h, 1d"),
    limit: int = Query(100, ge=1, le=500, description="Max candle count"),
):
    """Return authentic historical OHLCV candles from the real market data provider."""
    candles = await unified_market_manager.get_historical_candles(
        symbol=symbol, timeframe=timeframe, limit=limit
    )
    return {
        "symbol": symbol.upper().strip(),
        "timeframe": timeframe,
        "count": len(candles),
        "candles": candles,
    }


@router.get("/market-data/providers/status")
async def get_providers_status():
    """Retrieve operational status, data source vendor, and live vs demo modes for all providers."""
    return {
        "status": "HEALTHY",
        "providers": unified_market_manager.get_providers_status(),
        "disclaimer": "Free/delayed & simulated feeds active for non-credentialed providers. Real broker API keys stream live ticks.",
    }


@router.get("/risk-status")
async def risk_status():
    """Return current risk exposure snapshot."""
    from app.main import get_engine

    engine = get_engine()
    if engine:
        return engine.risk_manager.get_status()

    return {}
