"""Honest feed-state classification for every market-data provider.

The six states are exactly the ones the UI must be able to distinguish:

- ``LIVE``         — genuine authenticated / public exchange stream is connected
                    AND delivering fresh ticks with real timestamps.
- ``DELAYED``      — real data, intentionally delayed (e.g. configured CoinGecko
                    REST fallback feeding a non-real-time cadence).
- ``STALE``        — a real feed has not produced a fresh tick inside the
                    freshness window (fail-closed; old data is never presented
                    as current).
- ``DEMO``         — simulated data, honestly labelled; never claimed as live.
- ``MOCK``         — mock/test surface data used only inside automated tests.
- ``UNAVAILABLE``  — no genuine feed is connected (credentials missing, vendor
                    blocked, or reconnect budget exhausted).  Never pretends.
"""

from __future__ import annotations

from enum import Enum

from app.market_data.base import DataFeedMode


class FeedState(str, Enum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    STALE = "STALE"
    DEMO = "DEMO"
    MOCK = "MOCK"
    UNAVAILABLE = "UNAVAILABLE"


# DataFeedMode -> default honest feed state when no other information is known.
_MODE_DEFAULT_STATE: dict[str, FeedState] = {
    DataFeedMode.DEMO_SIMULATED.value: FeedState.DEMO,
    DataFeedMode.LIVE_BROKER_VENDOR.value: FeedState.UNAVAILABLE,  # until proven live
    DataFeedMode.PUBLIC_EXCHANGE_STREAM.value: FeedState.UNAVAILABLE,  # until proven live
}


def default_state_for_feed_mode(feed_mode: str | DataFeedMode | None) -> FeedState:
    """Return the fail-closed default state for a feed mode.

    Real feed modes default to UNAVAILABLE rather than LIVE: a provider only
    earns the LIVE label by genuinely connecting and delivering fresh ticks.
    """
    if feed_mode is None:
        return FeedState.UNAVAILABLE
    if isinstance(feed_mode, DataFeedMode):
        feed_mode = feed_mode.value
    return _MODE_DEFAULT_STATE.get(feed_mode, FeedState.UNAVAILABLE)


def is_genuinely_live_state(feed_state: str | FeedState | None) -> bool:
    """True only for an explicit, genuine LIVE feed state."""
    if feed_state is None:
        return False
    if isinstance(feed_state, FeedState):
        return feed_state is FeedState.LIVE
    return str(feed_state).upper() in (FeedState.LIVE.value,)


def is_real_feed_mode(feed_mode: str | DataFeedMode | None) -> bool:
    """True when the feed mode indicates real (non-simulated) data."""
    if feed_mode is None:
        return False
    if isinstance(feed_mode, DataFeedMode):
        feed_mode = feed_mode.value
    return feed_mode in (
        DataFeedMode.LIVE_BROKER_VENDOR.value,
        DataFeedMode.PUBLIC_EXCHANGE_STREAM.value,
    )