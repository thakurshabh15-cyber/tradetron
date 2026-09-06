"""P1 regression tests: the LIVE close CAS claim must be DURABLE (committed)
BEFORE the broker dispatch runs.

Defect (P1, crash-window): both ``close_position`` (``app/api/trades.py``) and
``_close_single_follower_position`` (``app/engine/copy_trading.py``) executed
the atomic CAS claim (``UPDATE positions SET status='CLOSED' WHERE
status='OPEN'``) inside the request transaction but did NOT commit it before
dispatching the real broker close order:

    CAS claim (uncommitted)  ->  broker dispatch  ->  PnL booking + commit

If the process crashes between the broker dispatch and the final commit (OOM
kill, power loss, DB outage), the DB transaction rolls back and the position
reverts to OPEN locally while the exchange has ALREADY closed it.  A restart
or manual retry then sees OPEN and dispatches a SECOND broker close order on a
position that no longer exists on the exchange -- a double close that can
synthesize unintended opposite-direction inventory (real money moved twice).

The concurrent-close CAS fix (test_concurrent_close_cas.py) closed the
*two-requests race* but deliberately left the dispatch running against an
uncommitted CAS ("for LIVE positions the CAS claim is uncommitted when the
broker dispatch runs").  The crash window above is the residual defect.

Fix: commit the CAS claim immediately after the atomic UPDATE, BEFORE any
broker dispatch -- mirroring the entry side (PENDING durably committed before
dispatch).  A crash after the CAS commit but before dispatch leaves a
"phantom close" (DB=CLOSED, exchange=OPEN), which is recoverable by manual
intervention / reconciliation and can NEVER trigger a second broker action.
A crash on the pre-fix path left DB=OPEN with exchange=CLOSED, which
unavoidably re-dispatches on retry.

These tests make the durability observable: the fake broker's ``place_order``
reads the position through a SEPARATE database session.  If the CAS were still
uncommitted in the request transaction, the separate session would observe
OPEN during dispatch; after the fix it must observe CLOSED.
"""

from __future__ import annotations

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
        email=f"casdur_{tag}_{uid[:8]}@tradetron.io",
        hashed_password=hash_password("SecurePassword123!"),
        full_name="CAS Durability Tester",
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
        account_name="Durability Account",
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
            name="CAS Durability Group",
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
# ---------------------------------------------------------------------------
# 1. Manual LIVE close: a separate session opened INSIDE the broker dispatch
#    must already observe the CAS claim durably committed (status == CLOSED).
#    Pre-fix the claim was uncommitted during dispatch -> separate observer
#    read OPEN (double-close window); post-fix it reads CLOSED.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_live_close_cas_committed_before_dispatch(monkeypatch):
    """The CAS must be durable before the broker sees the close order."""
    observed_statuses: list[str] = []

    class _ObservingBroker:
        async def place_order(self, req):
            # Read through a SEPARATE session: if the CAS were still uncommitted
            # in the request transaction, this sees OPEN during dispatch.
            async with SessionLocal() as obs_db:
                obs = await obs_db.get(PositionRecord, seeded["position_id"])
                observed_statuses.append(obs.status if obs else None)
            return {"broker_order_id": "DUR-1", "filled_price": 2540.0}

    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda r: _ObservingBroker())
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    seeded = await _seed_manual_position()
    settings.broker_mode = "live"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/trades/positions/{seeded['position_id']}/close",
            headers=seeded["headers"],
        )

    assert res.status_code == 200, res.text
    # The observer inside place_order MUST have seen the CAS already committed.
    assert observed_statuses == ["CLOSED"], (
        "CAS claim must be durably committed BEFORE broker dispatch; "
        f"observer saw statuses {observed_statuses} (pre-fix this is the "
        "double-close window: exchange CLOSED but local ledger OPEN)"
    )


# ---------------------------------------------------------------------------
# 2. Copy-trading LIVE close: same durability guarantee for the follower path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_live_close_cas_committed_before_dispatch(monkeypatch):
    """The copy-trading CAS must also be durable before broker dispatch."""
    observed_statuses: list[str] = []

    class _ObservingBroker:
        async def place_order(self, req):
            async with SessionLocal() as obs_db:
                obs = await obs_db.get(PositionRecord, seeded["position_id"])
                observed_statuses.append(obs.status if obs else None)
            return {"broker_order_id": "CPY-DUR-1", "filled_price": 260.0}

    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", lambda r: _ObservingBroker())
    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)

    seeded = await _seed_copy_scenario(follower_mode="LIVE")
    settings.broker_mode = "live"

    result = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50",
        master_user_id=seeded["master_id"],
        exit_price=260.0,
    )

    assert result.get("mirrored") is True, result
    assert observed_statuses == ["CLOSED"], (
        "copy-trading CAS claim must be durably committed BEFORE broker "
        f"dispatch; observer saw {observed_statuses}"
    )


# ---------------------------------------------------------------------------
# 3. Broker failure AFTER the CAS commit must re-OPEN the position so a retry
#    remains possible (and no fabricated CLOSED/PnL exists).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_live_close_broker_failure_reopens_after_cas_commit(monkeypatch):
    """Explicit re-open on broker failure keeps the position retryable."""
    dispatched: list = []

    class _FailingBroker:
        async def place_order(self, req):
            dispatched.append(req)
            raise RuntimeError("simulated outage after CAS was committed")

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
        assert pos.status == "OPEN", (
            "after broker failure the already-committed CAS must be explicitly "
            "reverted so the position stays retryable"
        )
        assert pos.closed_at is None
        assert pos.realized_pnl == 0.0
        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == seeded["user_id"])
            )
        ).scalars().all()
        assert trades == [], "no trade may be booked on a failed close"