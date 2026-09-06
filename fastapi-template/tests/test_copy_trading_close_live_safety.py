"""V3 regression tests: copy-trading LIVE close safety (mirror_close_position).

Target: 'mirror_close_position' must be LIVE-close safe:

  1. A cross-tenant broker account can never be selected for a LIVE close.
  2. The follower's OWN broker account is selected (server-derived identity).
  3. BROKER_MODE=simulated blocks a LIVE close BEFORE any broker dispatch.
  4. A LIVE close invokes assert_live_dispatch_allowed().
  5. A successful (mocked) broker close persists the correct confirmed state
     (CLOSED position at the broker fill price + FILLED close OrderRecord bound
     to the follower's own broker account + exit TradeRecord).
  6. A broker close failure persists REJECTED - never a fabricated close.
  7. PAPER close bookkeeping is unchanged (no broker involved).
  8. A PAPER master close can never become a real LIVE close for followers.
  9. Server-derived follower identity cannot be overridden by stored row/request
     data.

All broker dispatch is faked; nothing contacts a real broker or network, and
BROKER_MODE stays "simulated" throughout.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.engine.copy_trading import copy_trading_engine
from app.brokers import BrokerModeBlockedError
from app.config import settings
from app.core.security import hash_password
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


async def _create_user(db, tag: str) -> UserRecord:
    uid = str(uuid.uuid4())
    rec = UserRecord(
        id=uid,
        email=f"close_{tag}_{uid[:8]}@tradetron.io",
        hashed_password=hash_password("Pass12345!"),
        full_name="Close Safety Tester",
        role="trader",
        is_active=True,
        is_verified=True,
        paper_balance=1_000_000.0,
    )
    db.add(rec)
    await db.flush()
    return rec


async def _create_broker_account(
    db, user_id: str, status: str = "CONNECTED"
) -> BrokerAccountRecord:
    rec = BrokerAccountRecord(
        user_id=user_id,
        broker_name="SIMULATED",
        account_name="Close Safety Account",
        client_id="CLIENT_01",
        status=status,
        is_active=True,
    )
    rec.set_credentials(
        api_key="CLOSEKEY123", api_secret="CLOSESECRET123", access_token="CLOSETOKEN123"
    )
    db.add(rec)
    await db.flush()
    return rec


async def _create_group(db, master_user_id: str) -> CopyGroupRecord:
    rec = CopyGroupRecord(master_user_id=master_user_id, name="Close Safety Group")
    db.add(rec)
    await db.flush()
    return rec


async def _seed_close_scenario(
    follower_mode: str = "LIVE",
    broker_status: str = "CONNECTED",
    broker_owner: str | None = None,
    position_broker_account_id: str | None = None,
) -> dict:
    """Create a master + follower + broker + OPEN PositionRecord.

    - ``broker_owner``: if given and different from the follower, the broker is
      NOT owned by the follower (cross-tenant scenario).
    - ``position_broker_account_id``: allows seeding a stale/corrupt position
      whose stored broker_account_id differs from the follower's server-side one.
    """
    async with SessionLocal() as db:
        master = await _create_user(db, "master")
        follower = await _create_user(db, "follower")
        owner_id = broker_owner or follower.id
        broker = await _create_broker_account(db, owner_id, status=broker_status)
        group = await _create_group(db, master.id)

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

        pos_broker_id = position_broker_account_id or broker.id
        position = PositionRecord(
            user_id=follower.id,
            broker_account_id=pos_broker_id,
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
        db.add(position)
        await db.commit()

        return {
            "master_id": master.id,
            "follower_id": follower.id,
            "broker_id": broker.id,
            "follower_sub_id": follower_row.id,
            "position_id": position.id,
        }
@pytest.mark.asyncio
async def test_live_close_guard_block_never_fabricates_close(monkeypatch):
    """Item 3: BROKER_MODE=simulated blocks a LIVE close BEFORE broker dispatch."""
    broker_calls: list = []

    async def _fail_if_called(broker_rec):
        broker_calls.append(broker_rec)
        raise AssertionError("adapter must never be resolved when the guard blocks")

    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _fail_if_called)

    seeded = await _seed_close_scenario(follower_mode="LIVE")

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert outcome["mirrored"] is True
    assert outcome["closed_count"] == 0
    assert broker_calls == [], "broker adapter must not be touched when BROKER_MODE != live"

    async with SessionLocal() as db:
        position = await db.get(PositionRecord, seeded["position_id"])
        assert position.status == "OPEN", "LIVE close must NEVER be fabricated on guard block"
        assert position.realized_pnl == 0.0

        orders = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"],
                    OrderRecord.mode == "LIVE",
                )
            )
        ).scalars().all()
        assert len(orders) == 1
        assert orders[0].status == "REJECTED"
        assert (orders[0].error_message or "").startswith("(V3) LIVE close blocked")
        assert orders[0].filled_quantity == 0
        assert orders[0].filled_price is None

        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == seeded["follower_id"])
            )
        ).scalars().all()
        assert trades == [], "no fabricated exit trade on guard block"

        follower = await db.get(CopyFollowerRecord, seeded["follower_sub_id"])
        assert (follower.realized_pnl or 0.0) == 0.0


@pytest.mark.asyncio
async def test_live_close_invokes_assert_live_dispatch_allowed(monkeypatch):
    """Item 4: the LIVE close path must invoke the live-dispatch guard."""
    guard_called = {"v": False}

    def _guarded_guard():
        guard_called["v"] = True
        raise BrokerModeBlockedError("LIVE broker order dispatch blocked: BROKER_MODE is not 'live'.")

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", _guarded_guard)

    async def _fail_if_called(broker_rec):
        raise AssertionError("adapter must never be resolved when the guard blocks")

    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _fail_if_called)

    seeded = await _seed_close_scenario(follower_mode="LIVE")

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert guard_called["v"] is True, "assert_live_dispatch_allowed() was never invoked"
    assert outcome["closed_count"] == 0

    async with SessionLocal() as db:
        position = await db.get(PositionRecord, seeded["position_id"])
        assert position.status == "OPEN"
        order = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"], OrderRecord.mode == "LIVE"
                )
            )
        ).scalars().one()
        assert order.status == "REJECTED"
@pytest.mark.asyncio
async def test_live_close_successful_dispatch_persists_confirmed_state(monkeypatch):
    """Item 5: successful broker close persists the broker-confirmed state."""
    dispatched: list = []
    used_broker_accounts: list = []

    class _FakeBroker:
        async def place_order(self, req):
            dispatched.append(req)
            return {
                "broker_order_id": "CLOSE-4242",
                "status": "FILLED",
                "filled_price": 260.5,
                "filled_quantity": req.quantity,
            }

    def _adapter(broker_rec):
        used_broker_accounts.append(broker_rec.id)
        return _FakeBroker()

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _adapter)

    seeded = await _seed_close_scenario(follower_mode="LIVE")
    settings.broker_mode = "live"

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert outcome["mirrored"] is True
    assert outcome["closed_count"] == 1
    assert len(used_broker_accounts) == 1
    assert used_broker_accounts[0] == seeded["broker_id"], "close must use the follower's OWN account"
    assert len(dispatched) == 1
    assert dispatched[0].symbol == "NIFTY50"
    assert dispatched[0].side.value == "SELL", "closing side must oppose a LONG position"
    assert dispatched[0].quantity == 20
    assert dispatched[0].order_type == "MARKET"

    async with SessionLocal() as db:
        position = await db.get(PositionRecord, seeded["position_id"])
        assert position.status == "CLOSED"
        assert position.realized_pnl == round((260.5 - 250.0) * 20, 2)  # = 210.0
        assert position.broker_account_id == seeded["broker_id"]

        close_order = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"],
                    OrderRecord.mode == "LIVE",
                )
            )
        ).scalars().one()
        assert close_order.status == "FILLED"
        assert close_order.broker_account_id == seeded["broker_id"]
        assert close_order.broker_order_id == "CLOSE-4242"
        assert close_order.filled_price == 260.5
        assert close_order.filled_quantity == 20
        assert close_order.side == "SELL"

        trade = (
            await db.execute(
                select(TradeRecord).where(
                    TradeRecord.user_id == seeded["follower_id"],
                    TradeRecord.exit_reason == "MASTER_SIGNAL_EXIT",
                )
            )
        ).scalars().one()
        assert trade.price == 260.5
        assert trade.exit_price == 260.5
        assert trade.pnl == 210.0

        follower = await db.get(CopyFollowerRecord, seeded["follower_sub_id"])
        assert (follower.realized_pnl or 0.0) == 210.0
@pytest.mark.asyncio
async def test_live_close_dispatch_failure_never_fabricates_close(monkeypatch):
    """Item 6: broker failure persists REJECTED - never a fabricated close."""
    dispatched: list = []

    class _FailingBroker:
        async def place_order(self, req):
            dispatched.append(req)
            raise RuntimeError("simulated broker outage on close")

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", lambda broker_rec: _FailingBroker())

    seeded = await _seed_close_scenario(follower_mode="LIVE")
    settings.broker_mode = "live"

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert outcome["closed_count"] == 0
    assert len(dispatched) == 1, "the follower's own adapter was invoked once"

    async with SessionLocal() as db:
        position = await db.get(PositionRecord, seeded["position_id"])
        assert position.status == "OPEN", "no fabricated close on broker failure"
        assert position.realized_pnl == 0.0

        order = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"], OrderRecord.mode == "LIVE"
                )
            )
        ).scalars().one()
        assert order.status == "REJECTED"
        assert "(V3) LIVE close rejected by broker" in (order.error_message or "")

        trades = (
            await db.execute(
                select(TradeRecord).where(TradeRecord.user_id == seeded["follower_id"])
            )
        ).scalars().all()
        assert trades == []


@pytest.mark.asyncio
async def test_live_close_no_owned_connected_broker_rejected(monkeypatch):
    """Item 1/2 support: no CONNECTED owned broker => REJECTED, no close."""
    broker_calls: list = []

    async def _fail_if_called(broker_rec):
        broker_calls.append(broker_rec)
        raise AssertionError("adapter must never be resolved without an owned connected broker")

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _fail_if_called)

    seeded = await _seed_close_scenario(
        follower_mode="LIVE", broker_status="DISCONNECTED",
    )
    settings.broker_mode = "live"

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert outcome["closed_count"] == 0
    assert broker_calls == [], "no broker call without a CONNECTED owned account"

    async with SessionLocal() as db:
        position = await db.get(PositionRecord, seeded["position_id"])
        assert position.status == "OPEN"

        order = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"], OrderRecord.mode == "LIVE"
                )
            )
        ).scalars().one()
        assert order.status == "REJECTED"
        assert "no CONNECTED broker account owned by this follower" in (order.error_message or "")
        assert order.broker_account_id is None, "REJECTED close must not stamp an account ref"
@pytest.mark.asyncio
async def test_live_close_cross_tenant_broker_never_selected(monkeypatch):
    """Item 1: a cross-tenant broker account can never be selected for a LIVE close."""
    broker_calls: list = []

    async def _fail_if_called(broker_rec):
        broker_calls.append(broker_rec)
        raise AssertionError("cross-tenant broker adapter must never be resolved")

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _fail_if_called)

    async with SessionLocal() as db:
        other = await _create_user(db, "other")

    seeded = await _seed_close_scenario(
        follower_mode="LIVE", broker_owner=other.id,
    )
    settings.broker_mode = "live"

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert outcome["closed_count"] == 0
    assert broker_calls == [], "another user's broker account must never be used"

    async with SessionLocal() as db:
        position = await db.get(PositionRecord, seeded["position_id"])
        assert position.status == "OPEN", "no fabricated close through a foreign broker"

        order = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"], OrderRecord.mode == "LIVE"
                )
            )
        ).scalars().one()
        assert order.status == "REJECTED"


@pytest.mark.asyncio
async def test_paper_close_unchanged_no_broker_involved(monkeypatch):
    """Item 7: PAPER close bookkeeping is unchanged (no broker, no REJECTED order)."""
    broker_calls: list = []

    async def _fail_if_called(broker_rec):
        broker_calls.append(broker_rec)
        raise AssertionError("PAPER close must never touch a broker adapter")

    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _fail_if_called)

    seeded = await _seed_close_scenario(follower_mode="PAPER")

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert outcome["closed_count"] == 1
    assert broker_calls == []

    async with SessionLocal() as db:
        position = await db.get(PositionRecord, seeded["position_id"])
        assert position.status == "CLOSED"
        assert position.realized_pnl == round((255.0 - 250.0) * 20, 2)  # = 100.0

        orders = (
            await db.execute(
                select(OrderRecord).where(OrderRecord.user_id == seeded["follower_id"])
            )
        ).scalars().all()
        assert orders == [], "PAPER closes do not create broker OrderRecords"

        trade = (
            await db.execute(
                select(TradeRecord).where(
                    TradeRecord.user_id == seeded["follower_id"],
                    TradeRecord.exit_reason == "MASTER_SIGNAL_EXIT",
                )
            )
        ).scalars().one()
        assert trade.price == 255.0
        assert trade.mode == "PAPER"

        user = await db.get(UserRecord, seeded["follower_id"])
        assert user.paper_balance == round(1_000_000.0 + 100.0, 2)

        follower = await db.get(CopyFollowerRecord, seeded["follower_sub_id"])
        assert (follower.realized_pnl or 0.0) == 100.0
@pytest.mark.asyncio
async def test_paper_master_close_never_creates_live_follower_close(monkeypatch):
    """Item 8: a master exit cannot accidentally become a real LIVE close."""
    broker_calls: list = []

    async def _fail_if_called(broker_rec):
        broker_calls.append(broker_rec)
        raise AssertionError("simulated mode must block any LIVE close dispatch")

    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _fail_if_called)

    # LIVE follower with an OPEN LIVE position; BROKER_MODE remains simulated.
    seeded = await _seed_close_scenario(follower_mode="LIVE")

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert outcome["closed_count"] == 0
    assert broker_calls == [], "no LIVE broker dispatch may occur under BROKER_MODE=simulated"
    assert settings.broker_mode == "simulated"

    async with SessionLocal() as db:
        position = await db.get(PositionRecord, seeded["position_id"])
        assert position.status == "OPEN", "LIVE follower must not be closed by a simulated-mode fan-out"
        live_filled = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"],
                    OrderRecord.mode == "LIVE",
                    OrderRecord.status == "FILLED",
                )
            )
        ).scalars().all()
        assert live_filled == [], "no FILLED LIVE close order may be created"


@pytest.mark.asyncio
async def test_close_uses_server_derived_follower_identity(monkeypatch):
    """Item 2/9: server-derived identity cannot be overridden by a stored/request ref."""
    used_broker_accounts: list = []

    class _FakeBroker:
        async def place_order(self, req):
            return {"broker_order_id": "CLOSE-OWN-9", "status": "FILLED", "filled_price": 261.0}

    def _adapter(broker_rec):
        used_broker_accounts.append((broker_rec.id, broker_rec.user_id))
        return _FakeBroker()

    monkeypatch.setattr("app.engine.copy_trading.assert_live_dispatch_allowed", lambda: None)
    monkeypatch.setattr("app.engine.copy_trading.get_broker_adapter", _adapter)

    async with SessionLocal() as db:
        other = await _create_user(db, "other")
        other_broker = await _create_broker_account(db, other.id)
        await db.commit()
        other_broker_id = other_broker.id

    # The stored position row points at ANOTHER user's broker account (stale or
    # corrupted) - the close must ignore it and use the follower's own account.
    seeded = await _seed_close_scenario(
        follower_mode="LIVE",
        position_broker_account_id=other_broker_id,
    )
    settings.broker_mode = "live"

    outcome = await copy_trading_engine.mirror_close_position(
        symbol="NIFTY50", master_user_id=seeded["master_id"], exit_price=255.0,
    )

    assert outcome["closed_count"] == 1
    assert len(used_broker_accounts) == 1
    adapter_broker_id, adapter_broker_owner = used_broker_accounts[0]
    assert adapter_broker_id == seeded["broker_id"], "follower's OWN account must be used"
    assert adapter_broker_owner == seeded["follower_id"], "account must be owned by the follower"

    async with SessionLocal() as db:
        close_order = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == seeded["follower_id"],
                    OrderRecord.mode == "LIVE",
                    OrderRecord.status == "FILLED",
                )
            )
        ).scalars().one()
        assert close_order.broker_account_id == seeded["broker_id"]
        assert close_order.broker_account_id != other_broker_id