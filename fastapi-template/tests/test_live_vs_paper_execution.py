"""Tests for Live vs Paper Strategy Execution, Pre-Trade Margin Verification, and Emergency Kill-Switch."""

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from app.main import app, get_engine
from app.engine.risk_manager import RiskManager
from app.engine.trading_engine import TradingEngine
from app.brokers.simulated import SimulatedBroker
from app.models.trading import OrderRecord, StrategyRecord, TradeRecord
from app.models.broker_account import BrokerAccountRecord
from app.db.session import init_db, SessionLocal
from sqlalchemy import select


@pytest.mark.asyncio
async def test_live_vs_paper_mode_strategy_lifecycle():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 0. Register + authenticate a user (deploy is an authenticated action)
        import time as _time
        uid = int(_time.time() * 1000) % 1000000
        reg_res = await client.post("/api/auth/register", json={
            "email": f"live_mode_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "Live Mode Tester",
        })
        assert reg_res.status_code == 201
        token = reg_res.json()["access_token"]
        owner_id = reg_res.json()["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Create a Paper Mode strategy
        paper_res = await client.post(
            "/api/strategies",
            json={
                "name": "Paper RSI Strategy",
                "symbols": ["RELIANCE"],
                "conditions": [
                    {"indicator": "PRICE", "operator": "gt", "value": "2000.0", "period": 14}
                ],
                "action": {"side": "BUY", "quantity": 10, "order_type": "MARKET"},
                "enabled": True,
                "execution_mode": "PAPER",
                "capital_allocated": 25000.0,
            },
            headers=headers,
        )
        assert paper_res.status_code == 201
        paper_strat = paper_res.json()
        assert paper_strat["execution_mode"] == "PAPER"
        assert paper_strat["enabled"] is True

        # 2. Verify Deploying to LIVE without a broker connected FOR THIS USER
        #    returns HTTP 400 (other users' brokers must never satisfy this gate)
        live_deploy_res = await client.post(
            f"/api/strategies/{paper_strat['id']}/deploy",
            json={
                "execution_mode": "LIVE",
                "broker_name": "Zerodha",
                "multiplier": 1.0,
                "capital_allocated": 50000.0,
            },
            headers=headers,
        )
        # Should fail with 400 because no valid connected unexpired broker exists for this deployment
        assert live_deploy_res.status_code == 400
        detail = live_deploy_res.json()["detail"].lower()
        assert "connected broker" in detail or "expired" in detail

        # 3. Connect a mock broker account in DB owned by the authenticated user
        async with SessionLocal() as session:
            async with session.begin():
                broker_rec = BrokerAccountRecord(
                    user_id=owner_id,
                    broker_name="ZERODHA",
                    status="CONNECTED",
                )
                broker_rec.set_credentials(
                    api_key="mock_kite_key",
                    api_secret="mock_kite_secret",
                    access_token="mock_kite_token",
                )
                session.add(broker_rec)
                await session.flush()
                broker_id = broker_rec.id

        # 4. Now deploy strategy to LIVE mode with the connected broker
        live_deploy_res2 = await client.post(
            f"/api/strategies/{paper_strat['id']}/deploy",
            json={
                "execution_mode": "LIVE",
                "broker_name": "ZERODHA",
                "broker_account_id": broker_id,
                "multiplier": 1.0,
                "capital_allocated": 50000.0,
            },
            headers=headers,
        )
        assert live_deploy_res2.status_code == 200
        assert live_deploy_res2.json()["execution_mode"] == "LIVE"

        # Verify strategy row in DB has LIVE mode and linked broker
        async with SessionLocal() as session:
            strat_db = await session.get(StrategyRecord, paper_strat["id"])
            assert strat_db is not None
            assert strat_db.execution_mode == "LIVE"
            assert strat_db.broker_account_id == broker_id


@pytest.mark.asyncio
async def test_live_order_execution_and_margin_rejection():
    """Test that live signal execution checks broker margin and records genuine broker order IDs."""
    await init_db()
    from app.brokers.zerodha import ZerodhaKiteBroker
    from app.models.user import UserRecord
    import asyncio
    import uuid

    tick_q = asyncio.Queue()
    mock_sim_broker = SimulatedBroker()
    engine = TradingEngine(broker=mock_sim_broker, tick_queue=tick_q)

    # This test intentionally exercises the LIVE broker-dispatch path (mocked
    # Zerodha) so it must opt into BROKER_MODE=live.  The autouse
    # ``reset_broker_mode`` conftest fixture restores the safe default before
    # every other test.
    from app.config import settings as _settings
    _settings.broker_mode = "live"

    # 1. Connect a live Zerodha broker in DB
    async with SessionLocal() as session:
        async with session.begin():
            user_rec = UserRecord(
                id=str(uuid.uuid4()),
                email=f"trader_{uuid.uuid4().hex[:6]}@tradetron.io",
                hashed_password="hashed_pw_test",
                full_name="Live Order Tester",
            )
            session.add(user_rec)
            await session.flush()

            broker_rec = BrokerAccountRecord(
                user_id=user_rec.id,
                broker_name="ZERODHA",
                status="CONNECTED",
            )
            broker_rec.set_credentials("key", "sec", "tok")
            session.add(broker_rec)
            await session.flush()
            b_id = broker_rec.id

    live_strat = {
        "id": "strat-live-test-01",
        "name": "Live SMA Breakout",
        "symbols": ["RELIANCE"],
        "conditions": [],
        "action": {"side": "BUY", "quantity": 100, "order_type": "MARKET"},
        "enabled": True,
        "execution_mode": "LIVE",
        "broker_account_id": b_id,
        "capital_allocated": 100000.0,
    }

    # A. Test Margin Rejection (Broker returns only ₹500 cash, but 100 shares @ ₹2500 = ₹250,000 required)
    with patch.object(ZerodhaKiteBroker, "get_margins", new_callable=AsyncMock) as mock_margins:
        mock_margins.return_value = {"available_cash": 500.0, "collateral": 0.0}

        await engine._execute_signal(live_strat, "RELIANCE", 2500.0)

        # Verify order was rejected in DB
        async with SessionLocal() as session:
            stmt = select(OrderRecord).where(
                OrderRecord.strategy_id == "strat-live-test-01",
                OrderRecord.status == "REJECTED",
            )
            res = await session.execute(stmt)
            rejected_order = res.scalars().first()
            assert rejected_order is not None
            assert rejected_order.mode == "LIVE"
            assert "Insufficient margin" in rejected_order.error_message

    # B. Test Successful Live Order Placement (Broker returns ₹500,000 cash)
    with patch.object(ZerodhaKiteBroker, "get_margins", new_callable=AsyncMock) as mock_margins, \
         patch.object(ZerodhaKiteBroker, "place_order", new_callable=AsyncMock) as mock_place:

        mock_margins.return_value = {"available_cash": 500000.0, "collateral": 0.0}
        mock_place.return_value = {
            "broker_order_id": "240821000998877",
            "filled_price": 2500.0,
            "status": "FILLED",
        }

        await engine._execute_signal(live_strat, "RELIANCE", 2500.0)

        # Verify filled order with real broker order ID in DB
        async with SessionLocal() as session:
            stmt = select(OrderRecord).where(
                OrderRecord.strategy_id == "strat-live-test-01",
                OrderRecord.status == "FILLED",
            )
            res = await session.execute(stmt)
            filled_order = res.scalars().first()
            assert filled_order is not None
            assert filled_order.mode == "LIVE"
            assert filled_order.broker_order_id == "240821000998877"
            assert filled_order.quantity == 100


@pytest.mark.asyncio
async def test_emergency_kill_switch_immediate_block():
    """Test that activating the emergency kill switch immediately stops all live order flow."""
    import time
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Authenticated operator required for kill-switch
        uid = int(time.time() * 1000) % 1000000
        reg_res = await client.post("/api/auth/register", json={
            "email": f"killswitch_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "Kill Switch Operator",
        })
        assert reg_res.status_code == 201
        token = reg_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # The platform-wide kill-switch is admin-only. Promote this user to
        # admin in the DB (role is re-read from the DB on every authenticated
        # request, so the existing token remains valid).
        from sqlalchemy import select as _select
        from app.models.user import UserRecord as _UserRecord

        async with SessionLocal() as session:
            u = (await session.execute(_select(_UserRecord).where(_UserRecord.id == reg_res.json()["user"]["id"]))).scalar_one()
            u.role = "admin"
            await session.commit()

        # Trigger kill switch
        kill_res = await client.post(
            "/api/strategies/kill-switch",
            json={
                "action": "PAUSE_ALL",
                "reason": "Flash Crash Detected — Emergency Freeze",
            },
            headers=headers,
        )
        assert kill_res.status_code == 200
        assert kill_res.json()["status"] == "HALTED"

        # Verify all strategies are paused
        strat_res = await client.get("/api/strategies")
        assert strat_res.status_code == 200
        strats = strat_res.json()
        for s in strats:
            assert s["enabled"] is False

        # Reset kill switch for clean teardown
        engine = get_engine()
        if engine and hasattr(engine, "risk_manager"):
            engine.risk_manager.reset_kill_switch()


@pytest.mark.asyncio
async def test_manual_order_and_open_positions_lifecycle():
    """Test manual order entry (Paper Mode), real price fill, open position listing, and exit position."""
    import time
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Register a test user for auth
        uid = int(time.time() * 1000) % 1000000
        reg_res = await client.post("/api/auth/register", json={
            "email": f"trader_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "Rishabh Trader",
        })
        assert reg_res.status_code == 201
        token = reg_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Place a manual BUY order in PAPER mode
        order_res = await client.post(
            "/api/trades/order",
            json={
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 25,
                "order_type": "MARKET",
                "mode": "PAPER",
            },
            headers=headers,
        )
        assert order_res.status_code == 200
        order_data = order_res.json()
        assert order_data["success"] is True
        assert order_data["symbol"] == "RELIANCE"
        assert order_data["side"] == "BUY"
        assert order_data["quantity"] == 25
        assert order_data["status"] == "FILLED"
        assert order_data["price"] > 0
        pos_id = order_data.get("position_id")
        assert pos_id is not None

        # 2. Query open positions list
        pos_res = await client.get("/api/trades/positions", headers=headers)
        assert pos_res.status_code == 200
        positions = pos_res.json()
        assert len(positions) >= 1
        pos = next(p for p in positions if p["id"] == pos_id)
        assert pos["symbol"] == "RELIANCE"
        assert pos["side"] == "LONG"
        assert pos["quantity"] == 25
        assert pos["entry_price"] > 0
        assert pos["status"] == "OPEN"

        # 3. Close the open position
        close_res = await client.post(f"/api/trades/positions/{pos_id}/close", headers=headers)
        assert close_res.status_code == 200
        close_data = close_res.json()
        assert close_data["success"] is True
        assert close_data["status"] == "CLOSED"
        assert close_data["position_id"] == pos_id

        # 4. Verify position is no longer in open positions list
        pos_res_after = await client.get("/api/trades/positions", headers=headers)
        assert pos_res_after.status_code == 200
        positions_after = pos_res_after.json()
        assert not any(p["id"] == pos_id for p in positions_after)
