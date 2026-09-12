"""Phase 1 — honest Indian-equity market data (no fabricated LIVE, fail-closed)."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from app.market_data.base import AssetClass, DataFeedMode, NormalizedTick
from app.market_data.providers import indian_equity as ie_module
from app.market_data.providers.indian_equity import IndianEquityMarketDataProvider
from app.market_data.unified_manager import unified_market_manager


def _fake_yfinance_module(prices: dict[str, list[float]]) -> SimpleNamespace:
    """Fake yfinance module serving deterministic 1-min close/volume bars."""
    idx = pd.date_range("2026-09-11 09:20:00", periods=8, freq="min", tz="Asia/Kolkata")

    def _bars(closes: list[float]) -> list[float]:
        bars = list(closes)
        if len(bars) < 8:
            bars = bars + [bars[-1]] * (8 - len(bars))
        return bars[:8]

    def _df_from_mapping() -> pd.DataFrame:
        data = {}
        for sym, closes in prices.items():
            bars = _bars(closes)
            data[("Close", sym)] = bars
            data[("Volume", sym)] = [len(bars) * 10 + i for i in range(len(bars))]
        return pd.DataFrame(data, index=idx)

    class FakeYF:
        def download(self, tickers=None, period=None, interval=None, progress=None):
            return _df_from_mapping()

    return SimpleNamespace(download=FakeYF().download)


async def _wait_for(predicate, timeout=8.0, interval=0.05) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def test_demo_mode_default_is_honest_demo():
    provider = IndianEquityMarketDataProvider()
    assert provider.feed_mode == DataFeedMode.DEMO_SIMULATED
    assert provider._real_mode == "demo"

    got = []

    async def cb(tick):
        got.append(tick)

    provider.add_callback(cb)
    await provider.subscribe(["NIFTY50", "RELIANCE"])
    await provider.start()
    try:
        ok = await _wait_for(lambda: len(got) >= 1)
        assert ok
        assert provider.classify_feed_state().value == "DEMO"
        assert got[0].feed_mode == DataFeedMode.DEMO_SIMULATED
    finally:
        await provider.stop()


async def test_live_mode_fails_closed_without_genuine_stream():
    provider = IndianEquityMarketDataProvider(
        feed_mode="live", api_key="some-key", client_code="SOME123"
    )
    assert provider.feed_mode == DataFeedMode.LIVE_BROKER_VENDOR
    assert provider._real_mode == "live"

    got = []

    async def cb(tick):
        got.append(tick)

    provider.add_callback(cb)
    await provider.subscribe(["NIFTY50"])
    await provider.start()
    assert provider.classify_feed_state().value == "UNAVAILABLE"
    assert provider.last_sync_error
    await asyncio.sleep(0.3)
    assert not got, "LIVE mode must never emit synthetic ticks"
    assert provider.get_latest_quote("NIFTY50") is None
    await provider.stop()


async def test_delayed_mode_emits_real_priced_delayed_ticks():
    provider = IndianEquityMarketDataProvider(feed_mode="delayed")
    provider._delayed_refresh_seconds = 0.2
    assert provider.feed_mode == DataFeedMode.PUBLIC_EXCHANGE_STREAM

    prices = {"^NSEI": [25000.0, 25010.0, 25005.0, 25020.0, 25015.0],
              "RELIANCE.NS": [2480.0, 2485.0, 2482.5, 2490.0, 2495.25]}
    got = {}

    async def cb(tick):
        got[tick.symbol] = tick

    provider.add_callback(cb)
    await provider.subscribe(["NIFTY50", "RELIANCE"])
    with patch.object(ie_module, "_get_yfinance", return_value=_fake_yfinance_module(prices)):
        await provider.start()
        try:
            ok = await _wait_for(lambda: "RELIANCE" in got and "NIFTY50" in got)
            assert ok
        finally:
            await provider.stop()

    rel = got["RELIANCE"]
    assert rel.price == 2495.25
    assert rel.feed_mode == DataFeedMode.PUBLIC_EXCHANGE_STREAM
    assert rel.feed_state == "DELAYED"
    assert rel.data_source == "Yahoo Finance — NSE/BSE (delayed market data)"
    assert rel.asset_class == AssetClass.EQUITY
    assert provider.classify_feed_state().value == "DELAYED"


async def test_delayed_mode_never_interpolates_synthetic_ticks():
    provider = IndianEquityMarketDataProvider(feed_mode="delayed")
    provider._delayed_refresh_seconds = 5.0
    prices = {"RELIANCE.NS": [2480.0, 2485.0]}
    counts = {"ticks": 0}

    async def cb(tick):
        counts["ticks"] += 1

    provider.add_callback(cb)
    await provider.subscribe(["RELIANCE"])
    with patch.object(ie_module, "_get_yfinance", return_value=_fake_yfinance_module(prices)):
        await provider.start()
        try:
            ok = await _wait_for(lambda: counts["ticks"] >= 1)
            assert ok
            counts["ticks"] = 0
            await asyncio.sleep(0.4)
            assert counts["ticks"] == 0
        finally:
            await provider.stop()


async def test_delayed_pipeline_failure_tracks_last_good_and_stale():
    provider = IndianEquityMarketDataProvider(feed_mode="delayed")
    provider._delayed_refresh_seconds = 0.2
    prices = {"RELIANCE.NS": [2480.0, 2485.0]}
    got = {}

    async def cb(tick):
        got[tick.symbol] = tick

    provider.add_callback(cb)
    await provider.subscribe(["RELIANCE"])
    with patch.object(ie_module, "_get_yfinance", return_value=_fake_yfinance_module(prices)):
        await provider.start()
        try:
            ok = await _wait_for(lambda: "RELIANCE" in got)
            assert ok
        finally:
            await provider.stop()

    provider.last_sync_success = datetime.now(timezone.utc) - timedelta(hours=2)
    assert provider.classify_feed_state().value == "STALE"
    assert provider.get_latest_quote("RELIANCE") is not None


def test_with_freshness_delayed_vs_unavailable():
    fresh = NormalizedTick(
        symbol="RELIANCE", price=2500.0, bid=2500.0, ask=2500.0,
        open=2480.0, high=2510.0, low=2470.0, close=2500.0,
        change=20.0, change_pct=0.8, volume=1000,
        asset_class=AssetClass.EQUITY, feed_mode=DataFeedMode.PUBLIC_EXCHANGE_STREAM,
        data_source="Yahoo Finance — NSE/BSE (delayed market data)",
        timestamp=datetime.now(timezone.utc).isoformat(), feed_state="DELAYED",
    )
    enriched = unified_market_manager._with_freshness(fresh.to_dict())
    assert enriched["data_status"] == "DELAYED"
    assert enriched["feed_state"] == "DELAYED"

    aged = NormalizedTick(
        symbol="RELIANCE", price=2500.0, bid=2500.0, ask=2500.0,
        open=2480.0, high=2510.0, low=2470.0, close=2500.0,
        change=20.0, change_pct=0.8, volume=1000,
        asset_class=AssetClass.EQUITY, feed_mode=DataFeedMode.PUBLIC_EXCHANGE_STREAM,
        data_source="Yahoo Finance — NSE/BSE (delayed market data)",
        timestamp=(datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
        feed_state="DELAYED",
    )
    enriched_aged = unified_market_manager._with_freshness(aged.to_dict())
    assert enriched_aged["data_status"] == "STALE"
    assert enriched_aged["is_stale"] is True

    unavailable = NormalizedTick(
        symbol="NIFTY50", price=25000.0, bid=25000.0, ask=25000.0,
        open=25000.0, high=25000.0, low=25000.0, close=25000.0,
        change=0.0, change_pct=0.0, volume=0,
        asset_class=AssetClass.FNO, feed_mode=DataFeedMode.LIVE_BROKER_VENDOR,
        data_source="Angel One SmartStream (broker feed)",
        timestamp=datetime.now(timezone.utc).isoformat(), feed_state="UNAVAILABLE",
    )
    enriched_unavail = unified_market_manager._with_freshness(unavailable.to_dict())
    assert enriched_unavail["data_status"] == "UNAVAILABLE"


async def test_live_legacy_auto_mode_also_fails_closed():
    provider = IndianEquityMarketDataProvider(api_key="k", client_code="c", use_live_feed=True)
    assert provider.feed_mode == DataFeedMode.LIVE_BROKER_VENDOR
    got = []

    async def cb(tick):
        got.append(tick)

    provider.add_callback(cb)
    await provider.subscribe(["NIFTY50"])
    await provider.start()
    assert provider.classify_feed_state().value == "UNAVAILABLE"
    await asyncio.sleep(0.3)
    assert not got
    await provider.stop()