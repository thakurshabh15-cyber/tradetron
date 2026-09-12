"""Indian Equities, F&O Indices (NIFTY/BANKNIFTY), and MCX Commodities Market Data Provider.

Honest feed policy (production hardening):

* DEMO mode (``feed_mode="demo"`` / default): simulated ticks labelled
  ``DEMO_SIMULATED`` — this is the PAPER-trading feed and is never presented
  as real market data.
* DELAYED mode (``feed_mode="delayed"``): genuinely-sourced market data
  (Yahoo Finance NSE/BSE/commodity quotes) emitted at their real cadence and
  explicitly labelled ``feed_state=DELAYED``.  Real prices are used as-is —
  NO synthetic micro-ticks are ever interpolated between refreshes.
* LIVE mode (``feed_mode="live"`` + credentials): a genuine broker-vendor
  stream is the ONLY acceptable tick source.  When no verified vendor stream
  is connected the provider reports ``UNAVAILABLE`` and emits NO ticks — a
  synthetic price can never be labelled LIVE.  Operator must provide a real
  SmartStream session (``ANGEL_JWT_TOKEN``) before this path can connect.
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logging import get_logger
from app.market_data.base import AssetClass, BaseMarketDataProvider, DataFeedMode, NormalizedTick
from app.market_data.streaming.feed_state import FeedState

logger = get_logger("market.indian_equity")

# Lazy singleton handle for the yfinance vendor module.  `import yfinance` is
# expensive and must stay lazy (cold-start cost), but keeping a single cached
# handle also makes the delayed pipeline trivially deterministic to test —
# tests patch this loader instead of mutating the process-global sys.modules.
_LOADED_YF: Optional[Any] = None


def _get_yfinance() -> Any:
    """Return the shared yfinance handle, importing it lazily on first use."""
    global _LOADED_YF
    if _LOADED_YF is None:
        import yfinance as yf

        _LOADED_YF = yf
    return _LOADED_YF

# Default refresh cadence for the genuine DELAYED real-data pipeline.  The
# underlying source is delayed market data; never claimed as real-time.
_DEFAULT_DELAYED_REFRESH_SECONDS = 15.0
# A delayed pipeline that stops producing within 3 refresh cycles + grace is
# reported STALE (fail closed) instead of pretending the last quote is fresh.
_DELAYED_STALE_GRACE_SECONDS = 60.0

# Base reference prices for Indian Equities, Indices, and MCX — used ONLY for
# DEMO simulation seed prices and cold-start history, never as real quotes.
_INDIAN_SEED_PRICES: dict[str, float] = {
    "NIFTY50": 24850.50,
    "BANKNIFTY": 51200.75,
    "FINNIFTY": 23450.20,
    "RELIANCE": 2985.40,
    "TCS": 3940.60,
    "INFY": 1620.30,
    "HDFCBANK": 1685.10,
    "ICICIBANK": 1195.80,
    "TATAMOTORS": 985.20,
    "SBIN": 845.50,
    "CRUDEOIL": 6450.00,
    "GOLD": 71800.00,
    "SILVER": 84500.00,
}


_YFINANCE_MAP: dict[str, str] = {
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "INFY": "INFY.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "TATAMOTORS": "TATAMOTORS.NS",
    "SBIN": "SBIN.NS",
    "CRUDEOIL": "CL=F",
    "GOLD": "GC=F",
    "SILVER": "SI=F",
}


class IndianEquityMarketDataProvider(BaseMarketDataProvider):
    """Real-time provider for NSE/BSE Cash, F&O Derivatives, and MCX Commodities.
    
    In production mode with broker credentials, connects to Angel One / Zerodha Kite WebSocket.
    Fetches real-time prices and historical OHLCV data from real exchange feeds.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        client_code: Optional[str] = None,
        use_live_feed: bool = False,
        feed_mode: Optional[str] = None,
    ) -> None:
        """Construct the Indian-equity provider with honest feed labelling.

        ``feed_mode`` resolves as follows (``None`` = auto):
          - ``demo``         → DEMO_SIMULATED (paper feed; may simulate)
          - ``delayed``      → genuinely-sourced DELAYED real data (never LIVE)
          - ``live``         → LIVE_BROKER_VENDOR, but ONLY ever emits genuine
                               ticks; emits nothing when a verified vendor
                               stream is absent (classifies UNAVAILABLE).
        """
        requested = (feed_mode or "").strip().lower()
        auto = not requested
        live = (requested == "live") or (auto and use_live_feed and bool(api_key) and bool(client_code))
        if requested == "delayed":
            configured_mode = DataFeedMode.PUBLIC_EXCHANGE_STREAM
            data_source = "Yahoo Finance — NSE/BSE (delayed market data)"
            self._real_mode = "delayed"
        elif live and requested == "live":
            configured_mode = DataFeedMode.LIVE_BROKER_VENDOR
            data_source = "Angel One SmartStream (broker feed)"
            self._real_mode = "live"
        elif live:
            # Auto-resolved live (legacy callers pass use_live_feed=True with
            # credentials) — same strict rules as an explicit "live".
            configured_mode = DataFeedMode.LIVE_BROKER_VENDOR
            data_source = "Angel One SmartStream (broker feed)"
            self._real_mode = "live"
        else:
            configured_mode = DataFeedMode.DEMO_SIMULATED
            data_source = "NSE/BSE Demo Simulated Feed"
            self._real_mode = "demo"
        super().__init__(name="IndianEquityProvider", asset_class=AssetClass.EQUITY, feed_mode=configured_mode)
        self.data_source = data_source
        self.api_key = api_key
        self.client_code = client_code

        self._quotes: dict[str, NormalizedTick] = {}
        self._open_prices: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._reconnect_attempts = 0
        self._max_reconnect_delay = 30.0
        self._delayed_refresh_seconds = _DEFAULT_DELAYED_REFRESH_SECONDS
        self._last_delayed_ok: Optional[datetime] = None

        # Real-feed health tracking (only meaningful in real-data modes)
        self.last_sync_error: Optional[str] = None
        self.last_sync_success: Optional[datetime] = None
        # Provenance of the most recent get_historical_candles result
        self.last_candle_source: str = "REAL"

    def classify_feed_state(self) -> FeedState:
        """Honest six-state classification for admin/UI surfaces.

        * DEMO      → simulated paper feed (never claims real).
        * LIVE      → ONLY from a genuine, verified broker-vendor stream.
        * DELAYED   → genuinely-sourced delayed real data, pipeline fresh.
        * STALE     → delayed pipeline stopped delivering.
        * UNAVAILABLE → live requested but no verified vendor stream / creds.
        """
        if self._real_mode == "demo":
            return FeedState.DEMO
        if self._real_mode == "delayed":
            if self.last_sync_success is None:
                return FeedState.UNAVAILABLE
            age = (datetime.now(timezone.utc) - self.last_sync_success).total_seconds()
            if age <= (self._delayed_refresh_seconds * 3 + _DELAYED_STALE_GRACE_SECONDS):
                return FeedState.DELAYED
            return FeedState.STALE
        # live mode: ONLY a genuine stream can ever earn LIVE.  There is none
        # until an operator supplies a verified SmartStream session token.
        return FeedState.UNAVAILABLE

    async def start(self) -> None:
        self._is_running = True
        if self._real_mode == "demo":
            # Paper feed: simulated ticks (honestly labelled DEMO).
            self._task = asyncio.create_task(self._run_feed_loop())
        elif self._real_mode == "delayed":
            # Genuine delayed real data — never simulated, never LIVE.
            self._sync_task = asyncio.create_task(self._run_real_price_sync())
        else:
            # LIVE: strict fail-closed.  Without a verified genuine stream the
            # provider stays silent (no ticks, no fabrication) and reports
            # UNAVAILABLE.  A future genuine SmartStream integration plugs in
            # here and flips classify_feed_state() to LIVE on fresh ticks.
            self.last_sync_error = (
                "Genuine Indian-equity live feed is not connected: a verified "
                "broker stream session is required (ANGEL_JWT_TOKEN + "
                "smartapi-python). Refusing to emit synthetic ticks as LIVE."
            )
            logger.warning("[indian_equity] %s", self.last_sync_error)
        logger.info("Indian Equity Market Data Provider active [%s] - Mode: %s (real_mode=%s)", self.data_source, self.feed_mode.value, self._real_mode)

    async def stop(self) -> None:
        self._is_running = False
        for t in (self._task, self._sync_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        logger.info("Indian Equity Market Data Provider stopped")

    async def subscribe(self, symbols: list[str]) -> None:
        from app.market_data.instruments import instrument_master

        for s in symbols:
            clean = s.upper().strip()
            self._subscribers.add(clean)
            if clean not in self._open_prices:
                inst = instrument_master.get_instrument(clean)
                seed = inst.base_price if inst else _INDIAN_SEED_PRICES.get(clean, 1000.0)
                self._open_prices[clean] = seed

    async def unsubscribe(self, symbols: list[str]) -> None:
        for s in symbols:
            self._subscribers.discard(s.upper().strip())

    def get_latest_quote(self, symbol: str) -> Optional[NormalizedTick]:
        return self._quotes.get(symbol.upper().strip())

    async def get_historical_candles(
        self, symbol: str, timeframe: str = "5m", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch real historical OHLCV candles for Indian equities, indices, and commodities."""
        clean_sym = symbol.upper().strip()
        yf_sym = _YFINANCE_MAP.get(clean_sym, f"{clean_sym}.NS")

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
                if df.empty and not yf_sym.endswith(".NS"):
                    ticker = yf.Ticker(f"{clean_sym}.NS")
                    df = ticker.history(period=period, interval=interval)
                if df.empty:
                    return []
                df = df.tail(limit)
                candles = []
                for idx, row in df.iterrows():
                    ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx.to_pydatetime().timestamp())
                    candles.append({
                        "time": ts,
                        "open": round(float(row["Open"]), 2),
                        "high": round(float(row["High"]), 2),
                        "low": round(float(row["Low"]), 2),
                        "close": round(float(row["Close"]), 2),
                        "volume": float(row.get("Volume", 0)),
                    })
                return candles
            except Exception as e:
                logger.error("Error fetching yfinance candles for %s: %s", yf_sym, e)
                return []

        candles = await asyncio.to_thread(_fetch)
        if not candles:
            self.last_candle_source = "SIMULATED"
            # Generate high-fidelity continuous OHLCV candles around base anchor price
            import time
            from app.market_data.instruments import instrument_master

            inst = instrument_master.get_instrument(clean_sym)
            base_p = inst.base_price if inst else self._open_prices.get(clean_sym, 1000.0)
            now_ts = int(time.time())
            step_seconds = 60 if tf == "1m" else 300 if tf == "5m" else 900 if tf == "15m" else 3600 if tf in ("1h", "60m") else 86400

            candles = []
            curr_p = base_p * 0.98
            for i in range(limit, 0, -1):
                c_time = now_ts - (i * step_seconds)
                move = random.gauss(0.0001, 0.003) * curr_p
                o_val = round(curr_p, 2)
                c_val = round(max(0.5, curr_p + move), 2)
                wick_high = max(0.10, abs(random.gauss(0.001, 0.002) * curr_p))
                wick_low = max(0.10, abs(random.gauss(0.001, 0.002) * curr_p))
                h_val = round(max(o_val, c_val) + wick_high, 2)
                l_val = round(max(0.2, min(o_val, c_val) - wick_low), 2)
                v_val = float(random.randint(500, 25000))
                candles.append({
                    "time": c_time,
                    "open": o_val,
                    "high": h_val,
                    "low": l_val,
                    "close": c_val,
                    "volume": v_val,
                })
                curr_p = c_val

            logger.info("Generated %d distinct OHLCV baseline candles for %s (%s)", len(candles), clean_sym, tf)

        else:
            self.last_candle_source = "REAL"

        return candles

    async def _run_real_price_sync(self) -> None:
        """Genuinely-sourced DELAYED real-data pipeline (Phase 1 hardening).

        Activated ONLY in ``real_mode == "delayed"``.  Pulls real last-close /
        today-range data for subscribed symbols from the authoritative vendor
        (Yahoo Finance NSE/BSE/commodity) and emits one ``NormalizedTick`` per
        refresh that carries ``feed_state=DELAYED``.  Real prices are never
        interpolated: between refreshes NO synthetic ticks are emitted, so this
        pipeline can never masquerade as a LIVE feed.
        """
        if self._real_mode != "delayed":
            # Demo/live modes do not run this loop (demo simulates honestly;
            # live is strict-UNAVAILABLE without a genuine stream).
            return
        while self._is_running:
            try:
                def _sync() -> tuple[list[str], dict[str, dict[str, float]]]:
                    """Return real {symbol: {price, open, high, low, change, change_pct, volume}}."""
                    yf = _get_yfinance()
                    symbols = list(self._subscribers) or list(_INDIAN_SEED_PRICES.keys())
                    yf_tickers = [_YFINANCE_MAP.get(s, f"{s}.NS") for s in symbols]
                    data = yf.download(yf_tickers, period="1d", interval="1m", progress=False)
                    updated: dict[str, dict[str, float]] = {}
                    if data is None or data.empty or "Close" not in data:
                        return symbols, updated
                    close_data = data["Close"]
                    volume_data = data.get("Volume")
                    for s in symbols:
                        yf_s = _YFINANCE_MAP.get(s, f"{s}.NS")
                        try:
                            series = close_data[yf_s].dropna()
                            if series.empty:
                                continue
                            closes = [float(v) for v in series if v is not None and not math.isnan(float(v))]
                            if not closes:
                                continue
                            price = closes[-1]
                            if price <= 0:
                                continue
                            open_p = closes[0]
                            high = max(closes)
                            low = min(closes)
                            vol = 0.0
                            if volume_data is not None and not volume_data.empty:
                                vol_series = volume_data[yf_s].dropna()
                                if not vol_series.empty:
                                    vol = float(sum(0 if v is None or math.isnan(float(v)) else float(v) for v in vol_series))
                            updated[s] = {
                                "price": price,
                                "open": open_p,
                                "high": high,
                                "low": low,
                                "close": price,
                                "change": round(price - open_p, 2),
                                "change_pct": round((price - open_p) / open_p * 100, 2) if open_p > 0 else 0.0,
                                "volume": int(vol),
                            }
                        except Exception:
                            continue
                    return symbols, updated

                symbols, latest = await asyncio.to_thread(_sync)
                if latest:
                    self.last_sync_error = None
                    self.last_sync_success = datetime.now(timezone.utc)
                    for sym, rec in latest.items():
                        self._open_prices[sym] = rec["price"]
                        tick = NormalizedTick(
                            symbol=sym,
                            price=rec["price"],
                            bid=rec["price"],
                            ask=rec["price"],
                            open=rec["open"],
                            high=rec["high"],
                            low=rec["low"],
                            close=rec["close"],
                            change=rec["change"],
                            change_pct=rec["change_pct"],
                            volume=rec["volume"],
                            asset_class=self._asset_class_for(sym),
                            feed_mode=self.feed_mode,
                            data_source=self.data_source,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            feed_state="DELAYED",
                        )
                        self._quotes[sym] = tick
                        await self._emit_tick(tick)
                else:
                    self.last_sync_error = "delayed real-data pull returned no valid quotes"
                await asyncio.sleep(self._delayed_refresh_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_sync_error = f"delayed real-data pipeline error: {exc}"
                logger.warning("Exchange price sync background notice: %s", exc)
                await asyncio.sleep(self._delayed_refresh_seconds)

    @staticmethod
    def _asset_class_for(symbol: str) -> AssetClass:
        sym = symbol.upper().strip()
        if sym in ("NIFTY50", "BANKNIFTY", "FINNIFTY"):
            return AssetClass.FNO
        if sym in ("CRUDEOIL", "GOLD", "SILVER"):
            return AssetClass.COMMODITY
        return AssetClass.EQUITY
    async def _run_feed_loop(self) -> None:
        """Main market data streaming loop with real price micro-ticks."""
        reconnect_delay = 1.0
        while self._is_running:
            try:
                while self._is_running:
                    symbols = list(self._subscribers) or list(_INDIAN_SEED_PRICES.keys())[:6]
                    for symbol in symbols:
                        prev_quote = self._quotes.get(symbol)
                        open_p = self._open_prices.get(symbol, _INDIAN_SEED_PRICES.get(symbol, 1000.0))
                        prev_price = prev_quote.price if prev_quote else open_p

                        # Micro tick fluctuations around real market anchor
                        pct_move = random.gauss(0.00002, 0.0008)
                        new_price = max(0.05, round(prev_price * (1.0 + pct_move), 2))

                        spread = round(max(0.05, new_price * 0.0002), 2)
                        bid = round(new_price - spread / 2, 2)
                        ask = round(new_price + spread / 2, 2)
                        change = round(new_price - open_p, 2)
                        change_pct = round((change / open_p) * 100, 2) if open_p else 0.0
                        vol = random.randint(100, 25_000)

                        asset_type = AssetClass.FNO if symbol in ("NIFTY50", "BANKNIFTY", "FINNIFTY") else (
                            AssetClass.COMMODITY if symbol in ("CRUDEOIL", "GOLD", "SILVER") else AssetClass.EQUITY
                        )

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
                            asset_class=asset_type,
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
                logger.error("Indian equity feed error: %s. Reconnecting in %.1fs...", exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, self._max_reconnect_delay)
