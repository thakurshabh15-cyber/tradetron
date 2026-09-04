"""Crypto Market Data Provider — CoinGecko Public REST API + Demo Fallback.

When feed_mode_crypto=live: Polls CoinGecko's free public REST API every ~30 s
for real-time crypto prices.  No API key required.

  GET https://api.coingecko.com/api/v3/simple/price
      ?ids=bitcoin,ethereum,solana,binancecoin,ripple
      &vs_currencies=usd&include_24hr_vol=true&include_24hr_change=true
      &include_last_updated_at=true

CoinGecko free-tier rate limit ≈ 10-30 req/min; we poll at 30 s intervals
(≈ 2 req/min), well within limits.

When feed_mode_crypto=demo: Generates simulated ticks clearly labeled
DEMO_SIMULATED.

NOTE: The previous Binance WebSocket + REST implementation was removed because
Binance blocks Render infrastructure with HTTP 451 ("Unavailable For Legal
Reasons").  CoinGecko has no such restrictions and works from all cloud
providers without credentials.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.core.logging import get_logger
from app.market_data.base import AssetClass, BaseMarketDataProvider, DataFeedMode, NormalizedTick

logger = get_logger("market.crypto")

# ── CoinGecko symbol mapping ────────────────────────────────────────────────
# CoinGecko uses its own IDs (lowercase) — we map our USDT-denominated symbols
# to the CoinGecko id + the fiat/vs currency we want.
_COINGECKO_MAP: dict[str, dict[str, str]] = {
    "BTCUSDT":  {"id": "bitcoin",      "vs": "usd"},
    "ETHUSDT":  {"id": "ethereum",     "vs": "usd"},
    "SOLUSDT":  {"id": "solana",       "vs": "usd"},
    "BNBUSDT":  {"id": "binancecoin",  "vs": "usd"},
    "XRPUSDT":  {"id": "ripple",       "vs": "usd"},
    "BTCINR":   {"id": "bitcoin",      "vs": "inr"},
    "ETHINR":   {"id": "ethereum",     "vs": "inr"},
    "MATICINR": {"id": "matic-network", "vs": "inr"},
}

# Approximate spread (fraction) per symbol — CoinGecko does not provide bid/ask
_SPREAD: dict[str, float] = {
    "BTCUSDT": 0.0001,
    "ETHUSDT": 0.0002,
    "SOLUSDT": 0.0004,
    "BNBUSDT": 0.0003,
    "XRPUSDT": 0.0005,
    "BTCINR":  0.0002,
    "ETHINR":  0.0003,
    "MATICINR": 0.0006,
}

# ── CoinGecko API constants ──────────────────────────────────────────────────
_COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Polling interval in seconds.  CoinGecko free-tier ≈ 10-30 req/min;
# at 30 s intervals we make ≈ 2 req/min, well within limits.
_POLL_INTERVAL = 30.0

# Approximate USD/INR seed prices for demo mode and cold-start fallback.
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


class CryptoMarketDataProvider(BaseMarketDataProvider):
    """Real-time Crypto provider backed by CoinGecko public REST API + demo fallback."""

    def __init__(self, use_live_feed: bool = False) -> None:
        feed_mode = (
            DataFeedMode.PUBLIC_EXCHANGE_STREAM if use_live_feed
            else DataFeedMode.DEMO_SIMULATED
        )
        data_source = (
            "CoinGecko Public API (Live)" if use_live_feed
            else "Crypto Market Stream (Demo Simulated)"
        )
        super().__init__(
            name="CryptoMarketProvider",
            asset_class=AssetClass.CRYPTO,
            feed_mode=feed_mode,
        )
        self.data_source = data_source
        self._use_live = use_live_feed

        self._quotes: dict[str, NormalizedTick] = {}
        self._open_prices: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self.last_sync_error: Optional[str] = None
        self.last_sync_success: Optional[datetime] = None
        self._max_reconnect_delay = 30.0

    async def start(self) -> None:
        self._is_running = True
        if self._use_live:
            self._task = asyncio.create_task(self._run_live_poll())
        else:
            self._task = asyncio.create_task(self._run_demo_stream())
        logger.info(
            "Crypto Market Data Provider active [%s] - Mode: %s",
            self.data_source, self.feed_mode.value,
        )

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
        """Fetch OHLCV candles from CoinGecko public API.

        CoinGecko ``/coins/{id}/ohlc`` returns arrays of
        ``[timestamp_ms, open, high, low, close]``.  Volume is not provided
        by this endpoint and is set to 0.
        """
        clean_sym = symbol.upper().strip()
        mapping = _COINGECKO_MAP.get(clean_sym)
        if mapping is None:
            logger.warning("No CoinGecko mapping for %s — cannot fetch candles", clean_sym)
            return []

        coin_id = mapping["id"]
        vs = mapping["vs"]

        # CoinGecko OHLC: days param — 1 = 30-min candles, 7 = 4h, 30+ = daily
        url = f"{_COINGECKO_BASE}/coins/{coin_id}/ohlc?vs_currency={vs}&days=1"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    raw = resp.json()
                    candles: list[dict[str, Any]] = []
                    for entry in raw:
                        if not isinstance(entry, list) or len(entry) < 5:
                            continue
                        candles.append({
                            "time": int(entry[0] / 1000),
                            "open": float(entry[1]),
                            "high": float(entry[2]),
                            "low": float(entry[3]),
                            "close": float(entry[4]),
                            "volume": 0,
                        })
                    logger.info(
                        "Fetched %d CoinGecko candles for %s (%s)",
                        len(candles), clean_sym, vs,
                    )
                    return candles
                else:
                    logger.warning(
                        "CoinGecko OHLC returned %d for %s: %s",
                        resp.status_code, coin_id, resp.text[:200],
                    )
        except Exception as exc:
            logger.error("CoinGecko OHLC fetch failed for %s: %s", coin_id, exc)

        return []

    # ── LIVE: CoinGecko REST polling loop ───────────────────────────────────

    async def _run_live_poll(self) -> None:
        """Poll CoinGecko ``simple/price`` for subscribed symbols.

        Builds a batch request for all subscribed symbols in a single HTTP call.
        On failure, back off exponentially up to ``_max_reconnect_delay`` seconds.
        On success, reset the backoff.
        """
        reconnect_delay = 1.0

        while self._is_running:
            try:
                await self._poll_coingecko_prices()
                self.last_sync_success = datetime.now(timezone.utc)
                self.last_sync_error = None
                reconnect_delay = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_sync_error = str(exc)[:200]
                logger.error(
                    "CoinGecko live poll error: %s. Reconnecting in %.1fs...",
                    exc, reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self._max_reconnect_delay)
                continue

            await asyncio.sleep(_POLL_INTERVAL)

    async def _poll_coingecko_prices(self) -> None:
        """Single CoinGecko batch-price request for all subscribed symbols."""
        # Separate symbols into CoinGecko-supported and unmapped
        cg_ids: dict[str, str] = {}  # coingecko_id -> our symbol
        unmapped: list[str] = []

        for sym in list(self._subscribers):
            mapping = _COINGECKO_MAP.get(sym)
            if mapping:
                cg_ids[mapping["id"]] = sym
            else:
                unmapped.append(sym)

        if not cg_ids:
            if not self._quotes:
                await self._emit_demo_for_symbols(list(self._subscribers) or ["BTCUSDT"])
            return

        # Group by vs_currency for separate API calls
        vs_groups: dict[str, list[str]] = {}
        for cg_id, sym in cg_ids.items():
            vs = _COINGECKO_MAP[sym]["vs"]
            vs_groups.setdefault(vs, []).append(cg_id)

        all_prices: dict[str, dict[str, Any]] = {}

        for vs_currency, coin_ids in vs_groups.items():
            ids_str = ",".join(coin_ids)
            url = (
                f"{_COINGECKO_BASE}/simple/price"
                f"?ids={ids_str}"
                f"&vs_currencies={vs_currency}"
                f"&include_24hr_vol=true"
                f"&include_24hr_change=true"
                f"&include_last_updated_at=true"
            )
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                all_prices.update(data)

        # Process each CoinGecko result into a NormalizedTick
        for cg_id, sym in cg_ids.items():
            coin_data = all_prices.get(cg_id)
            if coin_data is None:
                logger.debug("CoinGecko returned no data for %s (%s)", cg_id, sym)
                continue

            vs = _COINGECKO_MAP[sym]["vs"]
            price = coin_data.get(vs)
            if price is None:
                continue

            volume_24h = coin_data.get(f"{vs}_24h_vol", 0)
            change_pct_24h = coin_data.get(f"{vs}_24h_change", 0.0)
            updated_at = coin_data.get("last_updated_at", int(time.time()))

            open_price = price / (1 + change_pct_24h / 100) if change_pct_24h else price
            spread_frac = _SPREAD.get(sym, 0.0005)
            spread = price * spread_frac
            bid = price - spread / 2
            ask = price + spread / 2
            change = price - open_price

            try:
                ts = datetime.fromtimestamp(updated_at, tz=timezone.utc).isoformat()
            except (OSError, ValueError):
                ts = datetime.now(timezone.utc).isoformat()

            tick = NormalizedTick(
                symbol=sym,
                price=round(price, 4) if price < 1 else round(price, 2),
                bid=round(bid, 4) if bid < 1 else round(bid, 2),
                ask=round(ask, 4) if ask < 1 else round(ask, 2),
                open=round(open_price, 4) if open_price < 1 else round(open_price, 2),
                high=round(price * 1.001, 4) if price < 1 else round(price * 1.001, 2),
                low=round(price * 0.999, 4) if price < 1 else round(price * 0.999, 2),
                close=round(price, 4) if price < 1 else round(price, 2),
                change=round(change, 4) if abs(change) < 1 else round(change, 2),
                change_pct=round(change_pct_24h, 2),
                volume=int(volume_24h) if volume_24h else 0,
                asset_class=AssetClass.CRYPTO,
                feed_mode=DataFeedMode.PUBLIC_EXCHANGE_STREAM,
                data_source="CoinGecko Public API (Live)",
                timestamp=ts,
            )

            self._quotes[sym] = tick
            await self._emit_tick(tick)

        if unmapped:
            await self._emit_demo_for_symbols(unmapped)

    # ── DEMO helper: single tick per symbol ─────────────────────────────────

    async def _emit_demo_for_symbols(self, symbols: list[str]) -> None:
        """Generate one demo tick per listed symbol (used for unmapped pairs)."""
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
