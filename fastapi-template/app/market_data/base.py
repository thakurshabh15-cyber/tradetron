"""Unified Market Data Interfaces, Models, and Asset Class Definitions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Optional


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    FNO = "FNO"
    CRYPTO = "CRYPTO"
    FOREX = "FOREX"
    COMMODITY = "COMMODITY"


class DataFeedMode(str, Enum):
    LIVE_BROKER_VENDOR = "LIVE_BROKER_VENDOR"
    PUBLIC_EXCHANGE_STREAM = "PUBLIC_EXCHANGE_STREAM"
    DEMO_SIMULATED = "DEMO_SIMULATED"


@dataclass
class NormalizedTick:
    """Standardized price tick model across Indian Equities, Crypto, and Forex."""

    symbol: str
    price: float
    bid: float
    ask: float
    open: float
    high: float
    low: float
    close: float
    change: float
    change_pct: float
    volume: int
    asset_class: AssetClass
    feed_mode: DataFeedMode
    data_source: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Age of this tick in seconds since its reported timestamp (UTC).

        Returns ``None`` when the timestamp cannot be parsed so callers can
        treat unparsable timestamps as opaque rather than guessing freshness.
        """
        now = now or datetime.now(timezone.utc)
        try:
            ts = datetime.fromisoformat(self.timestamp)
        except (TypeError, ValueError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds()

    def is_stale(self, max_age_seconds: float, now: Optional[datetime] = None) -> bool:
        """Return True when the tick is older than ``max_age_seconds``.

        A tick with an unparsable timestamp is conservatively treated as stale
        (fail closed) so a broken/absent timestamp is never presented as live.
        """
        age = self.age_seconds(now)
        if age is None:
            return True
        return age > max_age_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": round(self.price, 2) if self.price > 1 else round(self.price, 4),
            "bid": round(self.bid, 2) if self.bid > 1 else round(self.bid, 4),
            "ask": round(self.ask, 2) if self.ask > 1 else round(self.ask, 4),
            "open": round(self.open, 2) if self.open > 1 else round(self.open, 4),
            "high": round(self.high, 2) if self.high > 1 else round(self.high, 4),
            "low": round(self.low, 2) if self.low > 1 else round(self.low, 4),
            "close": round(self.close, 2) if self.close > 1 else round(self.close, 4),
            "change": round(self.change, 2) if abs(self.change) > 0.01 else round(self.change, 4),
            "change_pct": round(self.change_pct, 2),
            "volume": self.volume,
            "asset_class": self.asset_class.value,
            "feed_mode": self.feed_mode.value,
            "data_source": self.data_source,
            "timestamp": self.timestamp,
        }


TickCallback = Callable[[NormalizedTick], Coroutine[Any, Any, None]]


class BaseMarketDataProvider(ABC):
    """Abstract interface that all asset-class data providers must implement."""

    def __init__(self, name: str, asset_class: AssetClass, feed_mode: DataFeedMode) -> None:
        self.name = name
        self.asset_class = asset_class
        self.feed_mode = feed_mode
        self._subscribers: set[str] = set()
        self._callbacks: list[TickCallback] = []
        self._is_running = False

    def add_callback(self, callback: TickCallback) -> None:
        """Register a callback invoked whenever a normalized tick arrives."""
        self._callbacks.append(callback)

    async def _emit_tick(self, tick: NormalizedTick) -> None:
        """Broadcast normalized tick to registered engine/websocket handlers."""
        for cb in self._callbacks:
            try:
                await cb(tick)
            except Exception:
                pass

    @abstractmethod
    async def start(self) -> None:
        """Connect to vendor / exchange WebSocket with reconnect/backoff."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect and clean up resources."""
        pass

    @abstractmethod
    async def subscribe(self, symbols: list[str]) -> None:
        """Add symbols to active streaming subscription."""
        pass

    @abstractmethod
    async def unsubscribe(self, symbols: list[str]) -> None:
        """Remove symbols from active subscription."""
        pass

    @abstractmethod
    def get_latest_quote(self, symbol: str) -> Optional[NormalizedTick]:
        """Fetch the most recent cached quote for a symbol."""
        pass
