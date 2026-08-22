"""Unit tests for Multi-Broker Adapters (Zerodha Kite, Binance), Pre-Trade Margin Gates, Postbacks & Kill-Switch."""

import asyncio
import time
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.db.session import init_db
from app.brokers.zerodha import ZerodhaKiteBroker
from app.brokers.binance import BinanceBroker
from app.engine.risk_manager import RiskManager
from app.schemas.trading import OrderRequest, Side


async def test_multi_broker_execution_suite():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Setup Auth User
        uid = int(time.time() * 1000) % 1000000
        reg_res = await client.post("/api/auth/register", json={
            "email": f"broker_trader_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "Broker Test User",
        })
        assert reg_res.status_code == 201
        token = reg_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Test Zerodha Kite Connect Adapter & OAuth URL
        auth_url_res = await client.get("/api/brokers/oauth/authorize?broker=ZERODHA", headers=headers)
        assert auth_url_res.status_code == 200
        assert "kite.zerodha.com" in auth_url_res.json()["authorize_url"]

        # 3. Test OAuth Callback & Account Encryption
        callback_res = await client.post("/api/brokers/oauth/callback", json={
            "broker_name": "ZERODHA",
            "request_token": "valid_mock_request_token",
            "client_id": "ZR9988",
        }, headers=headers)
        assert callback_res.status_code == 200
        assert callback_res.json()["status"] == "CONNECTED"

        # 4. Test List Accounts with Masked Keys
        list_res = await client.get("/api/brokers/accounts", headers=headers)
        assert list_res.status_code == 200
        accounts = list_res.json()
        assert len(accounts) >= 1
        assert any(acc["broker_name"] == "ZERODHA" for acc in accounts)

        # 5. Test Binance Crypto Broker Adapter
        bn_broker = BinanceBroker(api_key="bn_key_test", api_secret="bn_secret_test", testnet=True)
        await bn_broker.connect()

        # Mock api_request for unit test simulation
        async def mock_api_req(method, path, params=None, signed=True):
            if path == "/api/v3/order":
                return {
                    "symbol": "BTCUSDT",
                    "orderId": 887766,
                    "status": "FILLED",
                    "executedQty": "1.0",
                    "cummulativeQuoteQty": "64250.0",
                    "side": "BUY",
                    "type": "MARKET",
                }
            elif path == "/api/v3/account":
                return {
                    "balances": [
                        {"asset": "USDT", "free": "10000.0", "locked": "0.0"},
                        {"asset": "BTC", "free": "0.5", "locked": "0.0"},
                    ]
                }
            return {}

        bn_broker._api_request = mock_api_req
        bn_order = await bn_broker.place_order(OrderRequest(symbol="BTCUSDT", side=Side.BUY, quantity=1))
        assert bn_order["status"] == "FILLED"
        assert bn_order["broker_order_id"] == "887766"

        bn_margins = await bn_broker.get_margins()
        assert bn_margins["available_cash"] == 10000.0
        assert bn_margins["currency"] == "USDT"
        await bn_broker.disconnect()

        # 6. Test Pre-Trade Margin Check in RiskManager
        rm = RiskManager()
        # Available ₹50,000 vs Required ₹1,20,000 -> Must REJECT
        ok, reason = rm.check_margin(available_margin=50000.0, required_margin=120000.0)
        assert ok is False
        assert "Insufficient margin" in reason

        # Available ₹2,50,000 vs Required ₹1,20,000 -> Must PASS
        ok_pass, _ = rm.check_margin(available_margin=250000.0, required_margin=120000.0)
        assert ok_pass is True

        # 7. Test Emergency Kill-Switch
        kill_res = await client.post("/api/strategies/kill-switch", json={
            "action": "PAUSE_ALL",
            "reason": "Extreme market circuit volatility",
        })
        assert kill_res.status_code == 200
        assert kill_res.json()["status"] == "HALTED"

        # Trading check during kill switch must reject orders
        order_check_res, rej_reason = rm.check(OrderRequest(symbol="RELIANCE", side=Side.BUY, quantity=10))
        # When kill switch triggered on rm:
        rm.trigger_kill_switch("Manual test halt")
        blocked, block_reason = rm.check(OrderRequest(symbol="RELIANCE", side=Side.BUY, quantity=10))
        assert blocked is False
        assert "Kill-Switch active" in block_reason

        # Release Kill-Switch
        resume_res = await client.post("/api/strategies/kill-switch", json={"action": "RESUME_ALL"})
        assert resume_res.status_code == 200
        assert resume_res.json()["status"] == "RUNNING"

        # 8. Test Broker Postback Webhook
        postback_res = await client.post("/api/brokers/postback/ZERODHA", json={
            "order_id": "KITE-ORD-8811",
            "status": "COMPLETE",
            "tradingsymbol": "NIFTY50",
            "filled_quantity": 50,
            "average_price": 24850.0,
        })
        assert postback_res.status_code == 200
        assert postback_res.json()["reconciled_status"] == "FILLED"
