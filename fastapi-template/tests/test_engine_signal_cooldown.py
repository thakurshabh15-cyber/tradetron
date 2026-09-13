"""Regression: the strategy-signal cooldown bounds repeated engine dispatch.

Defect (P0/P4): :meth:`TradingEngine._process_tick <app.engine.trading_engine.TradingEngine._process_tick>`
evaluated every enabled strategy on every incoming tick.  A persistently
satisfied threshold condition (e.g. ``RSI < 30``, ``price < lower Bollinger
band``) therefore dispatched AND persisted an order on EVERY tick.  With the
seeded enabled PAPER strategies that is roughly ``strategies x symbols``
order rows per second — fills plus risk-blocked REJECTED rows — i.e. ~300
rows/minute of unbounded orders-table growth on a 24/7 deployment.

Fix: a per-(strategy, symbol) cooldown gate before ``_execute_signal``
(``settings.strategy_signal_cooldown_seconds``, default 60s), with the map
pruned on strategy reload so it stays bounded in memory.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.engine.trading_engine import TradingEngine


def _strategy(sid: str, symbols: list[str]) -> dict:
    return {
        "id": sid,
        "user_id": None,
        "name": f"Test {sid}",
        "symbols": symbols,
        "conditions": [{"indicator": "PRICE", "operator": "gte", "value": 100.0}],
        "action": {"side": "BUY", "quantity": 1, "order_type": "MARKET"},
        "enabled": True,
        "execution_mode": "PAPER",
        "broker_account_id": None,
        "capital_allocated": 100000.0,
    }


@pytest.mark.asyncio
async def test_signal_cooldown_coalesces_repeated_ticks(monkeypatch):
    """A still-satisfied condition must NOT re-dispatch on every tick."""
    from app.brokers.simulated import SimulatedBroker

    monkeypatch.setattr(settings, "strategy_signal_cooldown_seconds", 60.0)
    engine = TradingEngine(broker=SimulatedBroker(), tick_queue=asyncio.Queue())
    engine._strategies["s-cooldown"] = _strategy("s-cooldown", ["RELIANCE", "AAPL"])

    calls = {"n": 0}

    async def spy(strategy, symbol, price):  # noqa: ANN001, ANN002, ANN003
        calls["n"] += 1

    engine._execute_signal = spy  # type: ignore[method-assign]

    await engine._process_tick("RELIANCE", 200.0)
    assert calls["n"] == 1, f"first tick must dispatch once, got {calls['n']}"

    # Same strategy + symbol, condition still satisfied -> throttled.
    await engine._process_tick("RELIANCE", 201.0)
    await engine._process_tick("RELIANCE", 202.0)
    assert calls["n"] == 1, f"cooldown must coalesce repeated ticks, got {calls['n']}"

    # A DIFFERENT symbol of the same strategy is its own key -> allowed.
    await engine._process_tick("AAPL", 300.0)
    assert calls["n"] == 2, f"different symbol must not share the cooldown key, got {calls['n']}"


@pytest.mark.asyncio
async def test_signal_cooldown_expires_and_refires(monkeypatch):
    """After the window elapses the same strategy+symbol may fire again."""
    import time

    from app.brokers.simulated import SimulatedBroker

    monkeypatch.setattr(settings, "strategy_signal_cooldown_seconds", 60.0)
    engine = TradingEngine(broker=SimulatedBroker(), tick_queue=asyncio.Queue())
    engine._strategies["s-expire"] = _strategy("s-expire", ["RELIANCE"])

    calls = {"n": 0}

    async def spy(strategy, symbol, price):  # noqa: ANN001, ANN002, ANN003
        calls["n"] += 1

    engine._execute_signal = spy  # type: ignore[method-assign]

    await engine._process_tick("RELIANCE", 200.0)
    assert calls["n"] == 1
    await engine._process_tick("RELIANCE", 201.0)
    assert calls["n"] == 1, "still inside the cooldown window"

    # Simulate the window elapsing; the same pair fires once more.
    engine._last_strategy_signal_ts[("s-expire", "RELIANCE")] = (
        time.monotonic() - 61.0
    )
    await engine._process_tick("RELIANCE", 202.0)
    assert calls["n"] == 2, "after cooldown expiry the pair may re-dispatch"


@pytest.mark.asyncio
async def test_signal_cooldown_map_pruned_on_reload(monkeypatch):
    """Reloading strategies drops cooldown keys for strategies no longer active."""
    from app.brokers.simulated import SimulatedBroker
    from app.db.session import init_db

    monkeypatch.setattr(settings, "strategy_signal_cooldown_seconds", 60.0)
    await init_db()
    engine = TradingEngine(broker=SimulatedBroker(), tick_queue=asyncio.Queue())

    # Stale entries from strategies that are neither loaded nor enabled.
    engine._last_strategy_signal_ts = {
        ("stale-deleted", "RELIANCE"): 123.0,
        ("stale-disabled", "AAPL"): 456.0,
    }

    await engine._load_strategies()

    assert engine._last_strategy_signal_ts == {}, (
        f"reload must prune stale cooldown keys, got {engine._last_strategy_signal_ts}"
    )