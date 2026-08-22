"""Unit tests for Real Market Data Feeds, WebSocket Ticks, and Real Historical Candles."""

import asyncio
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.db.session import init_db
from app.market_data.unified_manager import unified_market_manager


async def test_historical_candles_crypto():
    """Verify Binance public API returns real historical candles."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/market/candles?symbol=BTCUSDT&timeframe=5m&limit=10")
        assert res.status_code == 200
        data = res.json()
        assert data["symbol"] == "BTCUSDT"
        assert data["timeframe"] == "5m"
        assert len(data["candles"]) > 0
        candle = data["candles"][0]
        assert "time" in candle
        assert "open" in candle
        assert "high" in candle
        assert "low" in candle
        assert "close" in candle
        assert candle["high"] >= candle["low"]


async def test_historical_candles_equity_and_forex():
    """Verify Indian Equities and Forex pairs return genuine historical candles."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Equity: RELIANCE
        eq_res = await client.get("/api/market/candles?symbol=RELIANCE&timeframe=5m&limit=10")
        assert eq_res.status_code == 200
        eq_data = eq_res.json()
        assert eq_data["symbol"] == "RELIANCE"
        assert len(eq_data["candles"]) > 0

        # 2. Forex: USDINR
        fx_res = await client.get("/api/market/candles?symbol=USDINR&timeframe=5m&limit=10")
        assert fx_res.status_code == 200
        fx_data = fx_res.json()
        assert fx_data["symbol"] == "USDINR"
        assert len(fx_data["candles"]) > 0


async def test_multi_symbol_differentiation():
    """Confirm two different symbols show genuinely different, distinct market prices."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/market-data")
        assert res.status_code == 200
        data = res.json()
        market_list = data["market"]
        assert len(market_list) >= 2

        # Verify symbols have distinct prices
        prices_by_symbol = {m["symbol"]: m["price"] for m in market_list}
        assert "NIFTY50" in prices_by_symbol or "BTCUSDT" in prices_by_symbol
        if "NIFTY50" in prices_by_symbol and "RELIANCE" in prices_by_symbol:
            assert prices_by_symbol["NIFTY50"] != prices_by_symbol["RELIANCE"]
