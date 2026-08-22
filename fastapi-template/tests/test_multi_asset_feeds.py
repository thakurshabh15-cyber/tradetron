"""Unit tests for Multi-Asset Market Data Feeds (Indian Equities, F&O, Crypto, Forex) & Unified Hub."""

import asyncio
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.market_data.base import AssetClass, DataFeedMode
from app.market_data.unified_manager import unified_market_manager
from app.market_data.providers.indian_equity import IndianEquityMarketDataProvider
from app.market_data.providers.crypto import CryptoMarketDataProvider
from app.market_data.providers.forex import ForexMarketDataProvider


async def test_multi_asset_feeds_suite():
    # 1. Test Indian Equity & F&O Provider
    indian_prov = IndianEquityMarketDataProvider()
    await indian_prov.subscribe(["NIFTY50", "RELIANCE"])
    await indian_prov.start()
    await asyncio.sleep(1.2)  # Allow tick generation

    nifty_quote = indian_prov.get_latest_quote("NIFTY50")
    assert nifty_quote is not None
    assert nifty_quote.symbol == "NIFTY50"
    assert nifty_quote.price > 20000.0
    assert nifty_quote.bid < nifty_quote.ask
    assert nifty_quote.asset_class in (AssetClass.FNO, AssetClass.EQUITY)
    await indian_prov.stop()

    # 2. Test Crypto Provider
    crypto_prov = CryptoMarketDataProvider()
    await crypto_prov.subscribe(["BTCUSDT", "ETHUSDT"])
    await crypto_prov.start()
    await asyncio.sleep(1.2)

    btc_quote = crypto_prov.get_latest_quote("BTCUSDT")
    assert btc_quote is not None
    assert btc_quote.symbol == "BTCUSDT"
    assert btc_quote.price > 10000.0
    assert btc_quote.asset_class == AssetClass.CRYPTO
    await crypto_prov.stop()

    # 3. Test Forex Provider
    forex_prov = ForexMarketDataProvider()
    await forex_prov.subscribe(["USDINR", "EURUSD"])
    await forex_prov.start()
    await asyncio.sleep(1.2)

    usdinr_quote = forex_prov.get_latest_quote("USDINR")
    assert usdinr_quote is not None
    assert usdinr_quote.symbol == "USDINR"
    assert 70.0 < usdinr_quote.price < 100.0
    assert usdinr_quote.asset_class == AssetClass.FOREX
    await forex_prov.stop()

    # 4. Test Unified Market Data Hub Orchestrator
    await unified_market_manager.start([
        "BANKNIFTY", "INFY", "BTCUSDT", "USDINR", "GOLD"
    ])
    await asyncio.sleep(1.2)

    assert unified_market_manager.classify_symbol("BANKNIFTY") == AssetClass.FNO
    assert unified_market_manager.classify_symbol("INFY") == AssetClass.EQUITY
    assert unified_market_manager.classify_symbol("BTCUSDT") == AssetClass.CRYPTO
    assert unified_market_manager.classify_symbol("USDINR") == AssetClass.FOREX
    assert unified_market_manager.classify_symbol("GOLD") == AssetClass.COMMODITY

    banknifty = unified_market_manager.get_quote("BANKNIFTY")
    assert banknifty is not None
    assert banknifty["symbol"] == "BANKNIFTY"

    # Test asset class filtering snapshot
    crypto_snapshots = unified_market_manager.get_snapshot("CRYPTO")
    assert all(item["asset_class"] == "CRYPTO" for item in crypto_snapshots)

    # 5. Test REST Endpoints via AsyncClient
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET /api/market-data
        res = await client.get("/api/market-data?asset_class=ALL")
        assert res.status_code == 200
        data = res.json()
        assert "market" in data
        assert len(data["market"]) >= 1

        # GET /api/market-data/quote/{symbol}
        q_res = await client.get("/api/market-data/quote/BTCUSDT")
        assert q_res.status_code == 200
        assert q_res.json()["symbol"] == "BTCUSDT"

        # GET /api/market-data/providers/status
        status_res = await client.get("/api/market-data/providers/status")
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert status_data["status"] == "HEALTHY"
        assert len(status_data["providers"]) >= 3

    await unified_market_manager.stop()
