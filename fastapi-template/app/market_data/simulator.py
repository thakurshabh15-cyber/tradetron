"""Simulated market data generator.

Uses Geometric Brownian Motion (GBM) to produce realistic price ticks.
Each tick is:
  1. Fed into the trading engine's ``asyncio.Queue``
  2. Broadcast to all WebSocket subscribers on ``market:{symbol}``
  3. Used to update the simulated broker's price cache

The simulator runs as a background ``asyncio.Task`` tied to the app lifespan.
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timezone
from decimal import Decimal

from app.config import settings
from app.core.logging import get_logger
from app.market_data.manager import ws_manager

logger = get_logger("market.simulator")

# Seed prices for common symbols
_SEED_PRICES: dict[str, float] = {
    "AAPL": 225.32,
    "MSFT": 421.18,
    "NVDA": 128.44,
    "GOOGL": 178.92,
    "AMZN": 193.15,
    "META": 512.60,
    "TSLA": 248.75,
    "RELIANCE": 2945.50,
    "TCS": 3812.30,
    "INFY": 1567.80,
}

# Annualised volatility per symbol (higher = more movement)
_VOLATILITY: dict[str, float] = {
    "AAPL": 0.25,
    "MSFT": 0.22,
    "NVDA": 0.45,
    "GOOGL": 0.28,
    "AMZN": 0.30,
    "META": 0.35,
    "TSLA": 0.55,
    "RELIANCE": 0.20,
    "TCS": 0.18,
    "INFY": 0.22,
}


class MarketSimulator:
    """Generates realistic simulated price ticks using GBM."""

    def __init__(self, tick_queue: asyncio.Queue) -> None:
        self._tick_queue = tick_queue
        self._prices: dict[str, float] = {}
        self._open_prices: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._broker = None  # Set externally so prices sync

    def set_broker(self, broker) -> None:  # type: ignore[type-arg]
        """Link the simulated broker so it receives price updates."""
        self._broker = broker

    def _init_prices(self, symbols: list[str]) -> None:
        """Initialise seed prices, using defaults for unknown symbols."""
        for sym in symbols:
            price = _SEED_PRICES.get(sym, 100.0 + random.uniform(-20, 80))
            self._prices[sym] = price
            self._open_prices[sym] = price

    def _next_price(self, symbol: str) -> float:
        """Generate next price using Geometric Brownian Motion."""
        dt = settings.sim_tick_interval / (252 * 6.5 * 3600)  # fraction of trading year
        vol = _VOLATILITY.get(symbol, 0.25)
        drift = 0.0001  # slight upward drift

        price = self._prices[symbol]
        z = random.gauss(0, 1)
        price *= math.exp((drift - 0.5 * vol**2) * dt + vol * math.sqrt(dt) * z)

        # Clamp to avoid absurd values
        price = max(price, 0.01)
        self._prices[symbol] = round(price, 2)
        return self._prices[symbol]

    async def start(self, symbols: list[str]) -> None:
        """Begin generating ticks in a background coroutine."""
        self._init_prices(symbols)
        self._task = asyncio.create_task(self._run(symbols))
        logger.info("Market simulator started for %s", symbols)

    async def stop(self) -> None:
        """Cancel the background tick generator."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Market simulator stopped")

    async def _run(self, symbols: list[str]) -> None:
        """Main tick generation loop."""
        try:
            while True:
                for symbol in symbols:
                    price = self._next_price(symbol)
                    open_price = self._open_prices[symbol]
                    change = round(price - open_price, 2)
                    change_pct = round((change / open_price) * 100, 2) if open_price else 0
                    now = datetime.now(timezone.utc)

                    tick = {
                        "symbol": symbol,
                        "price": price,
                        "change": change,
                        "change_pct": change_pct,
                        "volume": random.randint(100, 50_000),
                        "timestamp": now.isoformat(),
                    }

                    # Feed into the engine
                    await self._tick_queue.put(tick)

                    # Update simulated broker price
                    if self._broker and hasattr(self._broker, "update_price"):
                        self._broker.update_price(symbol, price)

                    # Broadcast to WebSocket subscribers
                    await ws_manager.broadcast(f"market:{symbol}", tick)

                await asyncio.sleep(settings.sim_tick_interval)
        except asyncio.CancelledError:
            logger.debug("Simulator tick loop cancelled")
            raise

    def get_snapshot(self) -> list[dict]:
        """Return current prices for all tracked symbols (REST endpoint)."""
        now = datetime.now(timezone.utc).isoformat()
        result = []
        for sym, price in self._prices.items():
            open_p = self._open_prices.get(sym, price)
            change = round(price - open_p, 2)
            change_pct = round((change / open_p) * 100, 2) if open_p else 0
            result.append({
                "symbol": sym,
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "volume": random.randint(100, 50_000),
                "timestamp": now,
            })
        return result
