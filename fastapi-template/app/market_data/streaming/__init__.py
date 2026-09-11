"""Phase 15A — genuine real-time streaming infrastructure.

Extends the existing ``app.market_data`` abstractions (``BaseMarketDataProvider``,
``NormalizedTick``, ``DataFeedMode``) with a vendor-agnostic WebSocket streaming
engine and honest feed-state classification.  No duplicate abstraction for data
feeds: providers built here implement ``BaseMarketDataProvider`` exactly like the
legacy providers.
"""