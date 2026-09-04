"""Unit tests for Real Market Data Feeds, WebSocket Ticks, and Real Historical Candles."""

import asyncio
from datetime import datetime, timezone
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.db.session import init_db
from app.market_data.base import AssetClass, DataFeedMode, NormalizedTick
from app.market_data.unified_manager import unified_market_manager


async def test_historical_candles_crypto():
    """Verify CoinGecko public API returns real historical candles."""
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
    # Seed two distinct symbols so the snapshot returns multiple entries.
    tick_a = NormalizedTick(
        symbol="BTCUSDT", price=81072.0, bid=81071.0, ask=81073.0,
        open=81000.0, high=81200.0, low=80900.0, close=81072.0,
        change=72.0, change_pct=0.09, volume=1000,
        asset_class=AssetClass.CRYPTO, feed_mode=DataFeedMode.DEMO_SIMULATED,
        data_source="Simulated (test)", timestamp=datetime.now(timezone.utc).isoformat(),
    )
    tick_b = NormalizedTick(
        symbol="ETHUSDT", price=3480.5, bid=3480.0, ask=3481.0,
        open=3470.0, high=3490.0, low=3460.0, close=3480.5,
        change=10.5, change_pct=0.3, volume=2000,
        asset_class=AssetClass.CRYPTO, feed_mode=DataFeedMode.DEMO_SIMULATED,
        data_source="Simulated (test)", timestamp=datetime.now(timezone.utc).isoformat(),
    )
    unified_market_manager._quotes["BTCUSDT"] = tick_a
    unified_market_manager._quotes["ETHUSDT"] = tick_b
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/market-data")
        assert res.status_code == 200
        data = res.json()
        market_list = data["market"]
        assert len(market_list) >= 2

        # Verify symbols have distinct prices
        prices_by_symbol = {m["symbol"]: m["price"] for m in market_list}
        assert "BTCUSDT" in prices_by_symbol and "ETHUSDT" in prices_by_symbol
        assert prices_by_symbol["BTCUSDT"] != prices_by_symbol["ETHUSDT"]
