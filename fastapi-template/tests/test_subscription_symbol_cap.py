"""Regression: dynamic market-data subscriptions are bounded (memory safety).

A long-running process must never accumulate per-symbol subscription slots or
quote-cache entries for every historically-seen symbol.  The unified hub caps
the distinct subscribed universe (fail-closed, no partial state) and prunes the
presentational quote cache to the still-subscribed universe on every successful
subscribe.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.market_data.unified_manager import (
    SubscriptionLimitError,
    unified_market_manager,
)


def _snapshot_subscriber_sets() -> dict[int, set]:
    return {
        id(p): set(getattr(p, "_subscribers", set()) or set())
        for p in set(unified_market_manager._providers.values())
    }


def _restore_subscriber_sets(snapshot: dict[int, set]) -> None:
    for provider in set(unified_market_manager._providers.values()):
        subscribers = getattr(provider, "_subscribers", None)
        if subscribers is not None:
            subscribers.clear()
            subscribers.update(snapshot.get(id(provider), ()))


@pytest.mark.asyncio
async def test_subscribe_rejects_beyond_cap_without_partial_state(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_subscribed_symbols", 5)
    snapshot = _snapshot_subscriber_sets()
    prior_quotes = dict(unified_market_manager._quotes)
    try:
        symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        with pytest.raises(SubscriptionLimitError):
            await unified_market_manager.subscribe(symbols)
        # Fail closed: NOTHING may be half-subscribed by a rejected request.
        assert _snapshot_subscriber_sets() == snapshot
    finally:
        _restore_subscriber_sets(snapshot)
        unified_market_manager._quotes.clear()
        unified_market_manager._quotes.update(prior_quotes)


@pytest.mark.asyncio
async def test_quote_cache_pruned_to_subscribed_universe(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_subscribed_symbols", 50)
    snapshot = _snapshot_subscriber_sets()
    prior_quotes = dict(unified_market_manager._quotes)
    try:
        # A quote for a symbol no provider subscribes to is stale by definition
        # and must be dropped from the presentational cache once the next
        # subscribe happens (it would otherwise accumulate forever).
        unified_market_manager._quotes["GHOST"] = object()  # type: ignore[arg-type]
        await unified_market_manager.subscribe(["RELIANCE", "TCS"])
        assert "GHOST" not in unified_market_manager._quotes
        equity = next(
            p for p in set(unified_market_manager._providers.values())
            if getattr(p, "asset_class", None) is not None
            and getattr(p, "asset_class", None).value == "EQUITY"  # type: ignore[union-attr]
        )
        assert "RELIANCE" in equity._subscribers
        assert "TCS" in equity._subscribers
    finally:
        _restore_subscriber_sets(snapshot)
        unified_market_manager._quotes.clear()
        unified_market_manager._quotes.update(prior_quotes)


def test_subscribe_endpoint_returns_429_when_cap_exceeded(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_subscribed_symbols", 3)
    snapshot = _snapshot_subscriber_sets()
    try:
        client = TestClient(app)
        res = client.post(
            "/api/market-data/subscribe",
            json={"symbols": ["AAA", "BBB", "CCC", "DDD", "EEE"]},
        )
        assert res.status_code == 429
        assert "Subscription limit reached" in res.json()["detail"]
    finally:
        _restore_subscriber_sets(snapshot)