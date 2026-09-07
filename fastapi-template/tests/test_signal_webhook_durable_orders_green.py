"""P0-2 GREEN regression suite (extension of the RED file).

The two RED tests (test_signal_webhook_durable_orders_red.py) pin the core
invariants:
  A. a valid entry signal MUST create a durable OrderRecord;
  B. duplicate delivery MUST NOT create duplicate durable orders.

This file adds the remaining approved coverage with real DB sessions:

  C. concurrent duplicate delivery -> exactly one durable order + one dispatch
  D. process/restart-style loss of in-memory OrderManager state
  E. persistence failure before acknowledgement -> handler raises (fail closed)
  F. crash window between durable claim and dispatch
  G. DB state remains authoritative over the in-memory manager
  H. tenant-less signal key cannot create unsafe cross-tenant collisions
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.brokers.simulated import SimulatedBroker
from app.db.session import SessionLocal, init_db
from app.engine.order_manager import OrderManager
from app.models.audit import TradeAuditRecord
from app.models.trading import OrderRecord, TradeRecord
from app.webhooks.handlers.tradethrone_signal import handle_tradethrone_signal
from app.webhooks.queue.redis_streams import QueuedWebhook
from app.webhooks.validation.schemas import WebhookEnvelope

_SIGNAL = "entry_long"
_SYMBOL = "NIFTY"
_ACTION = "BUY"
_QTY = 65  # NIFTY lot size (65) --- compliant, no auto-correct surprises.


class _AllowRisk:
    """Permissive pre-trade risk gate: lets the handler reach the dispatch step."""

    def check(self, order_request) -> tuple[bool, str]:  # noqa: ANN001
        return True, ""


class _FakeEngine:
    """Mirror TradingEngine.__init__ wiring: a real (in-memory) OrderManager
    backed by the SimulatedBroker.  A risk_manager is attached so the handler
    gets past the pre-trade check deterministically."""

    def __init__(self) -> None:
        self._order_manager = OrderManager(broker=SimulatedBroker())
        self._order_manager.risk_manager = _AllowRisk()  # type: ignore[attr-defined]


class _CountingEngine(_FakeEngine):
    """Fake engine whose broker records every dispatched OrderRequest."""

    def __init__(self, dispatched: list) -> None:  # noqa: ANN001
        super().__init__()
        self._dispatched = dispatched
        broker = self._order_manager.broker
        original = broker.place_order

        async def _counting_place(req):  # noqa: ANN001
            dispatched.append(req)
            return await original(req)

        broker.place_order = _counting_place


def _webhook(tag: str, ts: datetime) -> QueuedWebhook:
    envelope = WebhookEnvelope(
        event_id=f"evt-{tag}",
        event_type="signal",
        timestamp=ts,
        provider="tradethrone",
        payload={
            "signal": _SIGNAL,
            "symbol": _SYMBOL,
            "action": _ACTION,
            "quantity": _QTY,
            "strategy_name": "red-repro",
            "order_type": "MARKET",
            "product_type": "INTRADAY",
            "exchange": "NFO",
            "validity": "DAY",
        },
        idempotency_key=f"sig-{tag}",
    )
    return QueuedWebhook(envelope=envelope)


@pytest.fixture(autouse=True)
async def _init_tables():
    """Real DB tables (same pattern as the other real-DB durability suites)."""
    await init_db()
    yield


@pytest.fixture(autouse=True)
async def _cleanup_audit_rows():
    """Remove the trade-audit rows this suite writes (best-effort housekeeping)."""
    yield
    async with SessionLocal() as db:
        await db.execute(
            delete(TradeAuditRecord).where(
                TradeAuditRecord.provider == "tradethrone",
                TradeAuditRecord.symbol == _SYMBOL,
                TradeAuditRecord.signal == _SIGNAL,
            )
        )
        await db.commit()


@pytest.fixture(autouse=True)
async def _cleanup_signal_orders():
    """Reset durable order state this suite creates.

    P0-2 durability tests assert EXACT order counts, so every test starts from
    a clean ``orders`` table.  Only rows carrying a tenant-less ``signal_key``
    (written exclusively by the webhook signal path) plus their linked
    ``TradeRecord`` rows are removed -- user-scoped DMA/strategy rows and the
    raw ``orders`` rows seeded via ``client_order_id`` are never touched.
    """
    yield
    async with SessionLocal() as db:
        order_ids = (
            await db.execute(
                select(OrderRecord.id).where(OrderRecord.signal_key.is_not(None))
            )
        ).scalars().all()
        if order_ids:
            await db.execute(
                delete(TradeRecord).where(TradeRecord.order_id.in_(order_ids))
            )
            await db.execute(
                delete(OrderRecord).where(OrderRecord.id.in_(order_ids))
            )
        # Remove whole-suite rows seeded directly (user-scoped DMA row in H).
        await db.execute(
            delete(OrderRecord).where(OrderRecord.user_id == "tenant-u-1")
        )
        await db.commit()

@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_single_dispatch(monkeypatch):
    """C - two CONCURRENT deliveries of the same signal must produce ONE
    durable order and exactly ONE broker dispatch.

    The DB partial unique index + the claim CAS are the backstops: only one
    delivery can own the ``signal_key``; the loser either sees the committed
    PENDING claim or loses the INSERT race and never dispatches.
    """
    import asyncio

    dispatched = []
    engine = _CountingEngine(dispatched)
    monkeypatch.setattr("app.main._engine", engine)

    ts = datetime.now(timezone.utc)
    a = uuid.uuid4().hex[:8]
    b = uuid.uuid4().hex[:8]
    await asyncio.gather(
        handle_tradethrone_signal(_webhook(tag="conc-" + a + "a", ts=ts)),
        handle_tradethrone_signal(_webhook(tag="conc-" + b + "b", ts=ts)),
    )

    async with SessionLocal() as db:
        orders = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.symbol == _SYMBOL,
                    OrderRecord.side == _ACTION,
                )
            )
        ).scalars().all()

    assert len(orders) == 1, (
        f"Concurrent duplicate deliveries created {len(orders)} durable "
        "orders; the unique signal_key claim must allow exactly one."
    )
    assert len(dispatched) == 1, (
        f"Concurrent duplicate deliveries reached the broker {len(dispatched)} "
        "times; the loser must never dispatch."
    )


@pytest.mark.asyncio
async def test_restart_loses_in_memory_manager_not_order_state(monkeypatch):
    """D - a process/restart-style loss of the in-memory OrderManager must NOT
    erase an accepted order and must NOT double-dispatch a re-delivered signal.

    A fresh engine (empty in-memory OrderManager, same DB) re-processing the
    same signal dedupes against the durable DB claim.
    """
    first_dispatched = []
    engine1 = _CountingEngine(first_dispatched)
    monkeypatch.setattr("app.main._engine", engine1)

    ts = datetime.now(timezone.utc)
    await handle_tradethrone_signal(
        _webhook(tag="rst-" + uuid.uuid4().hex[:8] + "1", ts=ts)
    )

    async with SessionLocal() as db:
        before = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.symbol == _SYMBOL,
                    OrderRecord.side == _ACTION,
                )
            )
        ).scalars().all()
    assert len(before) == 1
    assert before[0].status == "FILLED"
    first_order_id = before[0].id

    # "Restart": a brand-new engine has no knowledge of the in-memory state.
    restart_dispatched = []
    engine2 = _CountingEngine(restart_dispatched)
    engine2._order_manager.active_positions.clear()  # noqa: SLF001
    engine2._order_manager.closed_positions.clear()  # noqa: SLF001
    engine2._order_manager.execution_history.clear()  # noqa: SLF001
    monkeypatch.setattr("app.main._engine", engine2)

    await handle_tradethrone_signal(
        _webhook(tag="rst-" + uuid.uuid4().hex[:8] + "2", ts=ts)
    )

    async with SessionLocal() as db:
        after = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.symbol == _SYMBOL,
                    OrderRecord.side == _ACTION,
                )
            )
        ).scalars().all()

    assert len(after) == 1, (
        "Re-delivery after an in-memory state loss must dedupe against the "
        "DB claim -- the accepted order must survive the restart."
    )
    assert after[0].id == first_order_id
    assert len(restart_dispatched) == 0, (
        "A re-delivered signal after restart must NOT reach the broker again."
    )

@pytest.mark.asyncio
async def test_persistence_failure_before_acknowledgement_raises(monkeypatch):
    """E - if the durable claim cannot persist, the handler FAILS CLOSED:
    it raises (worker nacks / never XACKs success) and the broker is never
    dispatched to.
    """
    import app.engine.durable_claims as durable_claims_mod

    dispatched = []
    engine = _CountingEngine(dispatched)
    monkeypatch.setattr("app.main._engine", engine)

    async def _failing_claim(**kwargs):  # noqa: ANN003
        raise RuntimeError("simulated DB outage during durable claim")

    monkeypatch.setattr(
        durable_claims_mod, "claim_order_record", _failing_claim
    )

    with pytest.raises(RuntimeError, match="simulated DB outage"):
        await handle_tradethrone_signal(
            _webhook(tag="fail-" + uuid.uuid4().hex[:8], ts=datetime.now(timezone.utc))
        )

    assert dispatched == [], (
        "A failed durable claim must never reach broker dispatch."
    )
    # No durable order row was accepted.
    async with SessionLocal() as db:
        orders = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.symbol == _SYMBOL,
                    OrderRecord.side == _ACTION,
                )
            )
        ).scalars().all()
    assert orders == [], (
        "A failed persistence must not leave an order record behind "
        "(and must never be acknowledged as success)."
    )


@pytest.mark.asyncio
async def test_crash_window_between_durable_claim_and_dispatch(monkeypatch):
    """F - a crash AFTER the PENDING claim commit but BEFORE broker dispatch.

    The committed claim must survive (restart cannot erase it) and a re-delivery
    must NOT second-dispatch while the claim is in-flight.
    """
    from app.engine.durable_claims import claim_order_record, signal_client_order_key

    dispatched = []
    engine = _CountingEngine(dispatched)
    monkeypatch.setattr("app.main._engine", engine)

    ts = datetime.now(timezone.utc)
    key = signal_client_order_key(
        provider="tradethrone",
        strategy_name="red-repro",
        symbol=_SYMBOL,
        side="BUY",
        quantity=_QTY,
        ts_sec=int(ts.timestamp()),
        signal=_SIGNAL,
    )

    # Crash point: the durable PENDING claim is committed, nothing dispatched.
    claim_id = await claim_order_record(
        key_predicate=OrderRecord.signal_key == key,
        claim_values={
            "signal_key": key,
            "symbol": _SYMBOL,
            "side": "BUY",
            "quantity": _QTY,
            "order_type": "MARKET",
            "mode": "PAPER",
        },
    )
    assert claim_id is not None

    async with SessionLocal() as db:
        claim = await db.get(OrderRecord, claim_id)
    assert claim is not None and claim.status == "PENDING"
    assert claim.broker_order_id is None

    # "Restart" + redelivery of the same signal: must NOT double-dispatch.
    after_restart_dispatched = []
    engine2 = _CountingEngine(after_restart_dispatched)
    monkeypatch.setattr("app.main._engine", engine2)

    await handle_tradethrone_signal(
        _webhook(tag="crash-" + uuid.uuid4().hex[:8], ts=ts)
    )

    assert after_restart_dispatched == [], (
        "A re-delivered signal must not dispatch while its PENDING claim is "
        "in flight (crash window)."
    )
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.symbol == _SYMBOL,
                    OrderRecord.side == _ACTION,
                )
            )
        ).scalars().all()
    assert len(rows) == 1, "The crash-left claim must be the only order."
    assert rows[0].id == claim_id, "The accepted claim must not be erased."

@pytest.mark.asyncio
async def test_db_state_is_authoritative_over_in_memory_manager(monkeypatch):
    """G - the durable DB claim governs even when the in-memory OrderManager
    knows nothing about it.

    A PENDING row seeded directly in the DB (as if another worker/process had
    claimed it) must suppress dispatch when the same signal arrives here.
    """
    from app.engine.durable_claims import signal_client_order_key

    dispatched = []
    engine = _CountingEngine(dispatched)
    monkeypatch.setattr("app.main._engine", engine)

    ts = datetime.now(timezone.utc)
    key = signal_client_order_key(
        provider="tradethrone",
        strategy_name="red-repro",
        symbol=_SYMBOL,
        side="BUY",
        quantity=_QTY,
        ts_sec=int(ts.timestamp()),
        signal=_SIGNAL,
    )

    # Seed the durable in-flight claim exactly like another worker would have.
    async with SessionLocal() as db:
        db.add(
            OrderRecord(
                signal_key=key,
                symbol=_SYMBOL,
                side="BUY",
                quantity=_QTY,
                order_type="MARKET",
                mode="PAPER",
                status="PENDING",
            )
        )
        await db.commit()

    await handle_tradethrone_signal(
        _webhook(tag="auth-" + uuid.uuid4().hex[:8], ts=ts)
    )

    assert dispatched == [], (
        "The DB in-flight claim must be authoritative: this delivery must not "
        "dispatch a second order."
    )
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(OrderRecord).where(OrderRecord.signal_key == key))
        ).scalars().all()
    assert len(rows) == 1

@pytest.mark.asyncio
async def test_tenant_less_signal_key_no_unsafe_cross_tenant_collision(monkeypatch):
    """H - the tenant-less signal key cannot create unsafe collisions:

    1. Two different strategies/providers sending the same symbol/side/
       quantity in the same second produce DIFFERENT keys (no cross-strategy
       swallowing), each creating its own durable order.
    2. A user-scoped DMA row may legally carry a key STRING identical to a
       signal key (separate key domains / index spaces): the signal claim is
       unaffected and a DMA-style row never collides with the tenant-less
       signal claim.
    """
    import asyncio

    from app.engine.durable_claims import signal_client_order_key

    dispatched = []
    engine = _CountingEngine(dispatched)
    monkeypatch.setattr("app.main._engine", engine)

    ts = datetime.now(timezone.utc)
    ts_sec = int(ts.timestamp())
    raw = {
        "provider": "tradethrone",
        "symbol": _SYMBOL,
        "side": "BUY",
        "quantity": _QTY,
        "ts_sec": ts_sec,
    }
    key_strat_a = signal_client_order_key(
        **raw, strategy_name="strat-A", signal=_SIGNAL
    )
    key_strat_b = signal_client_order_key(
        **raw, strategy_name="strat-B", signal=_SIGNAL
    )
    assert key_strat_a != key_strat_b, (
        "Two distinct strategies must never share a tenant-less signal key."
    )

    # Seed a user-scoped DMA-style row that reuses the signal key STRING.
    async with SessionLocal() as db:
        db.add(
            OrderRecord(
                user_id="tenant-u-1",
                client_order_id=key_strat_a,
                symbol="NIFTY",
                side="BUY",
                quantity=65,
                order_type="MARKET",
                mode="LIVE",
                status="FILLED",
            )
        )
        await db.commit()

    def _payload_strat(name: str) -> dict:
        return {
            "signal": _SIGNAL,
            "symbol": _SYMBOL,
            "action": _ACTION,
            "quantity": _QTY,
            "strategy_name": name,
            "order_type": "MARKET",
            "product_type": "INTRADAY",
            "exchange": "NFO",
            "validity": "DAY",
        }

    def _env(name: str) -> WebhookEnvelope:
        return WebhookEnvelope(
            event_id="evt-h-" + name,
            event_type="signal",
            timestamp=ts,
            provider="tradethrone",
            payload=_payload_strat(name),
            idempotency_key="sig-h-" + name,
        )

    await asyncio.gather(
        handle_tradethrone_signal(QueuedWebhook(envelope=_env("strat-A"))),
        handle_tradethrone_signal(QueuedWebhook(envelope=_env("strat-B"))),
    )

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(OrderRecord).where(OrderRecord.signal_key.is_not(None))
            )
        ).scalars().all()

    assert len(rows) == 2, (
        f"Two distinct strategies must create two durable orders; got "
        f"{len(rows)} ({[r.signal_key for r in rows]})."
    )
    assert {r.signal_key for r in rows} == {key_strat_a, key_strat_b}

    # The pre-existing tenant-scoped row is untouched and never collided with
    # the tenant-less signal claims (separate key domains).
    async with SessionLocal() as db:
        dma = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == "tenant-u-1",
                    OrderRecord.client_order_id == key_strat_a,
                )
            )
        ).scalars().all()
    assert len(dma) == 1
    assert dma[0].signal_key is None
    assert dma[0].status == "FILLED"
