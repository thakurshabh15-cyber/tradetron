"""Phase 15 RED regression — the market-data simulator must survive per-cycle errors.

Defect (P1, resilience): ``app/market_data/simulator.py`` ``MarketSimulator._run``
only caught ``asyncio.CancelledError``.  ANY other exception while generating
one symbol's tick (e.g. a transient WebSocket broadcast failure) propagated out
of the loop and permanently killed the entire simulated market-data stream —
the engine receives no further ticks until process restart.

Same contract as the engine tick loop fix: a single bad unit of work is
contained and logged; the stream continues.

SAFETY: no real feed/network; the ws manager broadcast is patched to raise
exactly once then delegate to the real (clientless) broadcaster.
"""

from __future__ import annotations

import asyncio
import pytest


@pytest.mark.asyncio
async def test_simulator_survives_transient_broadcast_failure(monkeypatch):
    """A broadcast error on one symbol must NOT kill the market-data stream."""
    from app.config import settings
    from app.market_data import manager as md_manager
    from app.market_data.simulator import MarketSimulator

    tick_queue: asyncio.Queue = asyncio.Queue()
    sim = MarketSimulator(tick_queue=tick_queue)

    old_interval = settings.sim_tick_interval
    settings.sim_tick_interval = 0.02  # short cycle for a fast deterministic test
    try:
        broadcast_calls = {"n": 0}
        real_broadcast = md_manager.ws_manager.broadcast

        async def flaky_broadcast(channel, payload):  # noqa: ANN001
            broadcast_calls["n"] += 1
            if broadcast_calls["n"] == 1:
                raise RuntimeError("simulated transient broadcast failure (phase15)")
            return await real_broadcast(channel, payload)

        monkeypatch.setattr(md_manager.ws_manager, "broadcast", flaky_broadcast)

        await sim.start(["RELIANCE"])
        loop_task = sim._task
        assert loop_task is not None and not loop_task.done()

        await asyncio.sleep(0.5)

        assert broadcast_calls["n"] >= 2, (
            "simulator stream died after a single transient broadcast error — "
            "no further ticks are generated"
        )
        assert not loop_task.done(), (
            "simulator loop task died — the market-data stream stops permanently"
        )
    finally:
        settings.sim_tick_interval = old_interval
        await sim.stop()