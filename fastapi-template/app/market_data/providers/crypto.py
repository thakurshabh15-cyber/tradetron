"""Crypto Market Data Provider — Real Binance Public WebSocket + Demo Fallback.

When feed_mode_crypto=live: Connects to Binance's free public WebSocket stream
(wss://stream.binance.com:9443/ws) for real-time crypto prices. No API key required.

When feed_mode_crypto=demo: Generates simulated ticks clearly labeled DEMO_SIMULATED.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logging import get_logger
from app.market_data.base import AssetClass, BaseMarketDataProvider, DataFeedMode, NormalizedTick

logger = get_logger("market.crypto")

_CRYPTO_SEED_PRICES: dict[str, float] = {
    "BTCUSDT": 64250.00,
    "ETHUSDT": 3480.50,
    "SOLUSDT": 154.20,
    "BNBUSDT": 585.60,
    "XRPUSDT": 0.5840,
    "MATICINR": 48.50,
    "BTCINR": 5450000.00,
    "ETHINR": 295000.00,
}

# Map our symbols to Binance stream names (lowercase, no separator)
_BINANCE_STREAM_MAP: dict[str, str] = {
    "BTCUSDT": "btcusdt",
    "ETHUSDT": "ethusdt",
    "SOLUSDT": "solusdt",
    "BNBUSDT": "bnbusdt",
    "XRPUSDT": "xrpusdt",
}


class CryptoMarketDataProvider(BaseMarketDataProvider):
    """Real-time Crypto provider with Binance public WebSocket and demo fallback."""

    def __init__(self, use_live_feed: bool = False) -> None:
        feed_mode = DataFeedMode.PUBLIC_EXCHANGE_STREAM if use_live_feed else DataFeedMode.DEMO_SIMULATED
        data_source = "Binance Public WebSocket (Live)" if use_live_feed else "Crypto Market Stream (Demo Simulated)"
        super().__init__(name="CryptoMarketProvider", asset_class=AssetClass.CRYPTO, feed_mode=feed_mode)
        self.data_source = data_source
        self._use_live = use_live_feed

        self._quotes: dict[str, NormalizedTick] = {}
        self._open_prices: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._max_reconnect_delay = 30.0

    async def start(self) -> None:
        self._is_running = True
        if self._use_live:
            self._task = asyncio.create_task(self._run_binance_ws())
        else:
            self._task = asyncio.create_task(self._run_demo_stream())
        logger.info("Crypto Market Data Provider active [%s] - Mode: %s", self.data_source, self.feed_mode.value)

    async def stop(self) -> None:
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Crypto Market Data Provider stopped")

    async def subscribe(self, symbols: list[str]) -> None:
        for s in symbols:
            clean = s.upper().strip()
            self._subscribers.add(clean)
            if clean not in self._open_prices:
                seed = _CRYPTO_SEED_PRICES.get(clean, 100.0)
                self._open_prices[clean] = seed

    async def unsubscribe(self, symbols: list[str]) -> None:
        for s in symbols:
            self._subscribers.discard(s.upper().strip())

    def get_latest_quote(self, symbol: str) -> Optional[NormalizedTick]:
        return self._quotes.get(symbol.upper().strip())

    async def get_historical_candles(
        self, symbol: str, timeframe: str = "5m", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch real historical OHLCV klines from Binance Public API."""
        clean_sym = symbol.upper().strip()
        if not clean_sym.endswith("USDT") and not clean_sym.endswith("INR") and not clean_sym.endswith("BUSD"):
            clean_sym = f"{clean_sym}USDT"

        # Normalize timeframe for Binance (1m, 5m, 15m, 1h, 1d)
        interval = timeframe.lower()
        if interval == "1d":
            interval = "1d"

        url = f"https://api.binance.com/api/v3/klines?symbol={clean_sym}&interval={interval}&limit={min(limit, 500)}"

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    raw_klines = resp.json()
                    candles = []
                    for k in raw_klines:
                        # k: [open_time, open, high, low, close, volume, ...]
                        candles.append({
                            "time": int(k[0] / 1000),
                            "open": float(k[1]),
                            "high": float(k[2]),
                            "low": float(k[3]),
                            "close": float(k[4]),
                            "volume": float(k[5]),
                        })
                    logger.info("Fetched %d real Binance candles for %s (%s)", len(candles), clean_sym, interval)
                    return candles
                else:
                    logger.warning("Binance Kline API returned %d: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.error("Failed to fetch Binance historical candles for %s: %s", clean_sym, exc)

        return []

    # ── REAL: Binance Public WebSocket Stream ────────────────────────────────
    async def _run_binance_ws(self) -> None:
        """Connect to Binance combined stream for real-time mini ticker data.

        Uses the free public endpoint — no API key required.
        Streams: !miniTicker@arr (all symbols) or individual <symbol>@ticker
        """
        reconnect_delay = 1.0

        while self._is_running:
            try:
                import websockets  # type: ignore[import-untyped]
            except ImportError:
                logger.error(
                    "websockets package not installed — cannot connect to Binance. "
                    "Install with: pip install websockets. Falling back to demo mode."
                )
                await self._run_demo_stream()
                return

            # Build combined stream URL for subscribed Binance-supported symbols
            binance_streams = []
            for sym in list(self._subscribers):
                stream_name = _BINANCE_STREAM_MAP.get(sym)
                if stream_name:
                    binance_streams.append(f"{stream_name}@ticker")

            if not binance_streams:
                # No Binance-supported symbols subscribed, use demo for all
                logger.info("No Binance-supported symbols subscribed, using demo stream for INR pairs")
                await self._run_demo_stream()
                return

            url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(binance_streams)}"
            logger.info("Connecting to Binance WebSocket: %s", url)

            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("Binance WebSocket connected — streaming %d symbols", len(binance_streams))
                    reconnect_delay = 1.0

                    # Also start a background task for INR pairs that Binance doesn't cover
                    inr_symbols = [s for s in self._subscribers if s not in _BINANCE_STREAM_MAP]
                    inr_task = None
                    if inr_symbols:
                        inr_task = asyncio.create_task(self._run_demo_for_symbols(inr_symbols))

                    try:
                        async for raw_msg in ws:
                            if not self._is_running:
                                break
                            try:
                                msg = json.loads(raw_msg)
                                data = msg.get("data", msg)
                                await self._process_binance_ticker(data)
                            except (json.JSONDecodeError, KeyError, TypeError) as e:
                                logger.debug("Skipping malformed Binance message: %s", e)
                    finally:
                        if inr_task:
                            inr_task.cancel()
                            try:
                                await inr_task
                            except asyncio.CancelledError:
                                pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Binance WebSocket error: %s. Reconnecting in %.1fs...", exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self._max_reconnect_delay)

    async def _process_binance_ticker(self, data: dict[str, Any]) -> None:
        """Process a Binance 24hr ticker event into a NormalizedTick."""
        # Binance ticker fields: s=symbol, c=close, o=open, h=high, l=low, v=volume, b=bestBid, a=bestAsk
        symbol = data.get("s", "").upper()
        if symbol not in self._subscribers:
            return

        try:
            price = float(data.get("c", 0))
            open_p = float(data.get("o", price))
            high = float(data.get("h", price))
            low = float(data.get("l", price))
            bid = float(data.get("b", price))
            ask = float(data.get("a", price))
            volume = int(float(data.get("v", 0)))
            change = price - open_p
            change_pct = (change / open_p * 100) if open_p else 0.0
        except (ValueError, TypeError):
            return

        # Store the opening price on first tick
        if symbol not in self._open_prices or self._open_prices[symbol] == _CRYPTO_SEED_PRICES.get(symbol, 0):
            self._open_prices[symbol] = open_p

        tick = NormalizedTick(
            symbol=symbol,
            price=price,
            bid=bid,
            ask=ask,
            open=open_p,
            high=high,
            low=low,
            close=price,
            change=round(change, 4) if price < 1 else round(change, 2),
            change_pct=round(change_pct, 2),
            volume=volume,
            asset_class=AssetClass.CRYPTO,
            feed_mode=DataFeedMode.PUBLIC_EXCHANGE_STREAM,
            data_source="Binance Public WebSocket (Live)",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        self._quotes[symbol] = tick
        await self._emit_tick(tick)

    # ── DEMO: Simulated stream for symbols without live coverage ────────────
    async def _run_demo_for_symbols(self, symbols: list[str]) -> None:
        """Run demo simulation only for specific symbols (e.g. INR pairs)."""
        try:
            while self._is_running:
                for symbol in symbols:
                    prev_quote = self._quotes.get(symbol)
                    open_p = self._open_prices.get(symbol, _CRYPTO_SEED_PRICES.get(symbol, 100.0))
                    prev_price = prev_quote.price if prev_quote else open_p

                    pct_move = random.gauss(0.0002, 0.003)
                    new_price = max(0.0001, prev_price * (1.0 + pct_move))

                    spread = max(0.0001, new_price * 0.0005)
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
        except asyncio.CancelledError:
            pass

    async def _run_demo_stream(self) -> None:
        """Full demo stream for all subscribed symbols."""
        reconnect_delay = 1.0
        while self._is_running:
            try:
                while self._is_running:
                    symbols = list(self._subscribers) or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "MATICINR"]
                    for symbol in symbols:
                        prev_quote = self._quotes.get(symbol)
                        open_p = self._open_prices.get(symbol, _CRYPTO_SEED_PRICES.get(symbol, 100.0))
                        prev_price = prev_quote.price if prev_quote else open_p

                        # 24/7 Crypto volatility simulation (0.1% - 0.5%)
                        pct_move = random.gauss(0.0002, 0.003)
                        new_price = max(0.0001, prev_price * (1.0 + pct_move))

                        spread = max(0.0001, new_price * 0.0005)
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
                logger.error("Crypto demo feed error: %s. Reconnecting in %.1fs...", exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, self._max_reconnect_delay)
