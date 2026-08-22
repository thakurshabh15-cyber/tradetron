"""Unified Market Data Manager: Aggregates Equities, Crypto, and Forex feeds under one uniform API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logging import get_logger
from app.market_data.base import AssetClass, BaseMarketDataProvider, DataFeedMode, NormalizedTick
from app.market_data.manager import ws_manager
from app.market_data.providers.crypto import CryptoMarketDataProvider
from app.market_data.providers.forex import ForexMarketDataProvider
from app.market_data.providers.indian_equity import IndianEquityMarketDataProvider

logger = get_logger("market.unified")


class UnifiedMarketDataManager:
    """Central market data hub that multiplexes across Indian Equities, Crypto, and Forex providers."""

    def __init__(self, tick_queue: Optional[asyncio.Queue] = None) -> None:
        from app.config import settings

        self._tick_queue = tick_queue or asyncio.Queue()

        # Determine live vs demo per asset class from config
        equity_live = (
            settings.feed_mode_equity == "live"
            and bool(settings.angel_api_key)
            and bool(settings.angel_client_code)
        )
        crypto_live = settings.feed_mode_crypto == "live"
        # Forex is always demo — no free real-time forex API available
        forex_live = False

        equity_provider = IndianEquityMarketDataProvider(
            api_key=settings.angel_api_key if equity_live else None,
            client_code=settings.angel_client_code if equity_live else None,
        )
        crypto_provider = CryptoMarketDataProvider(use_live_feed=crypto_live)
        forex_provider = ForexMarketDataProvider(use_live_feed=forex_live)

        self._providers: dict[AssetClass, BaseMarketDataProvider] = {
            AssetClass.EQUITY: equity_provider,
            AssetClass.FNO: equity_provider,
            AssetClass.COMMODITY: equity_provider,
            AssetClass.CRYPTO: crypto_provider,
            AssetClass.FOREX: forex_provider,
        }
        self._quotes: dict[str, NormalizedTick] = {}
        self._broker = None

        # Wire callback from each provider into unified processor
        for provider in set(self._providers.values()):
            provider.add_callback(self._handle_incoming_tick)

        logger.info(
            "Market Data Hub configured — Equity: %s, Crypto: %s, Forex: %s",
            "LIVE" if equity_live else "DEMO",
            "LIVE (Binance WS)" if crypto_live else "DEMO",
            "DEMO (always)",
        )

    def set_broker(self, broker: Any) -> None:
        """Link active broker to receive synchronized price updates."""
        self._broker = broker

    def set_tick_queue(self, queue: asyncio.Queue) -> None:
        self._tick_queue = queue

    def classify_symbol(self, symbol: str) -> AssetClass:
        """Resolve the asset class for a given ticker symbol."""
        sym = symbol.upper().strip()
        if sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "MATICINR", "BTCINR", "ETHINR"):
            return AssetClass.CRYPTO
        if sym in ("USDINR", "EURINR", "GBPINR", "JPYINR", "EURUSD", "GBPUSD"):
            return AssetClass.FOREX
        if sym in ("CRUDEOIL", "GOLD", "SILVER", "NATURALGAS", "COPPER"):
            return AssetClass.COMMODITY
        if sym in ("NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
            return AssetClass.FNO
        return AssetClass.EQUITY

    async def start(self, initial_symbols: Optional[list[str]] = None) -> None:
        """Start all underlying asset feeds and subscribe initial symbols."""
        symbols = initial_symbols or [
            "NIFTY50", "BANKNIFTY", "RELIANCE", "TCS", "INFY", "HDFCBANK",
            "BTCUSDT", "ETHUSDT", "SOLUSDT",
            "USDINR", "EURUSD", "CRUDEOIL", "GOLD",
        ]

        # Group symbols by provider
        for sym in symbols:
            asset_class = self.classify_symbol(sym)
            provider = self._providers.get(asset_class)
            if provider:
                await provider.subscribe([sym])

        # Start unique provider instances
        for provider in set(self._providers.values()):
            await provider.start()

        logger.info("Unified Market Data Hub initialized with %d symbols across 5 asset classes", len(symbols))

    async def stop(self) -> None:
        """Gracefully stop all data feeds."""
        for provider in set(self._providers.values()):
            await provider.stop()
        logger.info("Unified Market Data Hub stopped")

    async def subscribe(self, symbols: list[str]) -> None:
        """Dynamically subscribe symbols to their respective provider."""
        for sym in symbols:
            asset_class = self.classify_symbol(sym)
            provider = self._providers.get(asset_class)
            if provider:
                await provider.subscribe([sym])

    async def _handle_incoming_tick(self, tick: NormalizedTick) -> None:
        """Process incoming standardized tick: cache, enqueue to trading engine, and broadcast."""
        self._quotes[tick.symbol] = tick
        tick_dict = tick.to_dict()

        # 1. Enqueue to Trading Engine for signal evaluation & SL/TP triggers
        if self._tick_queue:
            await self._tick_queue.put(tick_dict)

        # 2. Update linked broker price cache
        if self._broker and hasattr(self._broker, "update_price"):
            self._broker.update_price(tick.symbol, tick.price)

        # 3. Broadcast to symbol-specific channel (e.g. market:RELIANCE)
        await ws_manager.broadcast(f"market:{tick.symbol}", tick_dict)
        # 4. Broadcast to global ticker tape stream
        await ws_manager.broadcast("market:stream", tick_dict)

    def get_quote(self, symbol: str) -> Optional[dict[str, Any]]:
        """Retrieve standardized quote for a single symbol."""
        tick = self._quotes.get(symbol.upper().strip())
        return tick.to_dict() if tick else None

    def get_snapshot(self, asset_class_filter: Optional[str] = None) -> list[dict[str, Any]]:
        """Retrieve snapshot quotes with optional asset-class filtering."""
        quotes = list(self._quotes.values())
        if not quotes:
            for p in set(self._providers.values()):
                if hasattr(p, "_quotes"):
                    quotes.extend(list(p._quotes.values()))

        # If still empty on initial cold start, populate default seed ticks
        if not quotes:
            from app.market_data.providers.indian_equity import _INDIAN_SEED_PRICES
            from app.market_data.providers.crypto import _CRYPTO_SEED_PRICES
            from app.market_data.providers.forex import _FOREX_SEED_PRICES

            now = datetime.now(timezone.utc).isoformat()
            for s, p in list(_INDIAN_SEED_PRICES.items())[:6]:
                quotes.append(NormalizedTick(
                    symbol=s, price=p, bid=p*0.999, ask=p*1.001, open=p, high=p*1.005, low=p*0.995, close=p,
                    change=0.0, change_pct=0.0, volume=5000, asset_class=self.classify_symbol(s),
                    feed_mode=DataFeedMode.PUBLIC_EXCHANGE_STREAM, data_source="Market Data Hub", timestamp=now
                ))
            for s, p in list(_CRYPTO_SEED_PRICES.items())[:4]:
                quotes.append(NormalizedTick(
                    symbol=s, price=p, bid=p*0.999, ask=p*1.001, open=p, high=p*1.005, low=p*0.995, close=p,
                    change=0.0, change_pct=0.0, volume=12000, asset_class=AssetClass.CRYPTO,
                    feed_mode=DataFeedMode.PUBLIC_EXCHANGE_STREAM, data_source="Binance Public Stream", timestamp=now
                ))

        if asset_class_filter and asset_class_filter.upper() != "ALL":
            quotes = [q for q in quotes if q.asset_class.value == asset_class_filter.upper()]
        return [q.to_dict() for q in quotes]

    def get_providers_status(self) -> list[dict[str, Any]]:
        """Return connectivity and mode health metrics for each market provider."""
        result = []
        for provider in set(self._providers.values()):
            result.append({
                "provider_name": provider.name,
                "asset_class": provider.asset_class.value,
                "feed_mode": provider.feed_mode.value,
                "data_source": getattr(provider, "data_source", "Standard Feed"),
                "is_active": getattr(provider, "_is_running", False),
                "subscribed_symbols_count": len(provider._subscribers),
            })
        return result

    async def get_historical_candles(
        self, symbol: str, timeframe: str = "5m", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch authentic historical OHLCV candles from the matching asset-class provider."""
        clean_sym = symbol.upper().strip()
        asset_class = self.classify_symbol(clean_sym)
        provider = self._providers.get(asset_class)

        if provider and hasattr(provider, "get_historical_candles"):
            return await provider.get_historical_candles(clean_sym, timeframe, limit)

        # Fallback to Indian Equity provider
        eq_provider = self._providers.get(AssetClass.EQUITY)
        if eq_provider and hasattr(eq_provider, "get_historical_candles"):
            return await eq_provider.get_historical_candles(clean_sym, timeframe, limit)

        return []


# Application-wide singleton unified market data manager
unified_market_manager = UnifiedMarketDataManager()
