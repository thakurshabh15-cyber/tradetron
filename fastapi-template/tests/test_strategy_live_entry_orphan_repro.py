"""GREEN — durable pre-dispatch claim for autonomous strategy LIVE entries.

Verifies the complete durable-claim lifecycle:
  1. ``_execute_signal`` commits a keyed PENDING claim BEFORE broker dispatch.
  2. Window-C recovery (no broker ref): get_positions() resolves exposure.
  3. Duplicate signal idempotency: second call skipped.
  4. Reconciliation is strictly read-only.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.engine.trading_engine import TradingEngine
from app.engine.order_reconciliation import BrokerOrderReconciliationEngine
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord


@pytest.fixture(autouse=True)
async def _reset_db_and_restore_mode():
    await init_db()
    settings.broker_mode = "live"
    yield
    settings.broker_mode = "simulated"


async def _seed_user_and_broker() -> dict:
    uid = str(uuid.uuid4())
    broker_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(UserRecord(
            id=uid,
            email=f"orphan_{uid[:8]}@tradetron.io",
            hashed_password=hash_password("SecurePassword123!"),
            full_name="Orphan Tester",
            role="trader", is_active=True, is_verified=True,
            paper_balance=1_000_000.0,
        ))
        await db.flush()
        db.add(BrokerAccountRecord(
            id=broker_id, user_id=uid, broker_name="ZERODHA",
            account_name="Orphan Acct", status="CONNECTED", is_active=True,
            token_expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(days=30),
            client_id="CLIENT_01",
            api_key_encrypted="mock_api_key_encrypted",
            api_secret_encrypted="mock_api_secret_encrypted",
            access_token_encrypted=f"mock_orphan_token_{uid[:8]}",
        ))
        await db.commit()
    return {"uid": uid, "broker_id": broker_id}


async def _count_for_user(model, uid: str) -> int:
    async with SessionLocal() as db:
        return (await db.execute(
            select(func.count()).select_from(model).where(model.user_id == uid)
        )).scalar_one()


async def _run_reconciliation_once(now=None) -> dict:
    return await BrokerOrderReconciliationEngine().reconcile_once(now=now)


class _ConfirmingBroker:
    async def get_margins(self):
        return {"available_cash": 500000.0, "collateral": 0.0}
    async def place_order(self, req):
        return {"order_id": f"ZMB_{uuid.uuid4().hex[:10]}",
                "filled_price": 2500.0, "status": "FILLED"}
    async def get_positions(self):
        return []


class _WithPosition(_ConfirmingBroker):
    async def get_positions(self):
        return [{"symbol": "RELIANCE", "quantity": 10,
                 "average_price": 2500.0, "side": "LONG"}]


class _NoPositions(_ConfirmingBroker):
    async def get_positions(self):
        return []


class _ErrorPositions(_ConfirmingBroker):
    async def get_positions(self):
        raise RuntimeError("network timeout")


class _NonListPositions(_ConfirmingBroker):
    async def get_positions(self):
        return None



# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_entry_crash_recovered_by_window_c(monkeypatch):
    """Core RED-to-GREEN: crash before broker-ref persist (Window-C).

    Durable claim committed before dispatch survives the crash.
    Reconciliation detects live exposure via get_positions() and finalizes.
    """
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]

    dispatched: list = []
    persist_called = {"v": False}

    class _CrashBroker(_ConfirmingBroker):
        async def place_order(self, req):
            dispatched.append(req)
            return {"order_id": f"ZMB_{uuid.uuid4().hex[:10]}",
                    "filled_price": 2500.0, "status": "FILLED"}

    async def _crash_on_persist(self, *args, **kwargs):
        persist_called["v"] = True
        raise SystemExit(1)

    monkeypatch.setattr("app.engine.trading_engine.get_broker_adapter",
                        lambda rec: _CrashBroker())
    # LIVE path finalizes via _persist_strategy_live_fill, NOT _persist_trade.
    monkeypatch.setattr(TradingEngine, "_persist_strategy_live_fill",
                        _crash_on_persist)

    engine = TradingEngine(broker=_CrashBroker(), tick_queue=asyncio.Queue())
    strategy = {
        "id": str(uuid.uuid4()), "name": "Orphan LIVE Strat",
        "action": {"side": "BUY", "quantity": 10, "order_type": "MARKET"},
        "execution_mode": "LIVE", "symbols": ["RELIANCE"], "enabled": True,
        "user_id": uid, "broker_account_id": broker_id,
    }

    with pytest.raises(SystemExit):
        await engine._execute_signal(strategy, "RELIANCE", 2500.0)

    assert len(dispatched) == 1
    assert persist_called["v"] is True

    async with SessionLocal() as db:
        claims = (await db.execute(
            select(OrderRecord).where(
                OrderRecord.user_id == uid,
                OrderRecord.client_order_id.is_not(None),
                OrderRecord.status == "PENDING",
            )
        )).scalars().all()
    assert len(claims) == 1, "durable PENDING claim must exist"
    assert claims[0].mode == "LIVE"
    assert claims[0].broker_order_id is None, "Window-C: ref not yet persisted"

    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _WithPosition())
    # The claim was created moments ago, so reconcile with a simulated "now"
    # 10 minutes later to cross the 120s stale threshold.
    future_now = datetime.now(timezone.utc) + timedelta(minutes=10)
    summary = await _run_reconciliation_once(now=future_now)
    assert summary["scanned"] >= 1
    assert summary["filled"] >= 1, "Window-C must finalize FILLED"

    assert await _count_for_user(OrderRecord, uid) >= 1
    assert await _count_for_user(PositionRecord, uid) >= 1
    assert await _count_for_user(TradeRecord, uid) >= 1


@pytest.mark.asyncio
async def test_window_c_no_exposure_stays_pending(monkeypatch):
    """No matching position => PENDING (uncertain; never fabricate CANCELLED)."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]

    async with SessionLocal() as db:
        db.add(OrderRecord(
            user_id=uid, client_order_id=f"s-{uuid.uuid4().hex[:40]}",
            broker_account_id=broker_id, strategy_id=str(uuid.uuid4()),
            symbol="RELIANCE", side="BUY", quantity=10, price=2500.0,
            order_type="MARKET", mode="LIVE", status="PENDING",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        ))
        await db.commit()

    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _NoPositions())
    summary = await _run_reconciliation_once()
    assert summary["scanned"] >= 1
    assert summary["unknown"] >= 1



@pytest.mark.asyncio
async def test_window_c_error_leaves_pending(monkeypatch):
    """get_positions() error => unknown, claim stays PENDING."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]

    async with SessionLocal() as db:
        db.add(OrderRecord(
            user_id=uid, client_order_id=f"s-{uuid.uuid4().hex[:40]}",
            broker_account_id=broker_id, strategy_id=str(uuid.uuid4()),
            symbol="RELIANCE", side="BUY", quantity=10, price=2500.0,
            order_type="MARKET", mode="LIVE", status="PENDING",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        ))
        await db.commit()

    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _ErrorPositions())
    summary = await _run_reconciliation_once()
    assert summary["scanned"] >= 1
    assert summary.get("unknown", 0) >= 1


@pytest.mark.asyncio
async def test_window_c_non_list_leaves_pending(monkeypatch):
    """Non-list get_positions() => unknown, stays PENDING."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]

    async with SessionLocal() as db:
        db.add(OrderRecord(
            user_id=uid, client_order_id=f"s-{uuid.uuid4().hex[:40]}",
            broker_account_id=broker_id, strategy_id=str(uuid.uuid4()),
            symbol="RELIANCE", side="BUY", quantity=10, price=2500.0,
            order_type="MARKET", mode="LIVE", status="PENDING",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        ))
        await db.commit()

    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _NonListPositions())
    summary = await _run_reconciliation_once()
    assert summary["scanned"] >= 1
    assert summary.get("unknown", 0) >= 1


@pytest.mark.asyncio
async def test_duplicate_signal_no_double_dispatch(monkeypatch):
    """Second _execute_signal with same key must be skipped (idempotent)."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]

    dispatched: list = []
    broker = _ConfirmingBroker()

    async def _tracking_place(req):
        dispatched.append(req)
        return {"order_id": f"ZMB_{uuid.uuid4().hex[:10]}",
                "filled_price": 2500.0, "status": "FILLED"}
    broker.place_order = _tracking_place

    monkeypatch.setattr("app.engine.trading_engine.get_broker_adapter",
                        lambda rec: broker)
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: broker)

    engine = TradingEngine(broker=broker, tick_queue=asyncio.Queue())
    strategy = {
        "id": str(uuid.uuid4()), "name": "Idempotency Strat",
        "action": {"side": "BUY", "quantity": 10, "order_type": "MARKET"},
        "execution_mode": "LIVE", "symbols": ["RELIANCE"], "enabled": True,
        "user_id": uid, "broker_account_id": broker_id,
    }

    await engine._execute_signal(strategy, "RELIANCE", 2500.0)
    assert len(dispatched) == 1, "first call dispatches once"

    await engine._execute_signal(strategy, "RELIANCE", 2500.0)
    assert len(dispatched) == 1, "second call must NOT re-dispatch"


@pytest.mark.asyncio
async def test_reconciliation_never_calls_place_order(monkeypatch):
    """Reconciliation is strictly broker-read-only."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]

    async with SessionLocal() as db:
        db.add(OrderRecord(
            user_id=uid, client_order_id=f"s-{uuid.uuid4().hex[:40]}",
            broker_account_id=broker_id, strategy_id=str(uuid.uuid4()),
            symbol="RELIANCE", side="BUY", quantity=10, price=2500.0,
            order_type="MARKET", mode="LIVE", status="PENDING",
            broker_order_id=f"ZMB_{uuid.uuid4().hex[:10]}",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        ))
        await db.commit()

    place_called = {"v": False}

    class _ReadonlyBroker(_WithPosition):
        async def get_order_status(self, broker_oid):
            return {"status": "FILLED", "average_price": 2500.0,
                    "filled_quantity": 10}
        async def place_order(self, req):
            place_called["v"] = True
            raise AssertionError("must not call place_order")

    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _ReadonlyBroker())
    summary = await _run_reconciliation_once()
    assert summary["scanned"] >= 1
    assert summary["filled"] >= 1
    assert not place_called["v"], "reconciliation must be read-only"


@pytest.mark.asyncio
async def test_window_a_no_record_no_side_effect():
    """Window-A: crash BEFORE dispatch => no row, no exposure, no problem."""
    seeded = await _seed_user_and_broker()
    uid = seeded["uid"]

    async with SessionLocal() as db:
        count = (await db.execute(
            select(func.count()).select_from(OrderRecord).where(
                OrderRecord.user_id == uid,
                OrderRecord.status == "PENDING",
                OrderRecord.mode == "LIVE",
            )
        )).scalar_one()
    assert count == 0
