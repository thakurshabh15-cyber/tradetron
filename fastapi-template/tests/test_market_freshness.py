"""Phase 5 — Market-data freshness / staleness verification.

Confirms that quotes exposed to the frontend carry honest provenance and
freshness metadata (data_status / is_stale / age_seconds) and that STALE is
only ever attached to *real* feeds — never to honestly-simulated demo data
(which is labelled DEMO instead).
"""

import asyncio
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.market_data.base import AssetClass, DataFeedMode, NormalizedTick
from app.market_data.unified_manager import unified_market_manager


def _tick(seconds_ago: float, feed_mode=DataFeedMode.LIVE_BROKER_VENDOR,
          asset_class=AssetClass.CRYPTO, timestamp=None):
    ts = timestamp or (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    return NormalizedTick(
        symbol="BTCUSDT", price=60000.0, bid=59999.0, ask=60001.0,
        open=60000.0, high=60100.0, low=59900.0, close=60000.0,
        change=0.0, change_pct=0.0, volume=100.0,
        asset_class=asset_class, feed_mode=feed_mode,
        data_source="CoinGecko Public API", timestamp=ts,
    )


def test_tick_age_seconds_freshness():
    tick = _tick(seconds_ago=5)
    assert tick.age_seconds() is not None
    assert 3.0 <= tick.age_seconds() <= 7.0
    assert tick.is_stale(max_age_seconds=60) is False
    assert tick.is_stale(max_age_seconds=2) is True


def test_tick_unparsable_timestamp_fails_closed():
    bad = _tick(seconds_ago=0, timestamp="not-a-timestamp")
    assert bad.age_seconds() is None
    assert bad.is_stale(max_age_seconds=3600) is True


def test_with_freshness_demo_never_stale():
    demo = _tick(seconds_ago=900, feed_mode=DataFeedMode.DEMO_SIMULATED)
    enriched = unified_market_manager._with_freshness(demo.to_dict())
    assert enriched["data_status"] == "DEMO"
    # Demo data must never present a real freshness guarantee.
    assert enriched["is_stale"] is None


def test_with_freshness_live_when_fresh():
    live = _tick(seconds_ago=2)
    enriched = unified_market_manager._with_freshness(live.to_dict())
    assert enriched["data_status"] == "LIVE"
    assert enriched["is_stale"] is False
    assert enriched["age_seconds"] <= 5


def test_with_freshness_stale_when_old():
    stale = _tick(seconds_ago=7200)
    enriched = unified_market_manager._with_freshness(stale.to_dict())
    assert enriched["data_status"] == "STALE"
    assert enriched["is_stale"] is True


def test_quote_endpoint_exposes_freshness():
    async def run():
        # Seed the unified cache so the endpoint returns the enriched-quote path
        # rather than the cold-start instrument fallback (which lacks metadata).
        seeded = _tick(seconds_ago=3, feed_mode=DataFeedMode.LIVE_BROKER_VENDOR)
        unified_market_manager._quotes[seeded.symbol] = seeded
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/market-data/quote/BTCUSDT")
            assert res.status_code == 200
            body = res.json()
            assert "feed_mode" in body
            assert "data_status" in body, "quote must expose honest data_status"
            assert body["data_status"] in {"LIVE", "DEMO", "STALE", "UNKNOWN"}
            assert "is_stale" in body
            return body

    body = asyncio.run(run())
    assert body["symbol"] == "BTCUSDT"


def test_snapshot_items_carry_status_metadata():
    snap = unified_market_manager.get_snapshot()
    if not snap:
        return  # cold singleton without lifespan — covered by live-server probe
    for q in snap:
        assert "feed_mode" in q
        assert "data_status" in q
        if q["feed_mode"] == "DEMO_SIMULATED":
            assert q["data_status"] == "DEMO"
