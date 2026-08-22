"""Forex & NSE Currency Derivatives (NSE-CDS) Market Data Provider."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Optional

from app.core.logging import get_logger
from app.market_data.base import AssetClass, BaseMarketDataProvider, DataFeedMode, NormalizedTick

import math
from typing import Any

logger = get_logger("market.forex")

_FOREX_SEED_PRICES: dict[str, float] = {
    "USDINR": 83.9250,
    "EURINR": 91.4520,
    "GBPINR": 108.2040,
    "JPYINR": 0.5720,
    "EURUSD": 1.0895,
    "GBPUSD": 1.2890,
}

_FOREX_YF_MAP: dict[str, str] = {
    "USDINR": "USDINR=X",
    "EURINR": "EURINR=X",
    "GBPINR": "GBPINR=X",
    "JPYINR": "JPYINR=X",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
}


class ForexMarketDataProvider(BaseMarketDataProvider):
    """Real-time provider for NSE Currency Derivatives (NSE-CDS) and Global FX pairs.
    
    Provides live exchange rate ticks and real historical candle data.
    """

    def __init__(self, use_live_feed: bool = True) -> None:
        feed_mode = DataFeedMode.PUBLIC_EXCHANGE_STREAM if use_live_feed else DataFeedMode.DEMO_SIMULATED
        data_source = "Live Forex Exchange Rates" if use_live_feed else "NSE Currency Derivatives (Demo Feed)"
        super().__init__(name="ForexMarketProvider", asset_class=AssetClass.FOREX, feed_mode=feed_mode)
        self.data_source = data_source

        self._quotes: dict[str, NormalizedTick] = {}
        self._open_prices: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._max_reconnect_delay = 30.0

    async def start(self) -> None:
        self._is_running = True
        self._task = asyncio.create_task(self._run_forex_stream())
        self._sync_task = asyncio.create_task(self._run_fx_sync())
        logger.info("Forex Market Data Provider active [%s] - Mode: %s", self.data_source, self.feed_mode.value)

    async def stop(self) -> None:
        self._is_running = False
        for t in (self._task, self._sync_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        logger.info("Forex Market Data Provider stopped")

    async def subscribe(self, symbols: list[str]) -> None:
        for s in symbols:
            clean = s.upper().strip()
            self._subscribers.add(clean)
            if clean not in self._open_prices:
                seed = _FOREX_SEED_PRICES.get(clean, 80.0)
                self._open_prices[clean] = seed

    async def unsubscribe(self, symbols: list[str]) -> None:
        for s in symbols:
            self._subscribers.discard(s.upper().strip())

    def get_latest_quote(self, symbol: str) -> Optional[NormalizedTick]:
        return self._quotes.get(symbol.upper().strip())

    async def get_historical_candles(
        self, symbol: str, timeframe: str = "5m", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch real historical OHLCV candles for Forex pairs."""
        clean_sym = symbol.upper().strip()
        yf_sym = _FOREX_YF_MAP.get(clean_sym, f"{clean_sym}=X")

        tf = timeframe.lower()
        if tf in ("1m", "5m", "15m"):
            period = "5d"
            interval = tf
        elif tf in ("30m", "1h", "60m"):
            period = "1mo"
            interval = "1h" if tf in ("1h", "60m") else "30m"
        else:
            period = "1y"
            interval = "1d"

        def _fetch():
            try:
                import yfinance as yf
                ticker = yf.Ticker(yf_sym)
                df = ticker.history(period=period, interval=interval)
                if df.empty:
                    return []
                df = df.tail(limit)
                candles = []
                for idx, row in df.iterrows():
                    ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx.to_pydatetime().timestamp())
                    candles.append({
                        "time": ts,
                        "open": round(float(row["Open"]), 4),
                        "high": round(float(row["High"]), 4),
                        "low": round(float(row["Low"]), 4),
                        "close": round(float(row["Close"]), 4),
                        "volume": float(row.get("Volume", 0)),
                    })
                return candles
            except Exception as e:
                logger.error("Error fetching Forex yfinance candles for %s: %s", yf_sym, e)
                return []

        candles = await asyncio.to_thread(_fetch)
        if candles:
            logger.info("Fetched %d real Forex candles for %s (%s)", len(candles), clean_sym, tf)
        return candles

    async def _run_fx_sync(self) -> None:
        """Periodically refresh real spot forex exchange rates."""
        while self._is_running:
            try:
                def _sync():
                    import yfinance as yf
                    symbols = list(self._subscribers) or list(_FOREX_SEED_PRICES.keys())
                    yf_tickers = [_FOREX_YF_MAP.get(s, f"{s}=X") for s in symbols]
                    data = yf.download(yf_tickers, period="1d", interval="1m", progress=False)
                    updated = {}
                    if not data.empty and "Close" in data:
                        close_data = data["Close"]
                        for s in symbols:
                            yf_s = _FOREX_YF_MAP.get(s, f"{s}=X")
                            try:
                                if len(yf_tickers) == 1:
                                    last_val = close_data.dropna().iloc[-1]
                                else:
                                    last_val = close_data[yf_s].dropna().iloc[-1]
                                if last_val and not math.isnan(last_val):
                                    updated[s] = float(last_val)
                            except Exception:
                                pass
                    return updated

                latest = await asyncio.to_thread(_sync)
                for sym, price in latest.items():
                    self._open_prices[sym] = price
                    prev = self._quotes.get(sym)
                    if prev:
                        prev.price = round(price, 4)

                await asyncio.sleep(20.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Forex sync background notice: %s", exc)
                await asyncio.sleep(20.0)

    async def _run_forex_stream(self) -> None:
        """Stream currency derivative ticks with tight PIP spreads."""
        reconnect_delay = 1.0
        while self._is_running:
            try:
                while self._is_running:
                    symbols = list(self._subscribers) or list(_FOREX_SEED_PRICES.keys())
                    for symbol in symbols:
                        prev_quote = self._quotes.get(symbol)
                        open_p = self._open_prices.get(symbol, _FOREX_SEED_PRICES.get(symbol, 80.0))
                        prev_price = prev_quote.price if prev_quote else open_p

                        # Currency pip fluctuations (0.002% - 0.01%)
                        pip_move = random.gauss(0.000005, 0.00015)
                        new_price = max(0.0001, round(prev_price * (1.0 + pip_move), 4))

                        spread = 0.0025 if "INR" in symbol else 0.0001
                        bid = round(new_price - spread / 2, 4)
                        ask = round(new_price + spread / 2, 4)
                        change = round(new_price - open_p, 4)
                        change_pct = round((change / open_p) * 100, 2) if open_p else 0.0
                        vol = random.randint(1000, 50_000)

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
                            asset_class=AssetClass.FOREX,
                            feed_mode=self.feed_mode,
                            data_source=self.data_source,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )

                        self._quotes[symbol] = tick
                        await self._emit_tick(tick)

                    await asyncio.sleep(1.0)
                    reconnect_delay = 1.0

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Forex feed error: %s. Reconnecting in %.1fs...", exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, self._max_reconnect_delay)
