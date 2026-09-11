"""Shared helpers for tests that exercise the LIVE strategy execution path.

The Phase 15A realtime feed gate (`TradingEngine._feed_gate_for_live`) blocks
LIVE execution unless the symbol has a fresh, genuinely-live quote in the
unified manager cache.  Tests that intend to exercise gates *after* the feed
gate (margin, token expiry, broker-mode, durable claims, idempotency) must seed
a fresh live quote first so the feed gate passes.
"""

from datetime import datetime, timezone

from app.market_data.base import AssetClass, DataFeedMode, NormalizedTick


def seed_live_quote(symbol: str = "RELIANCE", price: float = 2500.0) -> None:
    """Place a fresh, non-stale, genuine vendor quote for ``symbol`` in the
    unified manager cache so the LIVE feed gate passes."""
    from app.market_data.unified_manager import unified_market_manager

    unified_market_manager._quotes[symbol] = NormalizedTick(
        symbol=symbol,
        price=price,
        bid=price - 0.05,
        ask=price + 0.05,
        open=price,
        high=round(price * 1.01, 2),
        low=round(price * 0.99, 2),
        close=price,
        change=0.0,
        change_pct=0.0,
        volume=1000,
        asset_class=AssetClass.EQUITY,
        feed_mode=DataFeedMode.LIVE_BROKER_VENDOR,
        data_source="NSE (Live Broker Feed)",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )