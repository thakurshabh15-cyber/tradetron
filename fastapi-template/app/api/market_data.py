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


# ── Full Instrument Master Search (NSE, BSE, NFO, MCX, Crypto, Forex) ─────────
from app.market_data.instruments import instrument_master


@router.get("/market-data/instruments/search")
async def search_instruments(
    q: str = Query("", description="Search term e.g. RELIANCE, NIFTY 24800, CRUDEOIL, GOLD, BTC"),
    exchange: Optional[str] = Query("ALL", description="Filter by exchange: ALL, NSE, BSE, NFO, MCX, BINANCE, CDS"),
    segment: Optional[str] = Query("ALL", description="Filter by segment: ALL, EQUITY, FNO, COMMODITY, FOREX, CRYPTO"),
    limit: int = Query(20, ge=1, le=100, description="Max results count"),
):
    """Search and discover real NSE/BSE Equities, F&O contracts, and MCX commodities from official master."""
    results = instrument_master.search(
        query=q,
        exchange=exchange if exchange != "ALL" else None,
        segment=segment if segment != "ALL" else None,
        limit=limit,
    )
    return {
        "query": q,
        "exchange": exchange,
        "segment": segment,
        "count": len(results),
        "instruments": results,
    }


@router.get("/market-data/instruments/categories")
async def get_instrument_categories():
    """List available market asset classes and segments."""
    return [
        {"id": "ALL", "label": "All Markets", "count": "10,000+"},
        {"id": "EQUITY", "label": "NSE/BSE Equities", "count": "2,000+"},
        {"id": "FNO", "label": "F&O Derivatives (Nifty / BankNifty / Stock Options)", "count": "5,000+"},
        {"id": "COMMODITY", "label": "MCX Commodities (Gold, Silver, Crude)", "count": "500+"},
        {"id": "FOREX", "label": "Forex & Currency (USD/INR)", "count": "20+"},
        {"id": "CRYPTO", "label": "Crypto Spot (BTC, ETH, SOL)", "count": "100+"},
    ]
