"""PHASE4 §8.2 defense-in-depth: live-dispatch gate on the latent visual-strategy primitive.

``app/engine/visual_strategy.py`` exposes a raw dispatch primitive that accepts
an arbitrary broker client and calls ``broker.place_order(...)`` per configured
leg.  It currently has ZERO callers (pinned by
``tests/test_live_routing_uncovered_paths.py``), but as a broker-dispatch
primitive it must carry the SAME fail-fast gate as every other real-broker
dispatch path (@PHASE4_REPORT §8 recommendations #2, mirroring the P2-10
adapter gates): while ``BROKER_MODE != live`` the broker must NEVER be touched,
even if a future caller forgets the guard.

- RED: today the method walks straight to ``await broker.place_order(...)``
  with no ``BROKER_MODE`` check, so a direct invocation would reach the broker
  in simulated mode.
- GREEN: ``assert_live_dispatch_allowed()`` fires first and raises
  ``BrokerModeBlockedError``; ``live`` mode passes through unchanged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.brokers import BrokerModeBlockedError
from app.config import settings
from app.engine.visual_strategy import VisualStrategyEngine

# Two configured option legs: a BUY CE (1 lot) and a SELL PE (2 lots).
LEGS = [
    {"strike": 25000, "type": "CE", "action": "BUY", "lots": 1},
    {"strike": 25000, "type": "PE", "action": "SELL", "lots": 2},
]


@pytest.mark.asyncio
async def test_latent_dispatch_blocked_before_any_broker_call_in_simulated_mode(monkeypatch):
    """The gate must fire BEFORE the first ``broker.place_order``.

    In simulated mode a direct class-level invocation must raise
    ``BrokerModeBlockedError`` ("BROKER_MODE") and the broker must never be
    awaited — proving the guard sits ahead of the dispatch loop itself, not
    merely ahead of a routed call path.
    """
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = AsyncMock()
    # A well-behaved broker that would complete both legs cleanly IF the loop
    # were ever reached — so the ONLY valid failure mode is the guard firing.
    broker.place_order.side_effect = [
        {"broker_order_id": "L1", "filled_price": 55.0},
        {"broker_order_id": "L2", "filled_price": 40.5},
    ]
    engine = VisualStrategyEngine()

    with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
        await engine.execute_legs(broker, underlying="NIFTY", legs=LEGS)

    broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_latent_dispatch_passes_through_and_returns_fills_in_live_mode(monkeypatch):
    """Live mode must pass through the gate and return normalized fills.

    The gate is fail-fast only for the wrong deployment mode; it must NOT
    disable the primitive for a deliberate ``BROKER_MODE=live`` caller
    (placeholder-stub broker — fully offline, no network).
    """
    monkeypatch.setattr(settings, "broker_mode", "live")
    broker = AsyncMock()
    broker.place_order.side_effect = [
        {"broker_order_id": "L1", "filled_price": 55.0},
        {"broker_order_id": "L2", "filled_price": 40.5},
    ]
    engine = VisualStrategyEngine()

    fills = await engine.execute_legs(broker, underlying="NIFTY", legs=LEGS)

    assert broker.place_order.await_count == 2
    assert fills[0]["symbol"] == "NIFTY-25000-CE"
    assert fills[0]["action"] == "BUY"
    assert fills[0]["quantity"] == 1
    assert fills[0]["broker_order_id"] == "L1"
    assert fills[1]["symbol"] == "NIFTY-25000-PE"
    assert fills[1]["action"] == "SELL"
    assert fills[1]["quantity"] == 2
    assert fills[1]["broker_order_id"] == "L2"