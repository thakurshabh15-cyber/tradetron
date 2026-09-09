"""Phase 15 RED regression — reconciliation must never over-book a partial fill.

Defect (P1, financial correctness): ``app/engine/order_reconciliation.py``
``_finalize_filled`` CAS-updates the order row with the broker-confirmed
``filled_quantity`` but books the created ``TradeRecord`` and
``PositionRecord`` at the FULL ``order.quantity``.

Reproduction (Window-B path with a broker reference):
  * keyed LIVE PENDING order: quantity=10, broker_order_id present
  * broker read-only ``get_order_status`` returns ``status=FILLED`` with
    ``filled_quantity=4`` (a partial fill, definitively confirmed by the
    broker's own order-status API)
  * reconciliation must book a position and trade of **4** (the confirmed
    exposure) — never 10.

Invariant pinned here is the engine's own ``M. Partial fill — never
over-book from partial info`` rule: the local ledger may never state a
larger position than the broker confirms.

SAFETY: no real broker/network call; the adapter factory is patched to a
deterministic fake that only serves the read-only status payload.
"""

from __future__ import annotations

import uuid as _uuid

import pytest

from app.db.session import init_db
from app.engine.order_reconciliation import BrokerOrderReconciliationEngine
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord


async def _seed_broker_account(user_id: str) -> str:
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        async with session.begin():
            rec = BrokerAccountRecord(
                user_id=user_id,
                broker_name="ZERODHA",
                status="CONNECTED",
                is_active=True,
            )
            rec.set_credentials("k", "s", "t")
            session.add(rec)
            await session.flush()
            return rec.id


async def _seed_stale_pending_order(
    broker_account_id: str,
    *,
    symbol: str = "RELIANCE",
    quantity: int = 10,
    price: float = 2500.0,
    broker_ref: str = "PARTIAL-BR-1",
) -> str:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        acct = await db.get(BrokerAccountRecord, broker_account_id)
        order = OrderRecord(
            user_id=acct.user_id if acct else broker_account_id,
            client_order_id="TST-PARTIAL-01",
            broker_account_id=broker_account_id,
            broker_order_id=broker_ref,
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            order_type="MARKET",
            price=price,
            mode="LIVE",
            status="PENDING",
            created_at=datetime.now(timezone.utc) - timedelta(seconds=300),
        )
        db.add(order)
        await db.commit()
        # Refresh to ensure the id default is populated for MS-SQL style tests
        await db.refresh(order)
        return str(order.id)


async def _fetch_order(order_id: str) -> OrderRecord:
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        return await db.get(OrderRecord, order_id)


async def _fetch_derived(order_id: str) -> tuple[TradeRecord | None, PositionRecord | None]:
    from sqlalchemy import select

    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        order = await db.get(OrderRecord, order_id)
        trades = (
            await db.execute(select(TradeRecord).where(TradeRecord.order_id == order_id))
        ).scalars().all()
        positions = (
            await db.execute(
                select(PositionRecord).where(PositionRecord.id == order.position_id)
            )
        ).scalars().all()
    return (trades[0] if trades else None), (positions[0] if positions else None)


class _PartialFillStatusBroker:
    """Deterministic fake adapter: read-only status read confirming a partial fill."""

    def __init__(self, filled_quantity: int = 4, average_price: float = 2505.0) -> None:
        self.filled_quantity = filled_quantity
        self.average_price = average_price
        self.place_calls = 0

    async def place_order(self, req):  # noqa: ANN001
        self.place_calls += 1
        raise AssertionError("reconciliation must NEVER call place_order")

    async def get_order_status(self, broker_order_id: str) -> dict:
        return {
            "status": "FILLED",
            "broker_order_id": broker_order_id,
            "filled_quantity": self.filled_quantity,
            "average_price": self.average_price,
        }


@pytest.fixture(autouse=True)
async def _isolate_reconciliation_state():
    """Deterministic isolation: ``reconcile_once()`` scans every stale keyed
    PENDING order globally, so leftover rows from other suites would consume
    the batch and pollute summaries.  Mirror the existing reconciliation
    suite's clean-slate fixture."""
    from sqlalchemy import delete

    from app.db.session import SessionLocal

    await init_db()
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(delete(PositionRecord))
            await session.execute(delete(TradeRecord))
            await session.execute(delete(OrderRecord))
            await session.execute(delete(BrokerAccountRecord))
    yield


@pytest.mark.asyncio
async def test_partial_fill_books_confirmed_quantity_not_order_quantity(monkeypatch):
    """Window-B: broker confirms FILLED with filled_quantity=4 of an order of 10.

    The reconciled TradeRecord and PositionRecord MUST carry the broker-
    confirmed quantity (4), never the full order quantity (10).
    """
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid = await _seed_stale_pending_order(sid, quantity=10)

    fake = _PartialFillStatusBroker(filled_quantity=4, average_price=2505.0)
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda rec: fake)

    summary = await engine.reconcile_once()

    order = await _fetch_order(oid)
    assert order.status == "FILLED", summary
    assert order.filled_quantity == 4
    assert fake.place_calls == 0
    assert summary["filled"] == 1 and summary["skipped"] == 0

    trade, position = await _fetch_derived(oid)
    assert trade is not None and position is not None
    # THE INVARIANT — never over-book from partial info:
    assert trade.quantity == 4, (
        f"Trade booked {trade.quantity} while broker confirmed 4 (order qty 10)"
    )
    assert position.quantity == 4, (
        f"Position booked {position.quantity} while broker confirmed 4 (order qty 10)"
    )
    assert position.status == "OPEN"