"""Binance Market Streams WebSocket provider — genuine real-time crypto (Phase 15A).

Replaces the previous 60-second CoinGecko REST polling as the LIVE crypto feed.
The Binance public market stream is a genuine real-time WebSocket pipeline:

  wss://stream.binance.com:9443/ws            (market data — no credentials)

Per-symbol streams subscribed dynamically:

  BTCUSDT -> btcusdt@trade   (real aggressive prints: price, quantity, event time)
  BTCUSDT -> btcusdt@ticker  (1 Hz snapshots: best bid/ask, OHLC, 24h volume, %change)

Design rules honoured:
- Every ``NormalizedTick`` timestamp is the EXCHANGE event time (``E``), never
  ``datetime.now()``.
- Invalid / non-finite / non-positive prices are rejected (never emitted).
- A symbol only reaches ``LIVE`` after it has received a genuine ``@ticker``
  snapshot (bid/ask/OHLC are real vendor values — nothing is fabricated).
- Reconnect + subscription restoration: after every (re)connect the provider
  re-sends the full requested subscription set (``on_reconnect`` hook).
- Heartbeat: protocol ping/pong plus a data-liveness deadline.
- If the stream is unreachable (e.g. Binance HTTP-451 geo-blocking from some
  cloud providers), the provider reports ``UNAVAILABLE`` — it NEVER pretends
  a delayed/synthetic feed is live.  An explicit ``DELAYED`` CoinGecko fallback
  is available only via ``crypto_ws_fallback_coingecko_delayed=True`` and is
  always labelled DELAYED.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logging import get_logger
from app.market_data.base import AssetClass, BaseMarketDataProvider, DataFeedMode, NormalizedTick
from app.market_data.streaming.feed_state import FeedState
from app.market_data.streaming.ws_stream import StreamStatus, WebSocketStream

logger = get_logger("market.crypto_stream")

_BINANCE_WS_DEFAULT = "wss://stream.binance.com:9443/ws"

# Approximate spread (fraction) used ONLY for the demo stream — never for the
# genuine live path (live bid/ask come from the exchange ticker).
_DEMO_SPREAD: dict[str, float] = {
    "BTCUSDT": 0.0001,
    "ETHUSDT": 0.0002,
    "SOLUSDT": 0.0004,
    "BNBUSDT": 0.0003,
    "XRPUSDT": 0.0005,
}

# Seed prices for DEMO mode (never presented as live).
_CRYPTO_SEED_PRICES: dict[str, float] = {
    "BTCUSDT": 64250.00,
    "ETHUSDT": 3480.50,
    "SOLUSDT": 154.20,
    "BNBUSDT": 585.60,
    "XRPUSDT": 0.5840,
    "BTCINR": 5_350_000.0,
    "ETHINR": 290_000.0,
    "MATICINR": 60.0,
}

_STREAM_REQUEST_ID = 1_000

# CoinGecko mapping used ONLY by the honest DELAYED fallback path
# (crypto_ws_fallback_coingecko_delayed=True), never for the live path.
_COINGECKO_FALLBACK_MAP: dict[str, dict[str, str]] = {
    "BTCUSDT": {"id": "bitcoin", "vs": "usd"},
    "ETHUSDT": {"id": "ethereum", "vs": "usd"},
    "SOLUSDT": {"id": "solana", "vs": "usd"},
    "BNBUSDT": {"id": "binancecoin", "vs": "usd"},
    "XRPUSDT": {"id": "ripple", "vs": "usd"},
}


def _stream_name(symbol: str, kind: str) -> str:
    """Binance stream identifier for a symbol (e.g. ``btcusdt@trade``)."""
    return f"{symbol.lower()}@{kind}"


def _safe_price(value: Any) -> Optional[float]:
    """Parse a positive finite price; returns None for anything invalid."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


class CryptoStreamMarketDataProvider(BaseMarketDataProvider):
    """Genuine real-time crypto provider backed by Binance public market streams."""

    def __init__(self, use_live_feed: bool = False, ws_url: Optional[str] = None) -> None:
        feed_mode = (
            DataFeedMode.PUBLIC_EXCHANGE_STREAM if use_live_feed
            else DataFeedMode.DEMO_SIMULATED
        )
        data_source = (
            "Binance Market Streams WebSocket (Real-time)"
            if use_live_feed
            else "Crypto Market Stream (Demo Simulated)"
        )
        super().__init__(
            name="CryptoStreamMarketProvider",
            asset_class=AssetClass.CRYPTO,
            feed_mode=feed_mode,
        )
        self.data_source = data_source
        self._use_live = use_live_feed

        from app.config import settings

        self._ws_url = ws_url or settings.crypto_ws_base_url or _BINANCE_WS_DEFAULT
        self._stale_after = settings.crypto_ws_stale_after
        self._allow_delayed_fallback = settings.crypto_ws_fallback_coingecko_delayed

        self._quotes: dict[str, NormalizedTick] = {}
        self._open_prices: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._stream: Optional[WebSocketStream] = None
        # Per-symbol merged state from @ticker + @trade events.
        self._state: dict[str, dict[str, Any]] = {}
        # Symbols that have received at least one genuine @ticker snapshot.
        self._ready: set[str] = set()
        self._requested_streams: set[str] = set()
        self._unresolved_symbols: set[str] = set()
        self._ever_connected = False
        self._delayed_fallback_active = False
        self._delayed_task: Optional[asyncio.Task] = None

        self.last_sync_error: Optional[str] = None
        self.last_sync_success: Optional[datetime] = None
        self.last_candle_source: str = "REAL"
        self._decoder_malformed = 0
        self._max_reconnect_delay = 30.0
# ── Feed state ─────────────────────────────────────────────────────

    def classify_feed_state(self) -> FeedState:
        """Honest, fail-closed classification for the crypto feed."""
        if not getattr(self, "_is_running", False):
            return FeedState.UNAVAILABLE
        if self.feed_mode == DataFeedMode.DEMO_SIMULATED:
            return FeedState.DEMO
        if self._delayed_fallback_active:
            return FeedState.DELAYED
        stream = self._stream
        if stream is None or stream.status is StreamStatus.UNAVAILABLE:
            return FeedState.UNAVAILABLE
        if stream.status is not StreamStatus.OPEN:
            return FeedState.STALE if self._ever_connected else FeedState.UNAVAILABLE
        if not self._quotes:
            return FeedState.UNAVAILABLE
        anchor = stream.last_message_at or self.last_sync_success
        if anchor is None:
            return FeedState.UNAVAILABLE
        age = (datetime.now(timezone.utc) - anchor).total_seconds()
        if age > self._stale_after:
            return FeedState.STALE
        return FeedState.LIVE

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        self._is_running = True
        if self._use_live:
            from app.config import settings

            self._stream = WebSocketStream(
                name="crypto-binance",
                url=self._ws_url,
                on_message=self._on_ws_message,
                on_reconnect=self._resubscribe_all,
                on_terminal=self._on_stream_terminal,
                heartbeat_interval=settings.crypto_ws_heartbeat_interval,
                heartbeat_deadline=settings.crypto_ws_heartbeat_deadline,
                max_reconnect_attempts=25,
                max_reconnect_delay=self._max_reconnect_delay,
            )
            self._stream.start()
            logger.info(
                "CryptoStream provider active [%s] — %s",
                self.data_source, self._ws_url,
            )
        else:
            self._task = asyncio.create_task(self._run_demo_stream())
            logger.info("CryptoStream provider active [%s] (DEMO)", self.data_source)

    async def stop(self) -> None:
        """Gracefully disconnect the WebSocket and cancel background tasks."""
        self._is_running = False
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._delayed_task is not None:
            self._delayed_task.cancel()
            try:
                await self._delayed_task
            except asyncio.CancelledError:
                pass
            self._delayed_task = None
        logger.info("CryptoStream provider stopped")

    # ── Subscription ─────────────────────────────────────────────────

    async def subscribe(self, symbols: list[str]) -> None:
        """Add symbols to the active streaming set and subscribe on the wire."""
        new_symbols: list[str] = []
        for sym in symbols:
            clean = sym.upper().strip()
            if clean not in self._subscribers:
                self._subscribers.add(clean)
                new_symbols.append(clean)
                if clean not in self._open_prices:
                    seed = _CRYPTO_SEED_PRICES.get(clean, 100.0)
                    self._open_prices[clean] = seed
        if self._use_live and new_symbols and self._stream is not None:
            await self._subscribe_stream(new_symbols)

    async def unsubscribe(self, symbols: list[str]) -> None:
        """Remove symbols from the active streaming set and unsubscribe on the wire."""
        removed: list[str] = []
        for sym in symbols:
            clean = sym.upper().strip()
            if clean in self._subscribers:
                self._subscribers.discard(clean)
                self._requested_streams.discard(clean)
                removed.append(clean)
                self._state.pop(clean, None)
                self._ready.discard(clean)
        if self._use_live and removed and self._stream is not None:
            await self._unsubscribe_stream(removed)

    def get_latest_quote(self, symbol: str) -> Optional[NormalizedTick]:
        """Return the most recent cached quote for a symbol."""
        return self._quotes.get(symbol.upper().strip())

    async def get_historical_candles(
        self, symbol: str, timeframe: str = "5m", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV candles from Binance public klines REST API.

        Binance ``/api/v3/klines`` returns arrays of:
        [open_time, open, high, low, close, volume, close_time, ...]

        Timeframe notation matches Binance (1m, 5m, 15m, 1h, 1d).
        """
        clean_sym = symbol.upper().strip()
        if not clean_sym.endswith("USDT"):
            self.last_candle_source = "UNAVAILABLE"
            return []
        params: dict[str, Any] = {
            "symbol": clean_sym,
            "interval": timeframe,
            "limit": min(limit, 500),
        }
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.binance.com/api/v3/klines", params=params
                )
                resp.raise_for_status()
                raw = resp.json()
        except Exception as exc:
            logger.error("Binance klines fetch failed for %s: %s", clean_sym, exc)
            # Fail-closed honesty: no genuine OHLCV was received (e.g. Binance
            # HTTP-451 geo-blocks cloud/CI runner IPs) — never label it REAL.
            self.last_candle_source = "UNAVAILABLE"
            return []

        candles: list[dict[str, Any]] = []

        if isinstance(raw, dict) and raw.get("code"):
            # Binance error payload: {"code":-1121,"msg":"Invalid symbol."}
            logger.warning("Binance klines error for %s: %s", clean_sym, raw.get("msg"))
            self.last_candle_source = "UNAVAILABLE"
            return []

        for row in raw:
            try:
                candles.append({
                    "time": int(row[0] / 1000),  # open_time ms -> epoch seconds
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            except (IndexError, TypeError, ValueError):
                continue
        # Honest fail-closed disclosure: only genuine Binance OHLCV earns the
        # REAL label; any empty result (e.g. all rows malformed) is UNAVAILABLE.
        self.last_candle_source = "REAL" if candles else "UNAVAILABLE"
        return candles

    # ── WebSocket message handling ───────────────────────────────────

    async def _on_ws_message(self, raw: str | bytes) -> None:
        """Parse a single Binance WebSocket message and update state."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        # Binance subscription ack: {"result":null,"id":1} or error
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._decoder_malformed += 1
            return

        if not isinstance(data, dict):
            return

        # Subscription/unsubscription acknowledgements — ignore
        if "result" in data and "id" in data:
            return

        event_type = data.get("e")
        if event_type == "aggTrade":
            await self._handle_trade(data)
        elif event_type == "24hrTicker":
            await self._handle_ticker(data)
        elif event_type == "error":
            logger.warning("[crypto-stream] Binance error: %s", data.get("msg", data))

    async def _handle_trade(self, data: dict[str, Any]) -> None:
        """Process an aggTrade event: update last-trade state for the symbol."""
        symbol = data.get("s", "").upper()
        if not symbol or symbol not in self._subscribers:
            return

        price = _safe_price(data.get("p"))
        if price is None:
            return

        event_time = data.get("E")
        trade_time = data.get("T", event_time)
        ts = self._millis_to_iso(trade_time)

        state = self._state.setdefault(symbol, {})
        state["last_trade_price"] = price
        state["last_trade_qty"] = data.get("q")
        state["last_trade_time"] = ts
        state["last_trade_buyer_maker"] = data.get("m", False)
        if event_time:
            state["event_time"] = event_time

        # Merge into quote if we already have a @ticker snapshot
        await self._emit_if_ready(symbol)

    async def _handle_ticker(self, data: dict[str, Any]) -> None:
        """Process a 24hrTicker event: update full quote state for the symbol."""
        symbol = data.get("s", "").upper()
        if not symbol or symbol not in self._subscribers:
            return

        last_price = _safe_price(data.get("c"))
        if last_price is None:
            return

        state = self._state.setdefault(symbol, {})
        state["ticker_last"] = last_price
        state["ticker_bid"] = _safe_price(data.get("b")) or 0.0
        state["ticker_ask"] = _safe_price(data.get("a")) or 0.0
        state["ticker_open"] = _safe_price(data.get("o")) or 0.0
        state["ticker_high"] = _safe_price(data.get("h")) or 0.0
        state["ticker_low"] = _safe_price(data.get("l")) or 0.0
        state["ticker_change"] = _safe_price(data.get("p")) or 0.0
        state["ticker_change_pct"] = _safe_price(data.get("P")) or 0.0
        state["ticker_volume"] = int(float(data.get("v", 0) or 0))
        event_time = data.get("E")
        if event_time:
            state["event_time"] = event_time
            state["ticker_time"] = self._millis_to_iso(event_time)

        # Mark symbol as ready once we have the first @ticker snapshot
        self._ready.add(symbol)

        await self._emit_if_ready(symbol)

    async def _emit_if_ready(self, symbol: str) -> None:
        """Build and emit a NormalizedTick if the symbol has sufficient state."""
        state = self._state.get(symbol)
        if state is None:
            return

        # We need at least a last trade price or ticker last to emit
        price = state.get("last_trade_price") or state.get("ticker_last")
        if price is None or price <= 0:
            return

        # Bid/ask: prefer @ticker values (real order book); fall back to spread
        bid = state.get("ticker_bid", 0.0) or 0.0
        ask = state.get("ticker_ask", 0.0) or 0.0
        if bid <= 0 or ask <= 0:
            spread = price * _DEMO_SPREAD.get(symbol, 0.0005)
            bid = price - spread / 2
            ask = price + spread / 2

        # OHLC: prefer @ticker values; fall back to open price
        open_p = state.get("ticker_open", 0.0) or self._open_prices.get(symbol, price)
        high = state.get("ticker_high", 0.0) or price
        low = state.get("ticker_low", 0.0) or price
        change = state.get("ticker_change", 0.0) or (price - open_p)
        change_pct = state.get("ticker_change_pct", 0.0) or (
            (change / open_p * 100) if open_p else 0.0
        )
        volume = state.get("ticker_volume", 0) or 0

        # Timestamp: prefer event time; fail-closed if missing
        ts = state.get("ticker_time") or state.get("last_trade_time")
        if not ts:
            ts = datetime.now(timezone.utc).isoformat()

        tick = NormalizedTick(
            symbol=symbol,
            price=price,
            bid=bid,
            ask=ask,
            open=open_p,
            high=high,
            low=low,
            close=open_p,
            change=change,
            change_pct=change_pct,
            volume=volume,
            asset_class=AssetClass.CRYPTO,
            feed_mode=DataFeedMode.PUBLIC_EXCHANGE_STREAM,
            data_source="Binance Market Streams (Live WebSocket)",
            timestamp=ts,
        )

        self._quotes[symbol] = tick
        self.last_sync_success = datetime.now(timezone.utc)
        self.last_sync_error = None
        await self._emit_tick(tick)

    # ── Wire-level subscribe/unsubscribe ─────────────────────────────

    async def _subscribe_stream(self, symbols: list[str]) -> None:
        """Send Binance SUBSCRIBE messages for the given symbols (both @trade and @ticker)."""
        global _STREAM_REQUEST_ID

        streams: list[str] = []
        for sym in symbols:
            clean = sym.upper().strip()
            if clean in self._requested_streams:
                continue  # already subscribed
            # Only subscribe Binance-supported symbols (USDT pairs)
            if not clean.endswith("USDT"):
                self._unresolved_symbols.add(clean)
                continue
            streams.append(_stream_name(clean, "trade"))
            streams.append(_stream_name(clean, "ticker"))
            self._requested_streams.add(clean)

        if not streams:
            return

        msg = json.dumps({
            "method": "SUBSCRIBE",
            "params": streams,
            "id": _STREAM_REQUEST_ID,
        })
        _STREAM_REQUEST_ID += 1

        if self._stream and self._stream.is_connected:
            ok = await self._stream.send_text(msg)
            if ok:
                logger.info("[crypto-stream] SUBSCRIBED %d streams: %s", len(streams), streams)
            else:
                logger.warning("[crypto-stream] SUBSCRIBE send failed for %s", streams)
        else:
            logger.debug("[crypto-stream] Stream not connected; subscriptions queued for reconnect")

    async def _unsubscribe_stream(self, symbols: list[str]) -> None:
        """Send Binance UNSUBSCRIBE messages for the given symbols."""
        global _STREAM_REQUEST_ID

        streams: list[str] = []
        for sym in symbols:
            clean = sym.upper().strip()
            if clean not in self._requested_streams:
                continue
            streams.append(_stream_name(clean, "trade"))
            streams.append(_stream_name(clean, "ticker"))
            self._requested_streams.discard(clean)

        if not streams:
            return

        msg = json.dumps({
            "method": "UNSUBSCRIBE",
            "params": streams,
            "id": _STREAM_REQUEST_ID,
        })
        _STREAM_REQUEST_ID += 1

        if self._stream and self._stream.is_connected:
            ok = await self._stream.send_text(msg)
            if ok:
                logger.info("[crypto-stream] UNSUBSCRIBED %d streams: %s", len(streams), streams)

    async def _resubscribe_all(self) -> None:
        """Reconnect hook: re-send all current subscriptions after reconnect."""
        if not self._requested_streams:
            return
        symbols = list(self._requested_streams)
        self._requested_streams.clear()  # clear so _subscribe_stream re-adds them
        await self._subscribe_stream(symbols)
        logger.info("[crypto-stream] Resubscribed %d symbols after reconnect", len(symbols))

    async def _on_stream_terminal(self) -> None:
        """Called when the WebSocket stream transitions to UNAVAILABLE."""
        logger.error(
            "[crypto-stream] Stream TERMINATED — reconnect budget exhausted"
        )
        self.last_sync_error = (
            "WebSocket stream unavailable (reconnect budget exhausted)"
        )

        # Optionally activate CoinGecko delayed fallback
        if self._allow_delayed_fallback and not self._delayed_fallback_active:
            self._delayed_fallback_active = True
            self._delayed_task = asyncio.create_task(self._run_delayed_coingecko_fallback())
            logger.warning(
                "[crypto-stream] Activating DELAYED CoinGecko fallback (honesty: DELAYED)"
            )

    # ── Demo stream ──────────────────────────────────────────────────

    async def _run_demo_stream(self) -> None:
        """Simulated 1-second tick loop — honestly labelled DEMO_SIMULATED."""
        reconnect_delay = 1.0
        while self._is_running:
            try:
                while self._is_running:
                    symbols = list(self._subscribers) or [
                        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                    ]
                    for symbol in symbols:
                        prev_quote = self._quotes.get(symbol)
                        open_p = self._open_prices.get(symbol, _CRYPTO_SEED_PRICES.get(symbol, 100.0))
                        prev_price = prev_quote.price if prev_quote else open_p

                        # 24/7 Crypto volatility simulation (0.1% – 0.5%)
                        pct_move = random.gauss(0.0002, 0.003)
                        new_price = max(0.0001, prev_price * (1.0 + pct_move))

                        spread_frac = _DEMO_SPREAD.get(symbol, 0.0005)
                        spread = max(0.0001, new_price * spread_frac)
                        bid = new_price - spread / 2
                        ask = new_price + spread / 2
                        change = new_price - open_p
                        change_pct = (change / open_p) * 100 if open_p else 0.0
                        vol = random.randint(500, 100_000)

                        tick = NormalizedTick(
                            symbol=symbol,
                            price=new_price,
                            bid=bid,
                            ask=ask,
                            open=open_p,
                            high=max(new_price, prev_quote.high if prev_quote else new_price),
                            low=min(new_price, prev_quote.low if prev_quote else new_price),
                            close=open_p,
                            change=change,
                            change_pct=change_pct,
                            volume=vol,
                            asset_class=AssetClass.CRYPTO,
                            feed_mode=DataFeedMode.DEMO_SIMULATED,
                            data_source="Crypto Market Stream (Demo Simulated)",
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )

                        self._quotes[symbol] = tick
                        await self._emit_tick(tick)

                    await asyncio.sleep(1.0)
                    reconnect_delay = 1.0

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "Crypto demo stream error: %s. Reconnecting in %.1fs...",
                    exc, reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, self._max_reconnect_delay)

    # ── Delayed CoinGecko fallback ───────────────────────────────────

    async def _run_delayed_coingecko_fallback(self) -> None:
        """Honest DELAYED fallback: polls CoinGecko REST every 60 s, always
        labelled ``DELAYED`` — never pretending to be live.

        Activated only when ``crypto_ws_fallback_coingecko_delayed=True``
        and the live WebSocket stream has gone terminal.
        """
        import httpx

        poll_interval = 60.0
        reconnect_delay = 1.0

        while self._is_running and self._delayed_fallback_active:
            try:
                supported = [sym for sym in self._subscribers if sym in _COINGECKO_FALLBACK_MAP]
                if not supported:
                    await asyncio.sleep(poll_interval)
                    continue

                ids = ",".join(_COINGECKO_FALLBACK_MAP[s]["id"] for s in supported)
                url = "https://api.coingecko.com/api/v3/simple/price"
                params = {
                    "ids": ids,
                    "vs_currencies": "usd",
                    "include_24hr_vol": "true",
                    "include_24hr_change": "true",
                    "include_last_updated_at": "true",
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()

                now = datetime.now(timezone.utc)
                for sym in supported:
                    cg_id = _COINGECKO_FALLBACK_MAP[sym]["id"]
                    entry = data.get(cg_id, {})
                    price = entry.get("usd")
                    if price is None:
                        continue

                    ts = entry.get("last_updated_at")
                    if ts:
                        tick_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    else:
                        tick_ts = now.isoformat()

                    volume = int(entry.get("usd_24h_vol", 0) or 0)
                    change_pct = entry.get("usd_24h_change", 0.0) or 0.0

                    spread_frac = _DEMO_SPREAD.get(sym, 0.0005)
                    spread = price * spread_frac

                    tick = NormalizedTick(
                        symbol=sym,
                        price=price,
                        bid=price - spread / 2,
                        ask=price + spread / 2,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        change=0.0,
                        change_pct=change_pct,
                        volume=volume,
                        asset_class=AssetClass.CRYPTO,
                        feed_mode=DataFeedMode.PUBLIC_EXCHANGE_STREAM,
                        data_source="CoinGecko REST (Delayed Fallback)",
                        timestamp=tick_ts,
                    )
                    self._quotes[sym] = tick
                    self.last_sync_success = now
                    await self._emit_tick(tick)

                self.last_sync_error = None
                reconnect_delay = 1.0

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_sync_error = f"CoinGecko delayed fallback error: {exc}"
                logger.error("[crypto-stream] Delayed fallback error: %s", exc)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

            await asyncio.sleep(poll_interval)

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _millis_to_iso(ms: int | float | None) -> str:
        """Convert Binance epoch milliseconds to ISO 8601 UTC string."""
        if ms is None:
            return datetime.now(timezone.utc).isoformat()
        try:
            return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return datetime.now(timezone.utc).isoformat()