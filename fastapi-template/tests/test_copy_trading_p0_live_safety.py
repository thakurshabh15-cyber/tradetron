"""P0-1 regression tests: copy-trading LIVE fill safety + broker_account_id ownership.

Target P0: 'Copy-trading phantom LIVE fills + cross-tenant broker_account_id
reference'.  These tests pin the MINIMAL SAFE REMEDIATION:

  1. join_copy_group rejects a cross-tenant broker_account_id (403).
  2. update_following_settings rejects a cross-tenant broker_account_id (403).
  3. a follower's OWN connected broker account is accepted.
  4. a LIVE follower can never fabricate FILLED/OPEN state when live dispatch
     is blocked (BROKER_MODE=simulated -> REJECTED only).
  5. a LIVE follower fan-out definitely invokes assert_live_dispatch_allowed().
  6. a successful (mocked) broker dispatch persists the correct FILLED state.
  7. a broker dispatch failure persists REJECTED - never FILLED/OPEN.
  8. PAPER follower behavior is unchanged (bookkeeping FILLED, no broker).
  9. the engine uses server-derived follower identity - it can never route a
     LIVE fill through another user's broker account.
  10. no real broker/network calls: broker interaction is fully mocked and
      recorded, and BROKER_MODE stays "simulated" throughout.

All broker dispatch is faked; nothing here contacts a real broker or network.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.engine.copy_trading import copy_trading_engine
from app.main import app

from app.brokers import BrokerModeBlockedError
from app.config import settings
from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal, init_db
from app.models.broker_account import BrokerAccountRecord
from app.models.copy_trading import CopyFollowerRecord, CopyGroupRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord


@pytest.fixture(autouse=True)
async def _reset_db_and_simulated_mode():
    """Ensure schema exists and LIVE dispatch is always blocked by default."""
    await init_db()
    settings.broker_mode = "simulated"
    yield
    settings.broker_mode = "simulated"


@pytest.fixture(autouse=True)
def _mock_notify_trade_fill(monkeypatch):
    """No Telegram/HTTP I/O from fill notifications during tests."""
    monkeypatch.setattr("app.engine.alerts.notify_trade_fill", AsyncMock())


def _token(user: UserRecord) -> str:
    return create_access_token({"sub": user.id, "email": user.email, "role": user.role})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_user(db) -> UserRecord:
    uid = str(uuid.uuid4())
    rec = UserRecord(
        id=uid,
        email=f"p0_{uid[:8]}@tradetron.io",
        hashed_password=hash_password("Pass12345!"),
        full_name="P0 Tester",
        role="trader",
        is_active=True,
        is_verified=True,
        paper_balance=1_000_000.0,
    )
    db.add(rec)
    await db.flush()
    return rec
async def _create_broker_account(db, user_id: str, status: str = "CONNECTED") -> BrokerAccountRecord:
    rec = BrokerAccountRecord(
        user_id=user_id,
        broker_name="SIMULATED",
        account_name="P0 Test Account",
        client_id="CLIENT_01",
        status=status,
        is_active=True,
    )
    rec.set_credentials(
        api_key="P0TESTKEY123", api_secret="P0TESTSECRET123", access_token="P0TOKEN123"
    )
    db.add(rec)
    await db.flush()
    return rec


async def _create_group(db, master_user_id: str) -> CopyGroupRecord:
    rec = CopyGroupRecord(master_user_id=master_user_id, name="P0 Safety Group")
    db.add(rec)
    await db.flush()
    return rec


async def _follower_snapshot(follower: CopyFollowerRecord, broker_account_id: str | None) -> SimpleNamespace:
    """Detached snapshot of the follower row the engine reads (avoids detached-instance errors)."""
    return SimpleNamespace(
        id=follower.id,
        follower_user_id=follower.follower_user_id,
        mode=follower.mode,
        broker_account_id=broker_account_id,
        multiplier=follower.multiplier,
        max_allocation=follower.max_allocation,
        total_copied_trades=follower.total_copied_trades or 0,
    )
# ── 1/2/3. API-level broker_account_id ownership gate ─────────────────────────


@pytest.mark.asyncio
async def test_join_rejects_cross_tenant_broker_account_id():
    client = TestClient(app)
    async with SessionLocal() as db:
        master = await _create_user(db)
        follower = await _create_user(db)
        other = await _create_user(db)
        other_broker = await _create_broker_account(db, other.id)
        group = await _create_group(db, master.id)
        await db.commit()
        follower_token = _token(follower)
        invite_code = group.invite_code
        other_broker_id = other_broker.id

    resp = client.post(
        "/api/copy-trading/join",
        headers=_auth(follower_token),
        json={
            "invite_code": invite_code,
            "mode": "LIVE",
            "broker_account_id": other_broker_id,
            "multiplier": 1.0,
            "max_allocation": 50000.0,
        },
    )
    assert resp.status_code == 403, resp.text

    async with SessionLocal() as db:
        subs = (
            await db.execute(
                select(CopyFollowerRecord).where(CopyFollowerRecord.follower_user_id == follower.id)
            )
        ).scalars().all()
        assert subs == []
@pytest.mark.asyncio
async def test_join_accepts_own_broker_account_id():
    client = TestClient(app)
    async with SessionLocal() as db:
        master = await _create_user(db)
        follower = await _create_user(db)
        own_broker = await _create_broker_account(db, follower.id)
        group = await _create_group(db, master.id)
        await db.commit()
        follower_token = _token(follower)
        invite_code = group.invite_code
        own_broker_id = own_broker.id

    resp = client.post(
        "/api/copy-trading/join",
        headers=_auth(follower_token),
        json={
            "invite_code": invite_code,
            "mode": "LIVE",
            "broker_account_id": own_broker_id,
            "multiplier": 1.5,
            "max_allocation": 75000.0,
        },
    )
    assert resp.status_code == 200, resp.text
    sub_id = resp.json()["follower_id"]

    async with SessionLocal() as db:
        follower_row = await db.get(CopyFollowerRecord, sub_id)
        assert follower_row is not None
        assert follower_row.follower_user_id == follower.id
        assert follower_row.broker_account_id == own_broker_id
        assert follower_row.mode == "LIVE"
@pytest.mark.asyncio
async def test_update_rejects_cross_tenant_broker_account_id():
    client = TestClient(app)
    async with SessionLocal() as db:
        master = await _create_user(db)
        follower = await _create_user(db)
        other = await _create_user(db)
        own_broker = await _create_broker_account(db, follower.id)
        other_broker = await _create_broker_account(db, other.id)
        group = await _create_group(db, master.id)
        follower_row = CopyFollowerRecord(
            group_id=group.id,
            follower_user_id=follower.id,
            mode="LIVE",
            broker_account_id=own_broker.id,
            multiplier=1.0,
            status="ACTIVE",
            max_allocation=50000.0,
        )
        db.add(follower_row)
        await db.commit()
        follower_token = _token(follower)
        sub_id = follower_row.id
        own_broker_id = own_broker.id
        other_broker_id = other_broker.id

    resp = client.patch(
        f"/api/copy-trading/following/{sub_id}",
        headers=_auth(follower_token),
        json={"mode": "LIVE", "broker_account_id": other_broker_id},
    )
    assert resp.status_code == 403, resp.text

    async with SessionLocal() as db:
        follower_row = await db.get(CopyFollowerRecord, sub_id)
        assert follower_row.broker_account_id == own_broker_id

    resp = client.patch(
        f"/api/copy-trading/following/{sub_id}",
        headers=_auth(follower_token),
        json={"broker_account_id": own_broker_id},
    )
    assert resp.status_code == 200, resp.text
@pytest.mark.asyncio
async def test_join_resume_rejects_cross_tenant_broker_account_id():
    client = TestClient(app)
    async with SessionLocal() as db:
        master = await _create_user(db)
        follower = await _create_user(db)
        other = await _create_user(db)
        other_broker = await _create_broker_account(db, other.id)
        group = await _create_group(db, master.id)
        follower_row = CopyFollowerRecord(
            group_id=group.id,
            follower_user_id=follower.id,
            mode="PAPER",
            broker_account_id=None,
            multiplier=1.0,
            status="STOPPED",
            max_allocation=50000.0,
        )
        db.add(follower_row)
        await db.commit()
        follower_token = _token(follower)
        invite_code = group.invite_code
        other_broker_id = other_broker.id

    resp = client.post(
        "/api/copy-trading/join",
        headers=_auth(follower_token),
        json={
            "invite_code": invite_code,
            "mode": "LIVE",
            "broker_account_id": other_broker_id,
            "multiplier": 1.0,
            "max_allocation": 50000.0,
        },
    )
    assert resp.status_code == 403, resp.text

    async with SessionLocal() as db:
        follower_row = (
            await db.execute(
                select(CopyFollowerRecord).where(CopyFollowerRecord.follower_user_id == follower.id)
            )
        ).scalars().one()
        assert follower_row.broker_account_id is None
        assert follower_row.status == "STOPPED"  # resume did not happen
# ── 4/5/6/7/8/9. Engine-level LIVE/PAPER dispatch safety ─────────────────────


async def _setup_engine_scenario(follower_mode: str):
    """Create a master + follower (with follower-owned broker) for engine tests."""
    async with SessionLocal() as db:
        master = await _create_user(db)
        follower = await _create_user(db)
        broker = await _create_broker_account(db, follower.id)
        group = await _create_group(db, master.id)
        follower_row = CopyFollowerRecord(
            group_id=group.id,
            follower_user_id=follower.id,
            mode=follower_mode,
            broker_account_id=broker.id,
            multiplier=2.0,
            status="ACTIVE",
            max_allocation=1_000_000.0,
        )
        db.add(follower_row)
        await db.commit()
        snap = await _follower_snapshot(follower_row, broker.id)
        return snap, broker.id, follower.id


@pytest.mark.asyncio
async def test_live_follower_blocked_never_fabricates_fill(monkeypatch):
    """BROKER_MODE=simulated + real guard => REJECTED, no FILLED/OPEN, no dispatch."""
    broker_calls: list = []

    async def _fail_if_called(broker_rec):
        broker_calls.append(broker_rec)
        raise AssertionError("adapter must never be resolved when the guard blocks")

    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _fail_if_called)

    snap, broker_id, user_id = await _setup_engine_scenario("LIVE")

    outcome = await copy_trading_engine._execute_single_follower_order(
        follower=snap, symbol="NIFTY50", side="BUY", master_qty=10,
        order_type="MARKET", price=250.0, master_mode="LIVE",
        master_order_id="M1",
    )

    assert outcome["success"] is False
    assert outcome["reason"] == "live_dispatch_blocked"
    assert broker_calls == [], "broker adapter must not be touched when BROKER_MODE != live"

    async with SessionLocal() as db:
        orders = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == user_id,
                    OrderRecord.symbol == "NIFTY50",
                    OrderRecord.mode == "LIVE",
                )
            )
        ).scalars().all()
        assert len(orders) == 1
        assert orders[0].status == "REJECTED"
        assert orders[0].error_message == "live_dispatch_blocked"
        assert orders[0].broker_account_id is None  # REJECTED claim carries no broker ref
        assert orders[0].filled_quantity == 0
        assert orders[0].filled_price is None
        positions = (
            await db.execute(select(PositionRecord).where(PositionRecord.user_id == user_id))
        ).scalars().all()
        trades = (
            await db.execute(select(TradeRecord).where(TradeRecord.user_id == user_id))
        ).scalars().all()
        assert positions == [], "no fabricated OPEN position"
        assert trades == [], "no fabricated trade record"
@pytest.mark.asyncio
async def test_live_follower_invokes_assert_live_dispatch_allowed(monkeypatch):
    """The LIVE fan-out must invoke the live-dispatch guard before any broker call."""
    guard_called = {"v": False}

    def _guarded_guard():
        guard_called["v"] = True
        raise BrokerModeBlockedError("LIVE broker order dispatch blocked: BROKER_MODE is not 'live'.")

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", _guarded_guard)

    async def _fail_if_called(broker_rec):
        raise AssertionError("adapter must never be resolved before the guard passes")

    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _fail_if_called)

    snap, broker_id, user_id = await _setup_engine_scenario("LIVE")

    outcome = await copy_trading_engine._execute_single_follower_order(
        follower=snap, symbol="NIFTY50", side="BUY", master_qty=10,
        order_type="MARKET", price=250.0, master_mode="LIVE",
        master_order_id="M2",
    )

    assert guard_called["v"] is True, "assert_live_dispatch_allowed was never invoked"
    assert outcome["success"] is False
    assert outcome["reason"] == "live_dispatch_blocked"
@pytest.mark.asyncio
async def test_live_follower_successful_dispatch_persists_correct_state(monkeypatch):
    """Successful mocked broker dispatch -> FILLED order / OPEN position / trade."""
    dispatched: list = []

    class _FakeBroker:
        async def place_order(self, req):
            dispatched.append(req)
            return {
                "broker_order_id": "BROKER-777",
                "status": "FILLED",
                "filled_price": 123.45,
                "filled_quantity": req.quantity,
            }

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", lambda broker_rec: _FakeBroker())

    snap, broker_id, user_id = await _setup_engine_scenario("LIVE")

    outcome = await copy_trading_engine._execute_single_follower_order(
        follower=snap, symbol="NIFTY50", side="BUY", master_qty=10,
        order_type="MARKET", price=250.0, master_mode="LIVE",
        master_order_id="M3",
    )

    assert outcome["success"] is True, outcome
    assert len(dispatched) == 1
    assert dispatched[0].symbol == "NIFTY50"
    async with SessionLocal() as db:
            order = (
                await db.execute(
                    select(OrderRecord).where(
                        OrderRecord.user_id == user_id,
                        OrderRecord.symbol == "NIFTY50",
                        OrderRecord.mode == "LIVE",
                    )
                )
            ).scalars().one()
            assert order.status == "FILLED"
            assert order.broker_account_id == broker_id
            assert order.broker_order_id == "BROKER-777"
            assert order.filled_price == 123.45

            position = (
                await db.execute(
                    select(PositionRecord).where(
                        PositionRecord.user_id == user_id,
                        PositionRecord.status == "OPEN",
                    )
                )
            ).scalars().one()
            assert position.broker_account_id == broker_id
            assert position.entry_price == 123.45

            trade = (
                await db.execute(
                    select(TradeRecord).where(
                        TradeRecord.user_id == user_id,
                        TradeRecord.symbol == "NIFTY50",
                    )
                )
            ).scalars().one()
            assert trade.price == 123.45

            follower_row = (
                await db.execute(
                    select(CopyFollowerRecord).where(CopyFollowerRecord.follower_user_id == user_id)
                )
            ).scalars().one()
            assert follower_row.total_copied_trades == 1
    assert dispatched[0].quantity == 20  # 10 * 2.0 multiplier
    assert dispatched[0].side.value == "BUY"
@pytest.mark.asyncio
async def test_live_follower_dispatch_failure_never_fabricates_fill(monkeypatch):
    """Broker dispatch exception -> REJECTED order, no FILLED/OPEN, no trade."""
    dispatched: list = []

    class _FailingBroker:
        async def place_order(self, req):
            dispatched.append(req)
            raise RuntimeError("simulated broker outage")

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", lambda broker_rec: _FailingBroker())

    snap, broker_id, user_id = await _setup_engine_scenario("LIVE")

    outcome = await copy_trading_engine._execute_single_follower_order(
        follower=snap, symbol="NIFTY50", side="BUY", master_qty=10,
        order_type="MARKET", price=250.0, master_mode="LIVE",
        master_order_id="M4",
    )

    assert outcome["success"] is False
    assert outcome["reason"] == "broker_dispatch_failed"
    assert len(dispatched) == 1, "the follower's own adapter was invoked once"

    async with SessionLocal() as db:
        order = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == user_id,
                    OrderRecord.symbol == "NIFTY50",
                    OrderRecord.mode == "LIVE",
                )
            )
        ).scalars().one()
        assert order.status == "REJECTED"
        assert order.error_message == "broker_dispatch_failed"
        assert order.broker_account_id is None  # REJECTED claim carries no broker ref
        positions = (
            await db.execute(select(PositionRecord).where(PositionRecord.user_id == user_id))
        ).scalars().all()
        trades = (
            await db.execute(select(TradeRecord).where(TradeRecord.user_id == user_id))
        ).scalars().all()
        assert positions == []
        assert trades == []
@pytest.mark.asyncio
async def test_paper_follower_unchanged_no_broker_involved(monkeypatch):
    """PAPER fan-out must keep bookkeeping semantics and never touch a broker."""
    def _raise_if_called(*args, **kwargs):
        raise AssertionError("PAPER must never invoke the live guard or a broker adapter")

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", _raise_if_called)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _raise_if_called)

    snap, broker_id, user_id = await _setup_engine_scenario("PAPER")

    outcome = await copy_trading_engine._execute_single_follower_order(
        follower=snap, symbol="NIFTY50", side="BUY", master_qty=10,
        order_type="MARKET", price=250.0, master_mode="PAPER",
        master_order_id="M5",
    )

    assert outcome["success"] is True, outcome

    async with SessionLocal() as db:
        order = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == user_id,
                    OrderRecord.symbol == "NIFTY50",
                    OrderRecord.mode == "PAPER",
                )
            )
        ).scalars().one()
        assert order.status == "FILLED"
        assert order.broker_account_id is None  # paper never carries a broker ref
        position = (
            await db.execute(
                select(PositionRecord).where(
                    PositionRecord.user_id == user_id,
                    PositionRecord.status == "OPEN",
                )
            )
        ).scalars().one()
        assert position.mode == "PAPER"
        trade = (
            await db.execute(select(TradeRecord).where(TradeRecord.user_id == user_id))
        ).scalars().one()
        assert trade.mode == "PAPER"
async def test_engine_uses_server_derived_identity_cannot_use_other_users_broker(monkeypatch):
    '''Even with a corrupted follower row pointing elsewhere, the engine resolves
    the account scoped to follower.follower_user_id and REJECTS.'''
    def _raise_if_called(*args, **kwargs):
        raise AssertionError("must not route a LIVE fill through another user's broker")

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _raise_if_called)

    async with SessionLocal() as db:
        master = await _create_user(db)
        follower = await _create_user(db)
        other = await _create_user(db)
        other_broker = await _create_broker_account(db, other.id)
        group = await _create_group(db, master.id)
        follower_row = CopyFollowerRecord(
            group_id=group.id,
            follower_user_id=follower.id,
            mode="LIVE",
            broker_account_id=other_broker.id,  # cross-tenant reference (bad row)
            multiplier=1.0,
            status="ACTIVE",
            max_allocation=1_000_000.0,
        )
        db.add(follower_row)
        await db.commit()
        snap = await _follower_snapshot(follower_row, other_broker.id)
        user_id = follower.id

    outcome = await copy_trading_engine._execute_single_follower_order(
        follower=snap, symbol="NIFTY50", side="BUY", master_qty=10,
        order_type="MARKET", price=250.0, master_mode="LIVE",
        master_order_id="M6",
    )

    assert outcome["success"] is False
    assert outcome["reason"] == "no_owned_broker"

    async with SessionLocal() as db:
        order = (
            await db.execute(select(OrderRecord).where(OrderRecord.user_id == user_id))
        ).scalars().one()
        assert order.status == "REJECTED"
        assert order.broker_account_id is None  # no cross-tenant ref leaked
        positions = (
            await db.execute(select(PositionRecord).where(PositionRecord.user_id == user_id))
        ).scalars().all()
        trades = (
            await db.execute(select(TradeRecord).where(TradeRecord.user_id == user_id))
        ).scalars().all()
        assert positions == []
        assert trades == []
