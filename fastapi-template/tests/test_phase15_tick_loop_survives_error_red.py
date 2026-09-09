"""Phase 15 RED regression — the engine tick loop must survive per-tick errors.

Defect (P1, resilience): ``app/engine/trading_engine.py`` ``TradingEngine._tick_loop``
only catches ``asyncio.CancelledError``.  ANY other exception raised while
processing a single tick (a transient DB failure in ``_persist_trade``, a
broadcast error, an unexpected strategy bug) propagates out of the loop and
permanently kills the engine's only processing task — ``self._running`` stays
``True`` but no further ticks are ever consumed.  Autonomous strategy
execution silently stops until process restart.

The codebase's own contract for a long-running loop (see
``order_reconciliation._run_pass``: "a per-cycle failure never crashes the
loop", and ``WorkerPool._worker_loop``: exception -> backoff -> continue) is
that a single bad unit of work is contained and logged.  The tick loop must
honour the same contract.

SAFETY: SimulatedBroker only; no real broker/network/payment interaction.
"""

from __future__ import annotations

import asyncio

import pytest

from app.engine.trading_engine import TradingEngine
from app.db.session import init_db


@pytest.mark.asyncio
async def test_tick_loop_survives_transient_persist_failure():
    """A tick whose processing raises must NOT kill the engine loop.

    Tick 1 triggers a PAPER strategy fill whose ``_persist_trade`` raises a
    transient error; tick 2 must still be processed afterwards and the loop
    task must remain alive.
    """
    await init_db()

    tick_queue: asyncio.Queue = asyncio.Queue()
    from app.brokers.simulated import SimulatedBroker

    engine = TradingEngine(broker=SimulatedBroker(), tick_queue=tick_queue)
    await engine.start()  # loads strategies from DB (empty) and starts the loop

    # A custom user strategy watching RELIANCE, always-true condition.
    engine._strategies["s-phase15-survive"] = {
        "id": "s-phase15-survive",
        "user_id": "u-phase15",
        "name": "Phase15 Survival",
        "symbols": ["RELIANCE"],
        "conditions": [{"indicator": "PRICE", "operator": "gte", "value": 100.0}],
        "action": {"side": "BUY", "quantity": 1, "order_type": "MARKET"},
        "enabled": True,
        "execution_mode": "PAPER",
        "broker_account_id": None,
        "capital_allocated": 100000.0,
    }

    # Make the FIRST _persist_trade raise a transient error, then work normally.
    original_persist = engine._persist_trade
    persist_calls = {"n": 0}

    async def flaky_persist(*args, **kwargs):  # noqa: ANN002, ANN003
        persist_calls["n"] += 1
        if persist_calls["n"] == 1:
            raise RuntimeError("simulated transient DB failure (phase15)")
        return await original_persist(*args, **kwargs)

    engine._persist_trade = flaky_persist

    loop_task = engine._task
    assert loop_task is not None and not loop_task.done()

    # Tick 1 -> strategy fires -> _persist_trade raises (transient failure).
    await tick_queue.put({"symbol": "RELIANCE", "price": 200.0})
    await asyncio.sleep(0.3)

    assert persist_calls["n"] >= 1, "tick 1 never reached _persist_trade"
    assert not loop_task.done(), (
        "Phase15 DEFECT: tick loop died after a single transient per-tick error; "
        "the engine will silently stop processing all future ticks"
    )

    # Tick 2 -> must still be processed by the (surviving) loop.
    await tick_queue.put({"symbol": "RELIANCE", "price": 201.0})
    await asyncio.sleep(0.3)

    assert persist_calls["n"] >= 2, (
        "tick 2 was never processed — the loop did not survive tick 1's error"
    )
    assert not loop_task.done(), "tick loop died while processing tick 2"

    # Cleanup: stop the engine gracefully.
    engine._running = False
    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)