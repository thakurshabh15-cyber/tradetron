"""P1 regression tests: atomic CAS on LIVE position close.

Defect (P1, race): both ``close_position`` (``app/api/trades.py``) and
``_close_single_follower_position`` (``app/engine/copy_trading.py``) used a
read-then-dispatch-then-commit pattern on ``PositionRecord.status``. Two
concurrent close requests could BOTH read ``status == "OPEN"``, BOTH dispatch a
real broker close order, and BOTH book a duplicate TradeRecord + realized PnL —
the one remaining unguarded money-moving path (entry / postback / reconciliation
all received atomic CAS protection, but close did not).

Fix: a database-level conditional UPDATE
``UPDATE positions SET status='CLOSED' WHERE id=:id AND status='OPEN'`` is now
the SOLE gate before any financial mutation.  Exactly one concurrent request
wins (``rowcount == 1``); losing requests return the conflict/not-found
response and must NEVER dispatch a broker close or book PnL.

Broker-failure safety: for LIVE positions the CAS claim is uncommitted when the
broker dispatch runs; if it fails the session is rolled back (manual HTTP path)
or the CAS is explicitly reverted to OPEN (copy-trading path, because there the
failure handler commits a REJECTED order).  No fabricated CLOSED / PnL.

These tests are deterministic concurrency regressions: they drive one position
through two genuine close attempts and assert that exactly one financial close
is booked and exactly one broker dispatch occurred.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.main import app
from app.config import settings
from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal, init_db
from app.engine.copy_trading import copy_trading_engine
from app.models.broker_account import BrokerAccountRecord
from app.models.copy_trading import CopyFollowerRecord, CopyGroupRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord

_TEST_USER_IDS: list[str] = []

@pytest.fixture(autouse=True)
async def _reset_db_and_simulated_mode():
    await init_db()
    settings.broker_mode = "simulated"
    yield
    settings.broker_mode = "simulated"


@pytest.fixture(autouse=True)
async def _cleanup_user_rows():
    _TEST_USER_IDS.clear()
    yield
    async with SessionLocal() as db:
        for uid in set(_TEST_USER_IDS):
            await db.execute(delete(TradeRecord).where(TradeRecord.user_id == uid))
            await db.execute(delete(OrderRecord).where(OrderRecord.user_id == uid))
            await db.execute(delete(PositionRecord).where(PositionRecord.user_id == uid))
            await db.execute(delete(CopyFollowerRecord).where(CopyFollowerRecord.follower_user_id == uid))
            await db.execute(
                delete(BrokerAccountRecord).where(BrokerAccountRecord.user_id == uid)
            )
            await db.execute(delete(UserRecord).where(UserRecord.id == uid))
        await db.commit()
    _TEST_USER_IDS.clear()


def _track(uid: str) -> None:
    if uid:
        _TEST_USER_IDS.append(uid)


async def _create_user(db, tag: str) -> UserRecord:
    uid = str(uuid.uuid4())
    _track(uid)
    rec = UserRecord(
        id=uid,
        email=f"cascas_{tag}_{uid[:8]}@tradetron.io",
        hashed_password=hash_password("SecurePassword123!"),
        full_name="CAS Close Tester",
        role="trader",
        is_active=True,
        is_verified=True,
        paper_balance=1_000_000.0,
    )
    db.add(rec)
    await db.flush()
    return rec


async def _create_broker(db, user_id: str) -> BrokerAccountRecord:
    rec = BrokerAccountRecord(
        user_id=user_id,
        broker_name="SIMULATED",
        account_name="CAS Account",
        client_id="CLIENT_01",
        status="CONNECTED",
        is_active=True,
    )
    rec.set_credentials("K", "S", "T")
    db.add(rec)
    await db.flush()
    return rec


async def _seed_manual_position() -> dict:
    """User + broker + OPEN LIVE position with broker_account_id bound."""
    async with SessionLocal() as db:
        user = await _create_user(db, "manual")
        broker = await _create_broker(db, user.id)
        pos = PositionRecord(
            id=str(uuid.uuid4()),
            user_id=user.id,
            broker_account_id=broker.id,
            symbol="RELIANCE",
            side="LONG",
            quantity=10,
            entry_price=2500.0,
            current_price=2520.0,
            realized_pnl=0.0,
            unrealized_pnl=200.0,
            mode="LIVE",
            status="OPEN",
        )
        db.add(pos)
        await db.commit()
    token = create_access_token({"sub": user.id, "email": user.email, "role": user.role})
    return {
        "user_id": user.id,
        "broker_id": broker.id,
        "position_id": pos.id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


async def _seed_copy_scenario(follower_mode: str = "LIVE") -> dict:
    """Master + follower + broker + group + OPEN follower position."""
    async with SessionLocal() as db:
        master = await _create_user(db, "master")
        follower = await _create_user(db, "follower")
        broker = await _create_broker(db, follower.id)
        group = CopyGroupRecord(
            master_user_id=master.id,
            name="CAS Group",
            is_active=True,
        )
        db.add(group)
        await db.flush()

        follower_row = CopyFollowerRecord(
            group_id=group.id,
            follower_user_id=follower.id,
            mode=follower_mode,
            broker_account_id=broker.id,
            multiplier=1.0,
            status="ACTIVE",
            max_allocation=1_000_000.0,
        )
        db.add(follower_row)
        await db.flush()

        pos = PositionRecord(
            id=str(uuid.uuid4()),
            user_id=follower.id,
            broker_account_id=broker.id,
            symbol="NIFTY50",
            side="LONG",
            quantity=20,
            entry_price=250.0,
            current_price=255.0,
            realized_pnl=0.0,
            unrealized_pnl=100.0,
            mode=follower_mode,
            status="OPEN",
        )
        db.add(pos)
        await db.commit()
        return {
            "master_id": master.id,
            "follower_id": follower.id,
            "broker_id": broker.id,
            "follower_sub_id": follower_row.id,
            "position_id": pos.id,
        }


# --------------------------------------------------------------------------
# 1. Two simultaneous manual LIVE closes -> exactly one broker dispatch and
#    exactly one financial close is booked.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_manual_live_closes_single_dispatch(monkeypatch):
    """Two concurrent manual LIVE closes must dispatch once and book once."""
    dispatched: list = []
    adapter_calls: list = []

    class _FakeBroker:
        async def place_order(self, req):
            dispatched.append(req)
            return {"broker_order_id": "MC-1", "filled_price": 2540.0}

    def _adapter(broker_rec):
        adapter_calls.append((broker_rec.id, broker_rec.user_id))
        return _FakeBroker()

    monkeypatch.setattr("app.api.trades.get_broker_adapter", _adapter)
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    seeded = await _seed_manual_position()
    settings.broker_mode = "live"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        results = await asyncio.gather(
            client.post(
                f"/api/trades/positions/{seeded['position_id']}/close",
                headers=seeded["headers"],
            ),
            client.post(
                f"/api/trades/positions/{seeded['position_id']}/close",
                headers=seeded["headers"],
            ),
            return_exceptions=True,
        )

    statuses = [r.status_code for r in results if not isinstance(r, Exception)]
    assert len(statuses) == 2
    assert sorted(statuses) == [200, 404], f"expected one 200 and one 404, got {statuses}"

    assert len(dispatched) == 1, f"expected exactly ONE broker dispatch, got {len(dispatched)}"
    assert len(adapter_calls) == 1, f"expected exactly ONE adapter resolution, got {len(adapter_calls)}"

    async with SessionLocal() as db:
        pos = await db.get(PositionRecord, seeded["position_id"])
        assert pos.status == "CLOSED"
        assert pos.realized_pnl == round((2540.0 - 2500.0) * 10, 2)  # = 400.0

        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == seeded["user_id"])
            )
        ).scalars().all()
        assert len(trades) == 1, f"expected ONE booked trade, got {len(trades)}"
        assert trades[0].exit_reason == "MANUAL_CLOSE"


# --------------------------------------------------------------------------
# 1b. Deterministic gate: both concurrent requests are FORCED to reach the broker
#     dispatch before either may proceed.  Pre-fix BOTH dispatched (double close);
#     post-fix the CAS lets exactly one through the gate.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_manual_close_barrier_only_one_passes_gate(monkeypatch):
    """A barrier in broker dispatch must observe only ONE request passing it."""
    entered_count = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()

    class _GatedBroker:
        async def place_order(self, req):
            nonlocal entered_count
            entered_count += 1
            if entered_count >= 2:
                both_entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            return {"broker_order_id": "GATE-1", "filled_price": 2540.0}

    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda r: _GatedBroker())
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    seeded = await _seed_manual_position()
    settings.broker_mode = "live"
    url = f"/api/trades/positions/{seeded['position_id']}/close"

    async def _close_once():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(url, headers=seeded["headers"])

    t1 = asyncio.create_task(_close_once())
    t2 = asyncio.create_task(_close_once())

    # Give both tasks a chance to enter place_order.  Post-fix exactly one may
    # (the loser fails the CAS first); pre-fix both pass through to dispatch.
    try:
        await asyncio.wait_for(both_entered.wait(), timeout=8)
    except asyncio.TimeoutError:
        pass
    release.set()

    r1, r2 = await asyncio.gather(t1, t2)

    assert entered_count == 1, (
        f"exactly ONE concurrent request may dispatch a broker close, got {entered_count}"
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 404], f"expected one 200 and one 404, got {statuses}"


# --------------------------------------------------------------------------
# 2. A close attempt on an already-CLOSED position returns 404 and never
#    dispatches a broker order.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_manual_close_after_already_closed_does_not_dispatch(monkeypatch):
    """A second manual close on a CLOSED position never dispatches."""
    dispatched: list = []

    class _FakeBroker:
        async def place_order(self, req):
            dispatched.append(req)
            return {"broker_order_id": "MC-2", "filled_price": 2545.0}

    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda r: _FakeBroker())
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    seeded = await _seed_manual_position()
    settings.broker_mode = "live"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res1 = await client.post(
            f"/api/trades/positions/{seeded['position_id']}/close",
            headers=seeded["headers"],
        )
        assert res1.status_code == 200, res1.text
        res2 = await client.post(
            f"/api/trades/positions/{seeded['position_id']}/close",
            headers=seeded["headers"],
        )
        assert res2.status_code == 404, res2.text

    assert len(dispatched) == 1, f"second close must NOT dispatch, got {len(dispatched)}"

    async with SessionLocal() as db:
        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == seeded["user_id"])
            )
        ).scalars().all()
        assert len(trades) == 1, "exactly one financial close must be booked"


# --------------------------------------------------------------------------
# 3. On broker failure the CAS must be rolled back: no fabricated CLOSED / PnL.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_live_close_broker_failure_never_fabricates_close(monkeypatch):
    """If the broker close dispatch fails, the position stays OPEN (CAS rolled back)."""
    dispatched: list = []

    class _FailingBroker:
        async def place_order(self, req):
            dispatched.append(req)
            raise RuntimeError("simulated broker outage on manual close")

    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda r: _FailingBroker())
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    seeded = await _seed_manual_position()
    settings.broker_mode = "live"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/trades/positions/{seeded['position_id']}/close",
            headers=seeded["headers"],
        )

    assert res.status_code == 502, res.text
    assert len(dispatched) == 1

    async with SessionLocal() as db:
        pos = await db.get(PositionRecord, seeded["position_id"])
        assert pos.status == "OPEN", "broker failure must NOT fabricate CLOSED"
        assert pos.realized_pnl == 0.0, "no PnL may be booked without a real close"
        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == seeded["user_id"])
            )
        ).scalars().all()
        assert trades == [], "no trade may be booked on a failed close"


# --------------------------------------------------------------------------
# 4. Copy-trading concurrent LIVE close has the same single-dispatch guarantee.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_copy_live_closes_single_dispatch(monkeypatch):
    """Two concurrent copy-trading LIVE closes dispatch once and book once."""
    dispatched: list = []

    class _FakeBroker:
        async def place_order(self, req):
            dispatched.append(req)
            return {"broker_order_id": "CPY-CAS-1", "filled_price": 260.0}

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", lambda r: _FakeBroker())

    seeded = await _seed_copy_scenario(follower_mode="LIVE")
    settings.broker_mode = "live"

    results = await asyncio.gather(
        copy_trading_engine.mirror_close_position(
            symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
        ),
        copy_trading_engine.mirror_close_position(
            symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
        ),
        return_exceptions=True,
    )

    closed_counts = [
        r["closed_count"] for r in results if isinstance(r, dict)
    ]
    assert len(closed_counts) == 2
    assert sum(closed_counts) == 1, f"expected exactly ONE closed follower, got {closed_counts}"

    assert len(dispatched) == 1, f"expected exactly ONE broker dispatch, got {len(dispatched)}"

    async with SessionLocal() as db:
        pos = await db.get(PositionRecord, seeded["position_id"])
        assert pos.status == "CLOSED"
        assert pos.realized_pnl == round((260.0 - 250.0) * 20, 2)  # = 200.0

        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == seeded["follower_id"])
            )
        ).scalars().all()
        assert len(trades) == 1, f"expected ONE booked exit trade, got {len(trades)}"
        assert trades[0].exit_reason == "MASTER_SIGNAL_EXIT"


# --------------------------------------------------------------------------
# 5. Copy-trading broker failure reverts the CAS: no fabricated CLOSED / PnL.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_live_close_broker_failure_never_fabricates_close(monkeypatch):
    """Copy-trading broker failure reverts the CAS so no CLOSED/PnL is fabricated."""
    dispatched: list = []

    class _FailingBroker:
        async def place_order(self, req):
            dispatched.append(req)
            raise RuntimeError("simulated copy-close broker outage")

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", lambda r: _FailingBroker())

    seeded = await _seed_copy_scenario(follower_mode="LIVE")
    settings.broker_mode = "live"

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert outcome["closed_count"] == 0
    assert len(dispatched) == 1

    async with SessionLocal() as db:
        pos = await db.get(PositionRecord, seeded["position_id"])
        assert pos.status == "OPEN", "copy close broker failure must NOT fabricate CLOSED"
        assert pos.realized_pnl == 0.0
        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == seeded["follower_id"])
            )
        ).scalars().all()
        assert trades == [], "no exit trade may be booked on a failed copy close"
        rejected = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"],
                    OrderRecord.mode == "LIVE",
                    OrderRecord.status == "REJECTED",
                )
            )
        ).scalars().all()
        assert len(rejected) == 1, "a REJECTED close order must be persisted on broker failure"


# --------------------------------------------------------------------------
# 6. Legitimate single PAPER close still works (unchanged behavior).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_single_close_still_works():
    """A PAPER position closes with exactly one booked trade (no broker involved)."""
    seeded = await _seed_copy_scenario(follower_mode="PAPER")

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=260.0,
    )

    assert outcome["closed_count"] == 1

    async with SessionLocal() as db:
        pos = await db.get(PositionRecord, seeded["position_id"])
        assert pos.status == "CLOSED"
        assert pos.realized_pnl == round((260.0 - 250.0) * 20, 2)  # = 200.0
        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == seeded["follower_id"])
            )
        ).scalars().all()
        assert len(trades) == 1, "PAPER close must book exactly one trade"
        order_rows = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"],
                    OrderRecord.mode == "PAPER",
                )
            )
        ).scalars().all()
        # PAPER closes are pure bookkeeping — no broker OrderRecord is created
        # (matches pre-fix behavior).
        assert order_rows == []

