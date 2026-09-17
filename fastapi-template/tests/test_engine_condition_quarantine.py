"""Engine-level regression tests for the production KeyError('value') incident.

Production (Render) symptom: repeated ``KeyError: 'value'`` from
``TradingEngine._process_tick → StrategyEvaluator.evaluate`` for MSFT/NVDA —
legacy strategy rows whose persisted conditions lacked ``value`` crashed on
every tick.

This suite pins the end-to-end fail-closed behavior:

1. ``_load_strategies`` quarantines malformed rows with structured
   diagnostics and still loads every healthy row (per-row containment).
2. Quarantined strategies never reach the evaluator (no per-tick errors).
3. Corrupt JSON rows are also contained — one bad row can no longer wipe
   out ALL strategies (previous behavior: the whole load aborted).
4. Valid strategies keep evaluating and executing PAPER orders end-to-end.

SAFETY: SimulatedBroker only; PAPER mode; no real broker/network interaction.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base, init_db
from app.engine.trading_engine import TradingEngine
from app.models.trading import StrategyRecord

HEALTHY_ID = "11111111-1111-1111-1111-111111111111"
BROKEN_ID = "22222222-2222-2222-2222-222222222222"
CORRUPT_ID = "33333333-3333-3333-3333-333333333333"


async def _seed_rows(factory) -> None:
    async with factory() as db:
        db.add_all(
            [
                StrategyRecord(
                    id=HEALTHY_ID,
                    user_id=None,
                    name="Healthy PRICE strategy",
                    symbols_json=json.dumps(["MSFT", "NVDA"]),
                    conditions_json=json.dumps(
                        [{"indicator": "PRICE", "operator": "gt", "value": 1.0, "period": 5}]
                    ),
                    action_json=json.dumps(
                        {"side": "BUY", "quantity": 1, "order_type": "MARKET"}
                    ),
                    enabled=True,
                    execution_mode="PAPER",
                ),
                # EXACT production failing shape: no 'value' key at all.
                StrategyRecord(
                    id=BROKEN_ID,
                    user_id=None,
                    name="Legacy no-value strategy",
                    symbols_json=json.dumps(["MSFT", "NVDA"]),
                    conditions_json=json.dumps(
                        [{"indicator": "PRICE", "operator": "gt", "period": 5}]
                    ),
                    action_json=json.dumps(
                        {"side": "BUY", "quantity": 1, "order_type": "MARKET"}
                    ),
                    enabled=True,
                    execution_mode="PAPER",
                ),
                # Corrupt JSON — must also be contained per-row.
                StrategyRecord(
                    id=CORRUPT_ID,
                    user_id=None,
                    name="Corrupt JSON strategy",
                    symbols_json=json.dumps(["MSFT"]),
                    conditions_json="{this is not json",
                    action_json=json.dumps(
                        {"side": "BUY", "quantity": 1, "order_type": "MARKET"}
                    ),
                    enabled=True,
                    execution_mode="PAPER",
                ),
            ]
        )
        await db.commit()


@pytest.fixture
async def engine_env(tmp_path, monkeypatch):
    await init_db()  # ensure Base registry is fully imported/created
    db_file = tmp_path / "engine_quarantine_test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _seed_rows(factory)

    import app.engine.trading_engine as te_module

    monkeypatch.setattr(te_module, "SessionLocal", factory)
    yield factory
    await engine.dispose()


def _make_engine() -> TradingEngine:
    from app.brokers.simulated import SimulatedBroker

    return TradingEngine(broker=SimulatedBroker(), tick_queue=asyncio.Queue())


@pytest.mark.asyncio
async def test_load_strategies_quarantines_broken_rows_keeps_healthy(
    engine_env, caplog
):
    eng = _make_engine()
    with caplog.at_level("ERROR", logger="engine.core"):
        await eng._load_strategies()

    # Broken + corrupt rows quarantined — NEVER loaded into the tick pipeline.
    assert BROKEN_ID not in eng._strategies
    assert CORRUPT_ID not in eng._strategies
    # Healthy row still loads.
    assert HEALTHY_ID in eng._strategies
    healthy = eng._strategies[HEALTHY_ID]
    # Conditions are TYPED rules (not raw dicts).
    from app.engine.conditions import ConditionRule

    assert all(isinstance(c, ConditionRule) for c in healthy["conditions"])

    # Structured diagnostics were logged for both bad rows.
    quarantine_logs = [
        r for r in caplog.records if "QUARANTINED at load" in r.getMessage()
    ]
    assert len(quarantine_logs) >= 2, (
        "expected structured quarantine diagnostics for the broken and "
        "corrupt rows"
    )


@pytest.mark.asyncio
async def test_process_tick_never_raises_for_legacy_broken_row(engine_env):
    """THE production incident: ticks for MSFT/NVDA must never crash the
    tick pipeline again — the broken strategy is simply not evaluated."""
    eng = _make_engine()
    await eng._load_strategies()

    # Process the exact production symbols repeatedly; must not raise.
    for _ in range(3):
        await eng._process_tick("MSFT", 420.0)
        await eng._process_tick("NVDA", 900.0)

    # No evaluation state leaks for the quarantined strategy.
    assert BROKEN_ID not in eng._strategies


@pytest.mark.asyncio
async def test_healthy_strategy_still_fires_after_quarantine(engine_env):
    """A valid strategy on the same page keeps evaluating (regression guard
    against over-broad containment)."""
    eng = _make_engine()
    await eng._load_strategies()
    assert HEALTHY_ID in eng._strategies

    # PRICE > 1.0 with price 420 → fires; cooldown disabled for determinism.
    from app.config import settings

    settings.strategy_signal_cooldown_seconds = 0.0

    fired: list[tuple[str, str, float]] = []

    async def spy_execute(strategy, symbol, price):  # noqa: ANN001
        fired.append((strategy["id"], symbol, price))

    eng._execute_signal = spy_execute  # type: ignore[method-assign]
    await eng._process_tick("MSFT", 420.0)
    assert fired == [(HEALTHY_ID, "MSFT", 420.0)]

