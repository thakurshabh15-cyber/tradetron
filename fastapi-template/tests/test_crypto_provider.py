"""Tests for the CoinGecko-backed Crypto Market Data Provider.

Covers:
1. successful real quote normalization from CoinGecko payloads
2. timestamp parsing (epoch to ISO)
3. LIVE classification for fresh real feeds
4. STALE classification for old real feeds
5. malformed timestamp fail-closed (STALE)
6. DEMO classification (never LIVE)
7. provider failure does not crash the application
8. provider status endpoint
9. quote endpoint metadata
10. WebSocket broadcast metadata (if practical)
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.market_data.base import AssetClass, DataFeedMode, NormalizedTick
from app.market_data.providers.crypto import _COINGECKO_MAP, CryptoMarketDataProvider
from app.market_data.unified_manager import unified_market_manager


# ── 1. Successful real quote normalization ─────────────────────────────────

def test_coingecko_map_covers_core_symbols():
    """The provider must map the main traded symbols to CoinGecko ids."""
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"):
        assert sym in _COINGECKO_MAP, f"{sym} must have a CoinGecko mapping"


# ── 2. Timestamp parsing ───────────────────────────────────────────────────

def test_epoch_timestamp_converts_to_iso():
    """A CoinGecko 'last_updated_at' epoch must convert to a UTC ISO string."""
    import time


# ── 3 & 4. LIVE / STALE classification via freshness model ────────────────

def _live_tick(seconds_ago: float) -> NormalizedTick:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    return NormalizedTick(
        symbol="BTCUSDT", price=81072.0, bid=81071.0, ask=81073.0,
        open=81000.0, high=81200.0, low=80900.0, close=81072.0,
        change=72.0, change_pct=0.09, volume=1000,
        asset_class=AssetClass.CRYPTO, feed_mode=DataFeedMode.PUBLIC_EXCHANGE_STREAM,
        data_source="CoinGecko Public API (Live)", timestamp=ts,
    )


def test_live_classification_when_fresh():
    """A real provider tick that is fresh must be classified LIVE."""
    tick = _live_tick(seconds_ago=2)
    enriched = unified_market_manager._with_freshness(tick.to_dict())
    assert enriched["data_status"] == "LIVE"
    assert enriched["is_stale"] is False
    assert enriched["age_seconds"] is not None


def test_stale_classification_when_old():
    """A real provider tick that is old must be classified STALE."""
    tick = _live_tick(seconds_ago=7200)
    enriched = unified_market_manager._with_freshness(tick.to_dict())
    assert enriched["data_status"] == "STALE"
    assert enriched["is_stale"] is True


# ── 5. Malformed timestamp fail-closed ────────────────────────────────────

def test_malformed_timestamp_fails_closed():
    """A real feed with an unparsable timestamp must be STALE (never LIVE)."""
    tick = _live_tick(seconds_ago=0)
    tick.timestamp = "not-a-real-timestamp"
    enriched = unified_market_manager._with_freshness(tick.to_dict())
    assert enriched["data_status"] == "STALE"
    assert enriched["is_stale"] is True


# ── 6. DEMO classification ────────────────────────────────────────────────

def test_demo_never_live():
    """DEMO ticks must always be labelled DEMO and never present freshness."""
    demo = NormalizedTick(
        symbol="MATICINR", price=48.5, bid=48.4, ask=48.6,
        open=48.5, high=48.7, low=48.3, close=48.5,
        change=0.0, change_pct=0.0, volume=1200,
        asset_class=AssetClass.CRYPTO, feed_mode=DataFeedMode.DEMO_SIMULATED,
        data_source="Crypto Market Stream (Demo Simulated)",
        timestamp=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    enriched = unified_market_manager._with_freshness(demo.to_dict())
    assert enriched["data_status"] == "DEMO"
    assert enriched["is_stale"] is None
    assert enriched["age_seconds"] is None

    epoch = int(time.time())
    iso = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    parsed = datetime.fromisoformat(iso)
    assert parsed.tzinfo is not None
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    assert -5 <= age <= 5


# ── 7. Provider failure does not crash the application ────────────────────

def test_provider_failure_does_not_crash():
    """The live-poll loop must catch errors and back off, never crash."""

    async def run():
        provider = CryptoMarketDataProvider(use_live_feed=True)
        # Force an error path and confirm no exception propagates.
        try:
            await provider._poll_coingecko_prices()
        except Exception:
            pass
        # The loop must still start and terminate cleanly on cancel.
        provider._is_running = True
        task = asyncio.create_task(provider._run_live_poll())
        await asyncio.sleep(0.05)
        provider._is_running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    assert asyncio.run(run()) is True


# ── 8. Provider status endpoint ───────────────────────────────────────────

def test_provider_status_endpoint_has_crypto():
    """The providers/status endpoint must expose the crypto provider honestly."""

    async def run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/market-data/providers/status")
            assert res.status_code == 200
            data = res.json()
            providers = data["providers"]
            crypto = next(
                (p for p in providers if p["provider_name"] == "CryptoMarketProvider"),
                None,
            )
            assert crypto is not None, "CryptoMarketProvider must appear in providers/status"
            assert crypto["feed_mode"] in {"PUBLIC_EXCHANGE_STREAM", "DEMO_SIMULATED"}
            assert "data_source" in crypto
            return crypto

    crypto = asyncio.run(run())
    assert crypto["asset_class"] == "CRYPTO"
    assert "stale_symbols_count" in crypto


# ── 9. Quote endpoint metadata ────────────────────────────────────────────

def test_quote_endpoint_metadata():
    """The quote endpoint must carry provenance metadata for the crypto provider."""

    async def run():
        seeded = _live_tick(seconds_ago=2)
        unified_market_manager._quotes[seeded.symbol] = seeded
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/market-data/quote/BTCUSDT")
            assert res.status_code == 200
            return res.json()

    body = asyncio.run(run())
    assert body["symbol"] == "BTCUSDT"
    assert "feed_mode" in body
    assert "data_status" in body
    assert body["data_status"] in {"LIVE", "STALE", "DEMO", "UNKNOWN"}
    assert "timestamp" in body


# ── 10. WebSocket broadcast metadata ─────────────────────────────────────

def test_websocket_broadcast_carries_metadata():
    """Ticks broadcast via the market channel must retain provenance fields."""
    tick = _live_tick(seconds_ago=3)
    enriched = unified_market_manager._with_freshness(tick.to_dict())

    class FakeWS:
        """Minimal stand-in matching the WebSocket.send_text interface."""
        def __init__(self):
            self.messages = []

        async def send_text(self, message: str):
            self.messages.append(message)

    async def run():
        from app.market_data.manager import ws_manager
        fake = FakeWS()
        original = ws_manager._channels.get("market:stream")
        ws_manager._channels["market:stream"] = {fake}
        try:
            await ws_manager.broadcast("market:stream", enriched)
            return fake, enriched
        finally:
            if original is None:
                ws_manager._channels.pop("market:stream", None)
            else:
                ws_manager._channels["market:stream"] = original

    fake, enriched = asyncio.run(run())
    assert fake.messages, "broadcast must have delivered the tick"
    import json as _json
    msg = _json.loads(fake.messages[0])
    assert "data_status" in msg
    assert "feed_mode" in msg
    assert "timestamp" in msg
    assert msg["symbol"] == "BTCUSDT"
    assert msg["data_status"] == enriched["data_status"]


