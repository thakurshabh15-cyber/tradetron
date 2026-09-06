"""P1 â€” Broker-acceptance crash-window reconciliation regression tests.

Feature: keyed DMA/manual order = durable PENDING claim -> broker accepts
(broker reference durably committed right after acceptance) -> process dies
before local finalization -> ``app.engine.order_reconciliation`` reads the
broker's status back (READ-ONLY) and finalizes the local row from confirmed
broker state.

Invariants: FILLED finalizes; OPEN keeps PENDING; REJECTED marks local
REJECTED; UNKNOWN / unsupported / missing-ref / fresh rows are never
mutated and never submitted; reconciliation never calls ``place_order()``;
user/tenant isolation + broker-account binding enforced; bounded batches;
errors never crash the pass; manual LIVE persists the broker reference;
DMA behavior intact; BROKER_MODE=simulated still blocks real adapters.

SAFETY: no real broker/payment/network call ever occurs. Every adapter is a
deterministic fake, or the real adapter whose BROKER_MODE gate raises before
any network work.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.session import init_db, SessionLocal
from app.engine.order_reconciliation import (
    RECONCILIATION_BATCH_SIZE,
    STALE_PENDING_MIN_AGE_SECONDS,
    BrokerOrderReconciliationEngine,
)
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


async def _seed_broker_account(
    user_id: str, broker_name: str = "ZERODHA"
) -> str:
    """Insert a CONNECTED active broker account owned by the user (no network)."""
    async with SessionLocal() as session:
        async with session.begin():
            rec = BrokerAccountRecord(
                user_id=user_id,
                broker_name=broker_name,
                status="CONNECTED",
                is_active=True,
            )
            rec.set_credentials("k", "s", "t")
            session.add(rec)
            await session.flush()
            return rec.id


async def _seed_order(
    broker_account_id: str,
    *,
    key: str,
    user_id: str | None = None,
    status: str = "PENDING",
    broker_ref: str | None = "BR-000001",
    mode: str = "LIVE",
    created_seconds_ago: float = STALE_PENDING_MIN_AGE_SECONDS + 60,
    symbol: str = "RELIANCE",
    side: str = "BUY",
    quantity: int = 10,
    order_type: str = "MARKET",
    price: float = 2500.0,
) -> str:
    """Insert a stale keyed order row owned by the broker account's user.

    ``user_id`` overrides the owner (used by the cross-tenant isolation test);
    defaults to the broker account's owner. Returns its id."""
    async with SessionLocal() as db:
        owner = user_id
        if owner is None:
            acct = await db.get(BrokerAccountRecord, broker_account_id)
            owner = acct.user_id if acct is not None else broker_account_id
        order = OrderRecord(
            user_id=owner,
            client_order_id=key,
            broker_account_id=broker_account_id,
            broker_order_id=broker_ref,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            mode=mode,
            status=status,
            created_at=datetime.now(timezone.utc)
            - timedelta(seconds=created_seconds_ago),
        )
        db.add(order)
        await db.commit()
        return order.id


async def _fetch_order(order_id: str) -> OrderRecord:
    async with SessionLocal() as db:
        return await db.get(OrderRecord, order_id)


async def _counts_for(order_id: str) -> tuple[int, int]:
    order = await _fetch_order(order_id)
    async with SessionLocal() as db:
        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == order.user_id)
            )
        ).scalars().all()
        positions = (
            await db.execute(
                select(PositionRecord).where(
                    PositionRecord.user_id == order.user_id
                )
            )
        ).scalars().all()
    return len(trades), len(positions)


@pytest.fixture(autouse=True)
async def _isolate_reconciliation_state():
    """Deterministic isolation for GLOBAL reconciliation semantics.

    ``reconcile_once()`` scans EVERY stale PENDING keyed order in the shared
    SQLite database (oldest-first, bounded batch), so leftover rows from a
    previous run or an earlier test in the same run would otherwise consume the
    batch and pollute the summary counts.  Each test therefore starts from an
    empty orders/trades/positions/broker-accounts state.

    SAFETY: pure local SQLite/PostgreSQL DELETEs on the app's own tables — no
    broker, payment, or network interaction of any kind.  ``init_db()`` is
    idempotent (schema + seed re-created if missing).
    """
    from sqlalchemy import delete

    await init_db()
    async with SessionLocal() as session:
        async with session.begin():
            # FK-safe delete order: children before parents.
            await session.execute(delete(PositionRecord))
            await session.execute(delete(TradeRecord))
            await session.execute(delete(OrderRecord))
            await session.execute(delete(BrokerAccountRecord))
    yield


class _StatusBroker:
    """Deterministic fake status adapter. Never placed orders."""

    def __init__(self, status: str, average_price: float | None = None):
        self.status = status
        self.average_price = average_price
        self.place_calls = 0
        self.status_calls = 0

    async def place_order(self, req):  # noqa: ANN001
        self.place_calls += 1
        raise AssertionError("reconciliation must NEVER call place_order")

    async def get_order_status(self, broker_order_id: str) -> dict:
        self.status_calls += 1
        return {
            "status": self.status,
            "broker_order_id": broker_order_id,
            **(
                {"average_price": self.average_price, "filled_quantity": 10}
                if self.average_price is not None
                else {}
            ),
        }
# â”€â”€ 1. FILLED â†’ local order finalized â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_stale_pending_filled_is_finalized(monkeypatch):
    """Stale PENDING + broker reference + FILLED broker status â†’ the local
    order is finalized (FILLED, filled price/qty, one trade, one position).
    Exactly one read-only status call; zero placements."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid = await _seed_order(sid, key="TST-REC-FILL-01")

    fake = _StatusBroker("FILLED", average_price=2505.0)
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    summary = await engine.reconcile_once()

    order = await _fetch_order(oid)
    assert order.status == "FILLED"
    assert order.filled_price == 2505.0
    assert order.filled_quantity == 10
    assert order.position_id is not None
    trades, positions = await _counts_for(oid)
    assert trades == 1 and positions == 1
    assert fake.place_calls == 0
    assert fake.status_calls == 1
    assert summary["filled"] == 1 and summary["scanned"] == 1


# â”€â”€ 2. OPEN â†’ remains PENDING â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_stale_pending_open_stays_pending(monkeypatch):
    """Broker says OPEN â†’ local row stays PENDING; no fabrication, no trade/
    position, no placement."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid = await _seed_order(sid, key="TST-REC-OPEN-01")

    fake = _StatusBroker("OPEN")
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    summary = await engine.reconcile_once()

    order = await _fetch_order(oid)
    assert order.status == "PENDING"
    trades, positions = await _counts_for(oid)
    assert trades == 0 and positions == 0
    assert fake.place_calls == 0
    assert summary["open"] == 1


# â”€â”€ 3. REJECTED â†’ local REJECTED â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_stale_pending_rejected_marks_local_rejected(monkeypatch):
    """Broker says REJECTED â†’ local order is marked REJECTED (retryable) with
    a reconciliation error message; no trade/position; no placement."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid = await _seed_order(sid, key="TST-REC-REJ-01")

    fake = _StatusBroker("REJECTED")
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    summary = await engine.reconcile_once()

    order = await _fetch_order(oid)
    assert order.status == "REJECTED"
    assert "REJECTED" in (order.error_message or "")
    trades, positions = await _counts_for(oid)
    assert trades == 0 and positions == 0
    assert fake.place_calls == 0
    assert summary["rejected"] == 1


# â”€â”€ 4. UNKNOWN â†’ no fabricated state, no placement â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_unknown_status_no_fabrication_no_placement(monkeypatch):
    """Broker status UNKNOWN / unexpected vocabulary â†’ the row stays PENDING;
    nothing is fabricated, nothing is submitted."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid = await _seed_order(sid, key="TST-REC-UNK-01")

    fake = _StatusBroker("SOME_STRANGE_BROKER_STATE")
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    summary = await engine.reconcile_once()

    order = await _fetch_order(oid)
    assert order.status == "PENDING"
    assert order.error_message is None
    trades, positions = await _counts_for(oid)
    assert trades == 0 and positions == 0
    assert fake.place_calls == 0
    assert summary["unknown"] == 1


# â”€â”€ 5. Adapter without a usable status API â†’ safely skipped â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_adapter_without_status_api_is_safely_skipped(monkeypatch):
    """An adapter with no status read-back behaves like UNKNOWN: no fabricated
    state, no placement, row stays PENDING."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid = await _seed_order(sid, key="TST-REC-NOSTAT-01")

    class _NoStatusBroker:
        async def place_order(self, req):  # noqa: ANN001
            raise AssertionError("reconciliation must NEVER call place_order")

    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _NoStatusBroker())

    summary = await engine.reconcile_once()

    order = await _fetch_order(oid)
    assert order.status == "PENDING"
    trades, positions = await _counts_for(oid)
    assert trades == 0 and positions == 0
    assert summary["unknown"] == 1  # missing method surfaced as status_error
# â”€â”€ 6. Fresh PENDING â†’ never reconciled â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_fresh_pending_order_is_not_reconciled(monkeypatch):
    """A PENDING order younger than the stale threshold is NEVER touched â€” no
    broker status query at all."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid = await _seed_order(
        sid,
        key="TST-REC-FRESH-01",
        created_seconds_ago=10,  # younger than the stale threshold
    )

    fake = _StatusBroker("FILLED", average_price=2505.0)
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    summary = await engine.reconcile_once()

    assert fake.status_calls == 0
    assert fake.place_calls == 0
    order = await _fetch_order(oid)
    assert order.status == "PENDING"
    assert summary["scanned"] == 0


# â”€â”€ 7. Missing broker reference â†’ no blind placement â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_missing_broker_reference_not_reconciled(monkeypatch):
    """A stale PENDING order WITHOUT a durable broker reference is left
    untouched â€” no heuristic lookup, no status call, no placement."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid = await _seed_order(
        sid, key="TST-REC-NOREF-01", broker_ref=None
    )

    fake = _StatusBroker("FILLED", average_price=2505.0)
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    summary = await engine.reconcile_once()

    assert fake.status_calls == 0
    assert fake.place_calls == 0
    order = await _fetch_order(oid)
    assert order.status == "PENDING"
    assert summary["scanned"] == 0


# â”€â”€ 8. Reconciliation never calls place_order â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_reconciliation_never_calls_place_order(monkeypatch):
    """Across FILLED/OPEN/REJECTED/UNKNOWN outcomes the fake's place_order
    (which raises if called) is never invoked."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)

    brokers = [_StatusBroker(s) for s in ("FILLED", "OPEN", "REJECTED", "ZZZ")]
    for i, fake in enumerate(brokers):
        oid = await _seed_order(
            sid, key=f"TST-REC-NOPLACE-{i}"
        )
        assert oid
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: brokers.pop(0))

    summary = await engine.reconcile_once()
    assert all(f.place_calls == 0 for f in brokers)
    assert summary["filled"] == 1
    assert summary["open"] == 1
    assert summary["rejected"] == 1
    assert summary["unknown"] == 1


# â”€â”€ 9. User/tenant isolation + broker-account binding â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_user_tenant_isolation_enforced(monkeypatch):
    """An order is reconciled only through its OWN connected, active broker
    account owned by the order's user. A cross-tenant row (order.user !=
    broker owner) is skipped without mutation; the legitimate order is finalized."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    user_a = _uuid.uuid4().hex
    user_b = _uuid.uuid4().hex
    acct_a = await _seed_broker_account(user_a)
    # user B's order points at A's broker account â†’ must be skipped.
    oid_b = await _seed_order(
        acct_a, key="TST-REC-ISOLATE-B", user_id=user_b
    )
    oid_a = await _seed_order(
        acct_a, key="TST-REC-ISOLATE-A", user_id=user_a
    )

    fake = _StatusBroker("FILLED", average_price=2510.0)
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    summary = await engine.reconcile_once()

    order_a = await _fetch_order(oid_a)
    order_b = await _fetch_order(oid_b)
    assert order_a.status == "FILLED"
    assert order_b.status == "PENDING", "cross-tenant order must not be touched"
    assert summary["filled"] == 1
    assert summary["skipped"] == 1
    assert fake.place_calls == 0
# â”€â”€ 10. Bounded batch processing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_bounded_batch_processing(monkeypatch):
    """Reconciliation is bounded: one pass processes at most the configured
    batch size; a smaller explicit max is honored."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    for i in range(RECONCILIATION_BATCH_SIZE + 5):
        await _seed_order(
            sid, key=f"TST-REC-BATCH-{i:02d}"
        )

    fake = _StatusBroker("FILLED", average_price=2515.0)
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    summary = await engine.reconcile_once()
    assert summary["scanned"] == RECONCILIATION_BATCH_SIZE
    assert summary["filled"] == RECONCILIATION_BATCH_SIZE
    assert fake.status_calls == RECONCILIATION_BATCH_SIZE

    # A second pass continues the remainder (bounded again).
    summary2 = await engine.reconcile_once(max_orders=3)
    assert summary2["scanned"] == 3
    assert summary2["filled"] == 3


# â”€â”€ 11. Errors never crash the worker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_reconciliation_errors_do_not_crash(monkeypatch):
    """A broker status query that raises is contained per order: the batch
    completes, other orders are processed, and the pass returns a summary."""
    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid_ok = await _seed_order(
        sid, key="TST-REC-ERR-OK"
    )
    oid_bad = await _seed_order(
        sid, key="TST-REC-ERR-BAD", broker_ref="BR-ERR-BAD"
    )

    class _FlakyBroker:
        """Raises ONLY for the BAD order's broker reference, fills otherwise —
        deterministic regardless of processing order inside the batch."""

        async def place_order(self, req):  # noqa: ANN001
            raise AssertionError("never")

        async def get_order_status(self, broker_order_id: str) -> dict:
            if broker_order_id == "BR-ERR-BAD":
                raise RuntimeError("simulated broker outage")
            return {
                "status": "FILLED",
                "average_price": 2520.0,
                "filled_quantity": 10,
            }

    fake = _FlakyBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    summary = await engine.reconcile_once()

    assert summary["errors"] + summary["unknown"] >= 1
    assert summary["filled"] == 1
    order_ok = await _fetch_order(oid_ok)
    order_bad = await _fetch_order(oid_bad)
    assert order_ok.status == "FILLED"
    assert order_bad.status == "PENDING"


# â”€â”€ 12. Manual LIVE broker reference persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_manual_live_keyed_persists_broker_reference(monkeypatch):
    """After a LIVE keyed manual order, the broker's returned order reference
    is durably stored on the claim (pre-fix the manual path stored only the
    local ORD_* id, losing the reference needed for crash recovery)."""
    from httpx import ASGITransport, AsyncClient

    from app.api.auth import create_access_token
    from app.core.security import hash_password
    from app.main import app
    from app.models.user import UserRecord

    await init_db()
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed",
                        lambda: None)

    class _FakeBroker:
        async def place_order(self, req):  # noqa: ANN001
            return {"broker_order_id": "MANUAL-BROKER-REF-42",
                    "filled_price": 2530.0}

    monkeypatch.setattr("app.api.trades.get_broker_adapter",
                        lambda rec: _FakeBroker())

    async with SessionLocal() as db:
        user = UserRecord(
            email=f"rec_{_uuid.uuid4().hex[:10]}@tradetron.io",
            hashed_password=hash_password("SecurePassword123!"),
            full_name="Recon Tester", is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        uid = user.id
        token = create_access_token({"sub": uid})

    await _seed_broker_account(uid)
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        key = "TST-REC-MANUAL-01"
        payload = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "LIVE", "client_order_id": key,
        }
        resp = await client.post("/api/trades/place", json=payload, headers=headers)
        assert resp.status_code == 200, resp.text

    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == uid,
                    OrderRecord.client_order_id == key,
                )
            )
        ).scalar_one()
        assert row.broker_order_id == "MANUAL-BROKER-REF-42", (
            "the broker's returned order reference must be persisted on the "
            "manual LIVE path (crash-window prerequisite)"
        )
        assert row.status == "FILLED"
# â”€â”€ 13. DMA existing broker reference behavior remains intact â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_dma_keyed_persists_broker_reference_and_replays(monkeypatch):
    """DMA keyed orders keep persisting the broker reference and replay it â€”
    existing behavior untouched."""
    from httpx import ASGITransport, AsyncClient

    from app.api.auth import create_access_token
    from app.core.security import hash_password
    from app.main import app
    from app.models.user import UserRecord

    await init_db()
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed",
                        lambda: None)

    class _FakeBroker:
        def __init__(self) -> None:
            self.calls = 0

        async def place_order(self, req):  # noqa: ANN001
            self.calls += 1
            return {"broker_order_id": "DMA-BROKER-REF-77", "filled_price": 21200.0}

    fake = _FakeBroker()
    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda rec: fake)

    async with SessionLocal() as db:
        user = UserRecord(
            email=f"rec_{_uuid.uuid4().hex[:10]}@tradetron.io",
            hashed_password=hash_password("SecurePassword123!"),
            full_name="DMA Recon Tester", is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        uid = user.id
        token = create_access_token({"sub": uid})

    await _seed_broker_account(uid, broker_name="ANGEL_ONE")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        key = "TST-REC-DMA-01"
        payload = {
            "symbol": "NIFTY", "side": "BUY", "lots": 1, "product": "MIS",
            "order_type": "MARKET", "mode": "LIVE", "client_order_id": key,
        }
        r1 = await client.post("/api/v1/orders/execute-dma", json=payload, headers=headers)
        assert r1.status_code == 200, r1.text
        r2 = await client.post("/api/v1/orders/execute-dma", json=payload, headers=headers)
        assert r2.status_code == 200 and r2.json()["idempotent_replay"] is True

    assert fake.calls == 1
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == uid,
                    OrderRecord.client_order_id == key,
                )
            )
        ).scalar_one()
        assert row.broker_order_id == "DMA-BROKER-REF-77"
        assert row.status == "FILLED"
    assert r2.json()["broker_order_id"] == "DMA-BROKER-REF-77"


# â”€â”€ 14. BROKER_MODE=simulated remains enforced â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_broker_mode_simulated_blocks_real_status_read(monkeypatch):
    """In BROKER_MODE=simulated a REAL broker adapter's status read is blocked
    by the live-connect guard; the engine treats the row as unknown/skipped and
    leaves it PENDING â€” no state fabrication and no broker contact."""
    await init_db()
    from app.config import settings

    settings.broker_mode = "simulated"
    engine = BrokerOrderReconciliationEngine()
    # No get_broker_adapter monkeypatch â†’ the real Zerodha adapter is built;
    # its connect()/BROKER_MODE guard raises BEFORE any network work.
    sid = await _seed_broker_account(_uuid.uuid4().hex, broker_name="ZERODHA")
    oid = await _seed_order(sid, key="TST-REC-SIM-01")

    summary = await engine.reconcile_once()

    order = await _fetch_order(oid)
    assert order.status == "PENDING"
    assert summary["unknown"] >= 1 or summary["errors"] >= 1
    trades, positions = await _counts_for(oid)
    assert trades == 0 and positions == 0

    # The API-layer guard is still live for real dispatches.
    from app.brokers import BrokerModeBlockedError, assert_live_dispatch_allowed
    with pytest.raises(BrokerModeBlockedError):
        assert_live_dispatch_allowed()


# â”€â”€ 15. Concurrent passes never overlap (in-process lock) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.asyncio
async def test_concurrent_passes_do_not_overlap(monkeypatch):
    """Two overlapping reconcile_once() calls serialize on the engine lock; the
    same order is never processed twice in one overlap."""
    import asyncio

    await init_db()
    engine = BrokerOrderReconciliationEngine()
    sid = await _seed_broker_account(_uuid.uuid4().hex)
    oid = await _seed_order(sid, key="TST-REC-LOCK-01")

    class _SlowBroker:
        def __init__(self) -> None:
            self.status_calls = 0

        async def place_order(self, req):  # noqa: ANN001
            raise AssertionError("never")

        async def get_order_status(self, broker_order_id: str) -> dict:
            self.status_calls += 1
            await asyncio.sleep(0.05)
            return {"status": "FILLED", "average_price": 2535.0,
                    "filled_quantity": 10}

    fake = _SlowBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: fake)

    s1, s2 = await asyncio.gather(
        engine.reconcile_once(), engine.reconcile_once()
    )

    order = await _fetch_order(oid)
    assert order.status == "FILLED"
    assert s1["filled"] + s2["filled"] == 1, (
        "exactly one pass may finalize; the other observes a non-PENDING row"
    )
    trades, positions = await _counts_for(oid)
    assert trades == 1 and positions == 1