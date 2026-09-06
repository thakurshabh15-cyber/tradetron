"""V4.1 regression tests: postback <-> reconciliation finalizer mutual exclusion.

Defect (P1, race): the broker postback reconciler
(``app.brokers.postback.reconcile_broker_postback``) and the crash-window order
reconciliation engine (``app.engine.order_reconciliation.BrokerOrderReconciliationEngine``)
can BOTH observe the same un-booked PENDING order concurrently and BOTH book the
confirmed fill -- creating a duplicate TradeRecord AND a duplicate PositionRecord
for ONE broker fill (double-counted exposure, inflated PnL, duplicate report
rows).

Root cause: the postback idempotency guard was built from non-atomic session
snapshot reads (``prior_status`` / no linked position / no BROKER_POSTBACK_FILL
trade).  A postback transaction that read the order while it was still PENDING
decides to book even after the reconciliation engine has already committed the
same fill -- the stale guard never re-checks the database's committed state.
The reconciliation engine's own CAS guarded only ``status == 'PENDING'``, which
the postback never participated in.

Fix: BOTH finalizers now perform the SAME atomic claim on the order row before
booking -- a conditional UPDATE that may only claim the order while it is not
already finalized, has no linked position, and carries no fill trade.  Exactly
one concurrent transaction can win the claim (rowcount == 1); the loser skips.

The stale-read race is reproduced deterministically here using SQLAlchemy's
identity map: a "worker" session loads the order as PENDING, a separate session
finalizes the fill, and the worker session (``expire_on_commit=False`` -- the
API's configured default) still returns the stale PENDING object to the
postback reconciler.  Pre-fix this double-books; post-fix the atomic claim
re-evaluates against the committed rows and the postback skips.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.brokers.postback import reconcile_broker_postback
from app.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.engine.order_reconciliation import BrokerOrderReconciliationEngine
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord

_TEST_USER_IDS: list[str] = []


@pytest.fixture(autouse=True)
async def _cleanup_race_data():
    _TEST_USER_IDS.clear()
    yield
    async with SessionLocal() as db:
        for uid in set(_TEST_USER_IDS):
            await db.execute(delete(TradeRecord).where(TradeRecord.user_id == uid))
            await db.execute(delete(OrderRecord).where(OrderRecord.user_id == uid))
            await db.execute(delete(PositionRecord).where(PositionRecord.user_id == uid))
            await db.execute(
                delete(BrokerAccountRecord).where(BrokerAccountRecord.user_id == uid)
            )
            await db.execute(delete(UserRecord).where(UserRecord.id == uid))
        await db.commit()
    _TEST_USER_IDS.clear()


@pytest.fixture(autouse=True)
async def _init_db_and_settings():
    await init_db()
    settings.environment = "testing"
    settings.webhook_local_mode = False
    settings.broker_mode = "simulated"
    yield
    settings.broker_mode = "simulated"


async def _seed_keyed_pending_order() -> dict:
    """User + connected broker + LIVE PENDING order with broker reference and
    idempotency key (the crash-window family the reconciliation engine owns)."""
    async with SessionLocal() as db:
        uid = str(uuid.uuid4())
        user = UserRecord(
            id=uid,
            email=f"race_{uid[:8]}@tradetron.io",
            hashed_password=hash_password("Pass12345!"),
            full_name="Race Tester",
            role="trader",
            is_active=True,
            is_verified=True,
            paper_balance=1_000_000.0,
        )
        db.add(user)
        await db.flush()

        broker = BrokerAccountRecord(
            user_id=user.id,
            broker_name="ZERODHA",
            account_name="Race Account",
            client_id="CLIENT_RACE",
            status="CONNECTED",
            is_active=True,
        )
        broker.set_credentials(
            api_key="RACEKEY123",
            api_secret="RACESECRET123",
            access_token="RACETOKEN123",
        )
        db.add(broker)
        await db.flush()

        broker_order_id = f"KITE-RACE-{uuid.uuid4().hex[:10]}"
        order = OrderRecord(
            user_id=user.id,
            broker_account_id=broker.id,
            broker_order_id=broker_order_id,
            client_order_id=f"race-key-{uuid.uuid4().hex[:8]}",
            symbol="NIFTY50",
            side="BUY",
            quantity=10,
            price=2400.0,
            filled_quantity=0,
            status="PENDING",
            mode="LIVE",
        )
        db.add(order)
        await db.commit()
        _TEST_USER_IDS.append(user.id)
        return {
            "user_id": user.id,
            "broker_id": broker.id,
            "order_id": order.id,
            "broker_order_id": broker_order_id,
        }


async def _fetch_open_positions(user_id: str) -> list[PositionRecord]:
    async with SessionLocal() as db:
        res = await db.execute(
            select(PositionRecord).where(
                PositionRecord.user_id == user_id,
                PositionRecord.status == "OPEN",
            )
        )
        return list(res.scalars().all())


async def _fetch_trades_by_reason(user_id: str, reason: str) -> list[TradeRecord]:
    async with SessionLocal() as db:
        res = await db.execute(
            select(TradeRecord).where(
                TradeRecord.user_id == user_id,
                TradeRecord.exit_reason == reason,
            )
        )
        return list(res.scalars().all())


async def _fetch_order(order_id: str) -> OrderRecord | None:
    async with SessionLocal() as db:
        return await db.get(OrderRecord, order_id)


_BROKER_FILL_RESP = {
    "status": "FILLED",
    "average_price": 24850.0,
    "filled_quantity": 50,
}


async def _reconciliation_finalize(
    db, order: OrderRecord
) -> tuple[str, str]:
    engine = BrokerOrderReconciliationEngine()
    return await engine._finalize_filled(db, order, dict(_BROKER_FILL_RESP))

# ---- 1. race: reconciliation books first, stale postback must not re-book ---


@pytest.mark.asyncio
async def test_postback_after_reconciliation_race_books_single_fill():
    """The postback reconciler holds a stale PENDING snapshot while the
    reconciliation engine books the fill (trade + position).  The postback must
    NOT add a second trade/position.

    Pre-fix this double-booked: the postback guard only inspected its own stale
    session state (prior_status read as PENDING, no linked position, no
    BROKER_POSTBACK_FILL trade visible) and inserted a duplicate PositionRecord
    + TradeRecord.
    """
    seeded = await _seed_keyed_pending_order()

    # "Postback worker" session: loads the order FIRST.  After that its
    # identity map keeps the PENDING snapshot even after another session
    # commits (SessionLocal uses expire_on_commit=False) -- exactly the stale
    # read a concurrent transaction performs.
    async with SessionLocal() as db_worker:
        stale_order = await db_worker.get(OrderRecord, seeded["order_id"])
        assert stale_order.status == "PENDING"
        await db_worker.commit()

        # Reconciliation finalizes first (books RECONCILIATION_FILL trade +
        # OPEN position) in a separate session.
        async with SessionLocal() as db_recon:
            recon_order = await db_recon.get(OrderRecord, seeded["order_id"])
            outcome, detail = await _reconciliation_finalize(db_recon, recon_order)
            assert outcome == "filled", detail

        # Postback arrives now, against the stale-snapshot worker session.
        outcome = await reconcile_broker_postback(
            db_worker,
            broker_order_id=seeded["broker_order_id"],
            broker_account_id=seeded["broker_id"],
            status="FILLED",
            symbol="NIFTY50",
            filled_quantity=50,
            average_price=24850.0,
        )
        assert outcome["event_processed"] is True

    order = await _fetch_order(seeded["order_id"])
    assert order is not None
    assert order.status == "FILLED"
    assert order.position_id is not None

    positions = await _fetch_open_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"
    postback_trades = await _fetch_trades_by_reason(
        seeded["user_id"], "BROKER_POSTBACK_FILL"
    )
    assert len(postback_trades) == 0, (
        f"postback must not re-book after reconciliation; got {len(postback_trades)}"
    )
    recon_trades = await _fetch_trades_by_reason(
        seeded["user_id"], "RECONCILIATION_FILL"
    )
    assert len(recon_trades) == 1, (
        f"expected exactly 1 RECONCILIATION_FILL trade, got {len(recon_trades)}"
    )

# ---- 2. reverse race: postback books first, stale reconciliation must skip ---


@pytest.mark.asyncio
async def test_reconciliation_after_postback_race_skips():
    """Reverse ordering: the postback books the fill first; the reconciliation
    engine, holding a stale PENDING snapshot, must lose the atomic claim and
    finalize nothing (no second trade/position)."""
    seeded = await _seed_keyed_pending_order()

    # "Reconciliation scheduler" session holds the stale PENDING snapshot.
    async with SessionLocal() as db_recon:
        stale_order = await db_recon.get(OrderRecord, seeded["order_id"])
        assert stale_order.status == "PENDING"
        await db_recon.commit()

        # Postback books the fill first through a fresh session.
        async with SessionLocal() as db_worker:
            outcome = await reconcile_broker_postback(
                db_worker,
                broker_order_id=seeded["broker_order_id"],
                broker_account_id=seeded["broker_id"],
                status="FILLED",
                symbol="NIFTY50",
                filled_quantity=50,
                average_price=24850.0,
            )
            assert outcome["event_processed"] is True

        # Reconciliation attempt from the stale snapshot must lose the claim.
        outcome, detail = await _reconciliation_finalize(db_recon, stale_order)
        assert outcome == "skipped", (outcome, detail)

    order = await _fetch_order(seeded["order_id"])
    assert order is not None
    assert order.status == "FILLED"

    positions = await _fetch_open_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"
    postback_trades = await _fetch_trades_by_reason(
        seeded["user_id"], "BROKER_POSTBACK_FILL"
    )
    assert len(postback_trades) == 1
    recon_trades = await _fetch_trades_by_reason(
        seeded["user_id"], "RECONCILIATION_FILL"
    )
    assert len(recon_trades) == 0


# ---- 3. positive path: reconciliation still books a plain single fill --------


@pytest.mark.asyncio
async def test_reconciliation_single_finalize_still_books_once():
    """Without any competing finalizer, the reconciliation engine's confirmed
    fill must still book exactly one trade and one linked position."""
    seeded = await _seed_keyed_pending_order()

    async with SessionLocal() as db:
        order = await db.get(OrderRecord, seeded["order_id"])
        outcome, detail = await _reconciliation_finalize(db, order)
        assert outcome == "filled", detail

    order = await _fetch_order(seeded["order_id"])
    assert order is not None
    assert order.status == "FILLED"
    assert order.position_id is not None

    positions = await _fetch_open_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"
    recon_trades = await _fetch_trades_by_reason(
        seeded["user_id"], "RECONCILIATION_FILL"
    )
    assert len(recon_trades) == 1
    postback_trades = await _fetch_trades_by_reason(
        seeded["user_id"], "BROKER_POSTBACK_FILL"
    )
    assert len(postback_trades) == 0

# ---- 4. mixed-path: raw COMPLETE then normalized FILLED must still book -----


@pytest.mark.asyncio
async def test_raw_complete_then_filled_postback_books_exactly_once():
    """The queued webhook worker passes broker statuses raw, so a real Zerodha
    event arrives as ``COMPLETE`` (never triggering the FILLED branch and
    booking nothing).  When a normalized ``FILLED`` event follows, the fill
    must STILL be booked exactly once - COMPLETE is not a finalized state while
    the fill is un-booked.
    """
    seeded = await _seed_keyed_pending_order()

    # Simulate the queued worker having delivered a raw COMPLETE event: local
    # status COMPLETE, ledgers untouched.
    async with SessionLocal() as db:
        order = await db.get(OrderRecord, seeded["order_id"])
        order.status = "COMPLETE"
        await db.commit()

    async with SessionLocal() as db:
        outcome = await reconcile_broker_postback(
            db,
            broker_order_id=seeded["broker_order_id"],
            broker_account_id=seeded["broker_id"],
            status="FILLED",
            symbol="NIFTY50",
            filled_quantity=50,
            average_price=24850.0,
        )
        assert outcome["event_processed"] is True

    order = await _fetch_order(seeded["order_id"])
    assert order is not None
    assert order.status == "FILLED"
    assert order.position_id is not None

    positions = await _fetch_open_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"
    postback_trades = await _fetch_trades_by_reason(
        seeded["user_id"], "BROKER_POSTBACK_FILL"
    )
    assert len(postback_trades) == 1


# ---- 5. terminal: FILLED postback on a REJECTED order must NOT book ----------


@pytest.mark.asyncio
async def test_filled_postback_on_rejected_order_books_nothing():
    """A terminally REJECTED order can never receive a fill - a FILLED event
    arriving after REJECTED must not fabricate a trade/position."""
    seeded = await _seed_keyed_pending_order()

    async with SessionLocal() as db:
        order = await db.get(OrderRecord, seeded["order_id"])
        order.status = "REJECTED"
        await db.commit()

    async with SessionLocal() as db:
        outcome = await reconcile_broker_postback(
            db,
            broker_order_id=seeded["broker_order_id"],
            broker_account_id=seeded["broker_id"],
            status="FILLED",
            symbol="NIFTY50",
            filled_quantity=50,
            average_price=24850.0,
        )
        assert outcome["event_processed"] is True

    positions = await _fetch_open_positions(seeded["user_id"])
    assert len(positions) == 0, f"expected no PositionRecord, got {len(positions)}"
    postback_trades = await _fetch_trades_by_reason(
        seeded["user_id"], "BROKER_POSTBACK_FILL"
    )
    assert len(postback_trades) == 0
