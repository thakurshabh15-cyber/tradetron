"""Tests for the Binance WebSocket-backed crypto streaming provider (Phase 15A).

Covers:
1. Binance @ticker frame -> NormalizedTick normalization (bid/ask/OHLC from vendor)
2. Binance @trade (aggTrade) updates last-trade and merges into the quote
3. Invalid / non-finite prices are NEVER emitted (fail-closed)
4. FeedState LIVE classification only when stream OPEN + fresh + quotes exist
5. FeedState STALE when the stream is open but data has aged out
6. FeedState DEMO for the simulated mode (never LIVE)
7. FeedState UNAVAILABLE when not running / stream closed / no quotes
8. subscribe() issues a Binance SUBSCRIBE wire message (both @trade and @ticker)
9. reconnect (_resubscribe_all) restores the full subscription set
10. demo stream emits DEMO_SIMULATED ticks honestly
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.market_data.base import DataFeedMode
from app.market_data.providers.crypto_stream import CryptoStreamMarketDataProvider
from app.market_data.streaming.feed_state import FeedState
from app.market_data.streaming.ws_stream import StreamStatus


# ── Fixtures / helpers ───────────────────────────────────────────────────

class FakeStream:
    """Minimal stand-in for WebSocketStream exposing the surface the provider uses."""

    def __init__(self, status=StreamStatus.OPEN):
        self._status = status
        self.sent = []
        self._last_message_at = None

    @property
    def status(self):
        return self._status

    @property
    def is_connected(self):
        return self._status is StreamStatus.OPEN

    @property
    def last_message_at(self):
        return self._last_message_at

    async def send_text(self, message: str):
        self.sent.append(message)
        return True


def _ticker_frame(symbol="BTCUSDT", last="64250.10", bid="64250.00", ask="64250.20",
                  event_ts: int = 1_752_000_000_000):
    """Build a realistic Binance 24hrTicker payload with sane OHLC values."""
    return {
        "e": "24hrTicker", "E": event_ts, "s": symbol,
        "p": "125.10", "P": "0.19",
        "c": last, "o": "64125.00", "h": "64500.00", "l": "63900.00",
        "v": "12000.5", "q": "771000000",
        "b": bid, "B": "1.5", "a": ask, "A": "2.0",
    }


def _trade_frame(symbol="BTCUSDT", price="64250.15", qty="0.25",
                 event_ts: int = 1_752_000_000_100):
    """Build a realistic Binance aggTrade payload."""
    return {
        "e": "aggTrade", "E": event_ts, "s": symbol,
        "a": 123456789, "p": price, "q": qty,
        "f": 100, "l": 105, "T": event_ts - 50, "m": False,
    }


def _mk_provider(use_live_feed: bool = True):
    provider = CryptoStreamMarketDataProvider(use_live_feed=use_live_feed)
    provider._is_running = True
    return provider


# ── 1 & 2. Normalization from vendor frames ───────────────────────────────

async def _feed_ticker_and_trade(provider, ticker=None, trade=None):
    if ticker:
        await provider._handle_ticker(ticker)
    if trade:
        await provider._handle_trade(trade)


def test_ticker_frame_normalizes_to_quote():
    """A genuine 24hrTicker frame must populate bid/ask/OHLC on the quote."""
    p = _mk_provider()
    p._subscribers.add("BTCUSDT")
    asyncio.run(_feed_ticker_and_trade(p, ticker=_ticker_frame()))
    q = p.get_latest_quote("BTCUSDT")
    assert q is not None
    assert q.price == 64250.10
    assert q.bid == 64250.00
    assert q.ask == 64250.20
    assert q.open == 64125.00
    assert q.high == 64500.00
    assert q.low == 63900.00
    assert q.feed_mode == DataFeedMode.PUBLIC_EXCHANGE_STREAM
    assert "Binance" in q.data_source


def test_trade_updates_price_after_ticker():
    """An aggTrade arriving after a ticker must advance the emitted price."""
    p = _mk_provider()
    p._subscribers.add("BTCUSDT")
    asyncio.run(_feed_ticker_and_trade(
        p, ticker=_ticker_frame(), trade=_trade_frame(price="64250.15"),
    ))
    q = p.get_latest_quote("BTCUSDT")
    assert q is not None
    assert q.price == 64250.15  # last trade wins over the 1 Hz ticker


def test_bad_price_never_emitted():
    """Non-finite / non-positive prices must never reach the quote cache."""
    p = _mk_provider()
    p._subscribers.add("BTCUSDT")
    asyncio.run(_feed_ticker_and_trade(
        p, ticker=_ticker_frame(last="nan"), trade=None,
    ))
    asyncio.run(_feed_ticker_and_trade(
        p, ticker=None, trade=_trade_frame(price="-5"),
    ))
    assert p.get_latest_quote("BTCUSDT") is None


def test_unknown_symbol_ignored():
    """Frames for symbols that are not subscribed must be ignored silently."""
    p = _mk_provider()
    p._subscribers.add("BTCUSDT")
    asyncio.run(_feed_ticker_and_trade(p, ticker=_ticker_frame(symbol="ETHUSDT")))
    assert p.get_latest_quote("ETHUSDT") is None
    assert p.get_latest_quote("BTCUSDT") is None


def test_subscribe_sends_wire_subscription():
    """subscribe() must emit a Binance SUBSCRIBE message for trade+ticker."""
    p = _mk_provider()
    fake = FakeStream()
    p._stream = fake
    p._ever_connected = True

    async def run():
        await p.subscribe(["BTCUSDT", "ETHUSDT"])
        await p.subscribe(["BTCUSDT"])  # duplicate must be skipped

    asyncio.run(run())
    assert len(fake.sent) == 1
    import json
    msg = json.loads(fake.sent[0])
    assert msg["method"] == "SUBSCRIBE"
    assert "btcusdt@trade" in msg["params"]
    assert "btcusdt@ticker" in msg["params"]
    assert "ethusdt@trade" in msg["params"]
    assert "ethusdt@ticker" in msg["params"]


def test_non_usdt_symbols_queued_as_unresolved():
    """INR-denominated symbols have no Binance stream — tracked as unresolved."""
    p = _mk_provider()
    fake = FakeStream()
    p._stream = fake
    p._ever_connected = True

    async def run():
        await p.subscribe(["BTCINR"])

    asyncio.run(run())
    assert fake.sent == []  # nothing sent on the wire
    assert "BTCINR" in p._unresolved_symbols
    assert p._subscribers == {"BTCINR"}


def test_resubscribe_restores_full_set():
    """After reconnect the provider must re-send the entire subscription set."""
    p = _mk_provider()
    fake = FakeStream()
    p._stream = fake
    p._ever_connected = True
    # Simulate an established subscription, then a reconnect where the server
    # forgot everything.
    asyncio.run(p.subscribe(["BTCUSDT", "SOLUSDT"]))

    fake2 = FakeStream()  # fresh socket after reconnect
    p._stream = fake2
    asyncio.run(p._resubscribe_all())

    import json
    all_params = []
    for m in fake2.sent:
        all_params.extend(json.loads(m)["params"])
    assert "btcusdt@trade" in all_params
    assert "btcusdt@ticker" in all_params
    assert "solusdt@trade" in all_params
    assert "solusdt@ticker" in all_params


# ── 4 & 5. FeedState classification ───────────────────────────────────────

def test_feed_state_live_when_open_fresh():
    p = _mk_provider()
    p._subscribers.add("BTCUSDT")
    asyncio.run(_feed_ticker_and_trade(p, ticker=_ticker_frame()))
    fake = FakeStream()
    fake._last_message_at = datetime.now(timezone.utc)
    p._stream = fake
    p._ever_connected = True
    assert p.classify_feed_state() == FeedState.LIVE


def test_feed_state_stale_when_open_but_aged():
    p = _mk_provider()
    p._subscribers.add("BTCUSDT")
    asyncio.run(_feed_ticker_and_trade(p, ticker=_ticker_frame()))
    fake = FakeStream()
    fake._last_message_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    p._stream = fake
    p._ever_connected = True
    assert p.classify_feed_state() == FeedState.STALE


def test_feed_state_stale_when_reconnecting_after_connection():
    p = _mk_provider()
    p._subscribers.add("BTCUSDT")
    asyncio.run(_feed_ticker_and_trade(p, ticker=_ticker_frame()))
    fake = FakeStream(status=StreamStatus.RECONNECTING)
    p._stream = fake
    p._ever_connected = True
    assert p.classify_feed_state() == FeedState.STALE


def test_feed_state_unavailable_when_not_running():
    p = _mk_provider()
    p._is_running = False
    assert p.classify_feed_state() == FeedState.UNAVAILABLE


def test_feed_state_unavailable_when_no_stream():
    p = _mk_provider()
    p._ever_connected = True
    p._stream = None
    assert p.classify_feed_state() == FeedState.UNAVAILABLE


def test_feed_state_demo_never_live():
    p = _mk_provider(use_live_feed=False)
    assert p.classify_feed_state() == FeedState.DEMO


# ── 10. Demo stream honesty ───────────────────────────────────────────────

def test_demo_stream_emits_honest_demo_ticks():
    p = _mk_provider(use_live_feed=False)
    p._subscribers.add("BTCUSDT")
    received = []

    async def callback(tick):
        received.append(tick)

    p.add_callback(callback)

    async def run():
        task = asyncio.create_task(p._run_demo_stream())
        await asyncio.sleep(0.2)  # let the loop emit at least one cycle
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return p

    asyncio.run(run())
    assert received, "demo stream must emit ticks"
    tick = received[0]
    assert tick.feed_mode == DataFeedMode.DEMO_SIMULATED
    assert tick.price > 0
    assert tick.bid < tick.ask
    assert p.classify_feed_state() == FeedState.DEMO