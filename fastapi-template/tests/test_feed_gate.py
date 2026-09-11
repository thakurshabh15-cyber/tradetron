"""Tests for the Phase 15A LIVE-execution feed gate and feed_state exposure.

Covers:
1. LIVE execution is blocked when no quote exists for the symbol (UNAVAILABLE)
2. LIVE execution is blocked when the feed is DEMO/SIMULATED
3. LIVE execution is blocked when the quote is STALE (not LIVE/fresh)
4. LIVE execution is allowed only when the quote is a fresh genuine exchange feed
5. providers/status endpoint exposes per-provider feed_state plus aggregate
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.market_data.base import AssetClass, DataFeedMode, NormalizedTick
from app.market_data.unified_manager import unified_market_manager


def _quote(feed_mode: str, timestamp: str) -> NormalizedTick:
    return NormalizedTick(
        symbol="BTCUSDT", price=64250.0, bid=64249.0, ask=64251.0,
        open=64100.0, high=64500.0, low=63900.0, close=64250.0,
        change=150.0, change_pct=0.23, volume=1000,
        asset_class=AssetClass.CRYPTO,
        feed_mode=DataFeedMode(feed_mode),
        data_source="Binance Market Streams WebSocket (Real-time)",
        timestamp=timestamp,
    )


def _fresh_ts() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()


def _stale_ts() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()


def _engine():
    from app.engine.trading_engine import TradingEngine

    class _StubBroker:
        pass

    return TradingEngine(broker=_StubBroker(), tick_queue=asyncio.Queue())


# ── 1. No quote → blocked ────────────────────────────────────────────────

def test_feed_gate_blocks_when_no_quote():
    engine = _engine()
    with patch.object(unified_market_manager, "_quotes", new={}):
        reason = engine._feed_gate_for_live("BTCUSDT")
    assert reason is not None
    assert "No market data" in reason


# ── 2. DEMO feed → blocked ───────────────────────────────────────────────

def test_feed_gate_blocks_demo_feed():
    engine = _engine()
    demo = _quote(DataFeedMode.DEMO_SIMULATED.value, _fresh_ts())
    with patch.object(unified_market_manager, "get_quote", return_value=demo.to_dict()):
        reason = engine._feed_gate_for_live("BTCUSDT")
    assert reason is not None
    assert "DEMO" in reason


# ── 3. STALE data → blocked ──────────────────────────────────────────────

def test_feed_gate_blocks_stale_quote():
    engine = _engine()
    real = _quote(DataFeedMode.PUBLIC_EXCHANGE_STREAM.value, _stale_ts())
    enriched = unified_market_manager._with_freshness(real.to_dict())
    assert enriched["data_status"] == "STALE"  # pre-condition
    with patch.object(unified_market_manager, "get_quote", return_value=enriched):
        reason = engine._feed_gate_for_live("BTCUSDT")
    assert reason is not None
    assert "STALE" in reason


# ── 4. Fresh genuine exchange feed → allowed ─────────────────────────────

def test_feed_gate_allows_fresh_live_feed():
    engine = _engine()
    real = _quote(DataFeedMode.PUBLIC_EXCHANGE_STREAM.value, _fresh_ts())
    enriched = unified_market_manager._with_freshness(real.to_dict())
    assert enriched["data_status"] == "LIVE"  # pre-condition
    with patch.object(unified_market_manager, "get_quote", return_value=enriched):
        reason = engine._feed_gate_for_live("BTCUSDT")
    assert reason is None


# ── 5. providers/status exposes feed_state ───────────────────────────────

def test_providers_status_exposes_feed_state():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    async def run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/market-data/providers/status")
            assert res.status_code == 200
            return res.json()

    body = asyncio.run(run())
    assert "feed_states" in body
    assert body["providers"]
    for provider in body["providers"]:
        assert "feed_state" in provider
        assert provider["feed_state"] in {
            "LIVE", "DELAYED", "STALE", "DEMO", "MOCK", "UNAVAILABLE", None,
        }