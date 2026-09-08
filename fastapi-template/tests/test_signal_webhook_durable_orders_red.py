"""P0-2 RED regression: the TradeThrone signal webhook must persist durable
order state in the database (source of truth) instead of relying on the
process-local / in-memory ``OrderManager``.

Defect (P0-2): the normal (non-local) webhook path delivers the validated
signal to ``handle_tradethrone_signal`` (``app/webhooks/handlers/tradethrone_signal.py``),
which obtains the engine's ``_order_manager`` — an ``OrderManager`` whose entire
order/position ledger lives in process memory (``active_positions``,
``closed_positions``, ``execution_history``, ``realized_pnl``) — and calls
``order_manager.place_order(...)``, a method that does not exist on
``OrderManager`` (source- and runtime-verified).  The handler's only durable
artifacts are a Redis idempotency record and a ``trade_audit_logs`` audit row:
**no** ``OrderRecord`` is ever written.

Consequences (failure model):
  * Process restart / Render deploy / crash between worker receipt and any
    durable write -> accepted signal state is lost (no DB record).
  * Multiple workers/processes each own a private in-memory OrderManager; no
    cross-process order ledger exists.
  * Retries / duplicate delivery / PEL recovery have no DB idempotency claim to
    dedupe against, so a second dispatch is not prevented by the DB.

Invariant being proven (RED, pre-fix):
  1. Processing a *valid* entry signal through ``handle_tradethrone_signal``
     MUST result in a durable ``OrderRecord`` (DB is the source of truth).
  2. Two deliveries of the same signal MUST NOT create duplicate durable
     orders.

Both assertions fail against the current implementation (zero durable
``OrderRecord`` rows), which is exactly the P0-2 regression this file pins.
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
from app.models.trading import OrderRecord
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


def _webhook(tag: str, ts: datetime | None = None) -> QueuedWebhook:
    envelope = WebhookEnvelope(
        event_id=f"evt-{tag}",
        event_type="signal",
        timestamp=ts or datetime.now(timezone.utc),
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

    P0-2 durability tests assert EXACT order counts (``len(orders) == 1``), so
    every test must start from a clean ``orders`` table.  Only rows carrying a
    tenant-less ``signal_key`` (written exclusively by the webhook signal
    path) are removed -- user-scoped DMA/strategy rows are never touched.
    Linked ``TradeRecord`` rows are removed first (FK ``order_id``).

    Cleanup runs BOTH before and after each test to guarantee isolation from
    other test files that leave stale rows in the shared persistent SQLite
    database.
    """
    from app.models.trading import TradeRecord

    async def _remove_signal_rows():
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
                await db.commit()

    await _remove_signal_rows()
    yield
    await _remove_signal_rows()


@pytest.fixture
def _fake_engine(monkeypatch):
    """Make the handler's ``get_engine()`` return a real-OrderManager-backed engine."""
    import app.main

    engine = _FakeEngine()
    monkeypatch.setattr(app.main, "_engine", engine)
    return engine


@pytest.mark.asyncio
async def test_valid_entry_signal_creates_durable_order_record(_fake_engine):
    """P0-2 invariant 1: a valid entry signal MUST be durably committed as an
    OrderRecord (DB is the source of truth), not only held in the in-memory
    OrderManager (which is process-local and lost on restart/deploy/scale)."""
    await handle_tradethrone_signal(_webhook(tag=uuid.uuid4().hex[:12]))

    async with SessionLocal() as db:
        orders = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.symbol == _SYMBOL,
                    OrderRecord.side == _ACTION,
                    OrderRecord.signal_key.is_not(None),
                )
            )
        ).scalars().all()

    assert orders, (
        "handle_tradethrone_signal completed a VALID entry signal without "
        "creating a durable OrderRecord. The order state exists only in the "
        "in-memory OrderManager (lost across processes/workers/restarts) -- "
        "P0-2 invariant violated."
    )


@pytest.mark.asyncio
async def test_duplicate_signal_delivery_does_not_duplicate_order(_fake_engine):
    """P0-2 invariant 2: two deliveries of the same signal (duplicate webhook /
    PEL recovery re-delivery) must NOT produce duplicate durable orders."""
    key = uuid.uuid4().hex[:12]
    # SAME envelope timestamp == same coarse-second identity for the
    # deterministic tenant-less signal_key -- exactly what a PEL /
    # XAUTOCLAIM re-delivery of the same signal event would carry.  The two
    # deliveries differ only in event_id/idempotency_key.
    ts = datetime.now(timezone.utc)
    await handle_tradethrone_signal(_webhook(tag=f"{key}a", ts=ts))
    await handle_tradethrone_signal(_webhook(tag=f"{key}b", ts=ts))

    async with SessionLocal() as db:
        orders = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.symbol == _SYMBOL,
                    OrderRecord.side == _ACTION,
                    OrderRecord.signal_key.is_not(None),
                )
            )
        ).scalars().all()

    assert len(orders) == 1, (
        f"Expected exactly 1 durable OrderRecord after two deliveries of the "
        f"same signal; got {len(orders)}. The signal path has no DB idempotency "
        f"claim, so duplicate delivery can duplicate state -- P0-2 invariant "
        f"violated."
    )
