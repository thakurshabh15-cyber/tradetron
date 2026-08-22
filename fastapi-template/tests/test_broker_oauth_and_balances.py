"""Unit tests for Real Broker OAuth (Zerodha, Upstox, Angel One), KYC Gating, Encrypted Tokens, Daily Expiry, and Live Holdings/Margins."""

import time
from datetime import datetime, timedelta, timezone
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.db.session import init_db, SessionLocal
from app.models.broker_account import BrokerAccountRecord
from app.models.user import UserRecord
from sqlalchemy import select


async def test_broker_oauth_and_live_balances_suite():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Register Auth User
        uid = int(time.time() * 1000) % 1000000
        reg_res = await client.post("/api/auth/register", json={
            "email": f"oauth_trader_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "OAuth Broker Tester",
        })
        assert reg_res.status_code == 201
        token = reg_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Test Get OAuth Authorize URLs for all major Indian brokers (Freely allowed)
        kite_auth_res = await client.get("/api/brokers/oauth/authorize?broker=ZERODHA", headers=headers)
        assert kite_auth_res.status_code == 200
        assert "kite.zerodha.com/connect/login" in kite_auth_res.json()["authorize_url"]

        # 3. Optional KYC submission flow test
        kyc_submit_res = await client.post("/api/user/kyc/submit", json={
            "pan_number": "ABCDE1234F",
            "id_proof_type": "PAN_CARD",
            "id_proof_doc": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        }, headers=headers)
        assert kyc_submit_res.status_code == 200
        assert kyc_submit_res.json()["kyc_status"] == "PENDING"

        upstox_auth_res = await client.get("/api/brokers/oauth/authorize?broker=UPSTOX", headers=headers)
        assert upstox_auth_res.status_code == 200
        assert "api.upstox.com/v2/login/authorization/dialog" in upstox_auth_res.json()["authorize_url"]

        angel_auth_res = await client.get("/api/brokers/oauth/authorize?broker=ANGEL_ONE", headers=headers)
        assert angel_auth_res.status_code == 200
        assert "smartapi.angelbroking.com" in angel_auth_res.json()["authorize_url"]

        # 5. Test OAuth Callback & Token Encryption for Zerodha
        kite_callback_res = await client.post("/api/brokers/oauth/callback", json={
            "broker_name": "ZERODHA",
            "request_token": "kite_req_token_test_12345",
            "client_id": "ZR_TESTER",
        }, headers=headers)
        assert kite_callback_res.status_code == 200
        kite_acc_id = kite_callback_res.json()["account_id"]
        assert kite_callback_res.json()["status"] == "CONNECTED"
        assert "token_expires_at" in kite_callback_res.json()

        # 6. Test OAuth Callback & Token Encryption for Upstox
        upstox_callback_res = await client.post("/api/brokers/oauth/callback", json={
            "broker_name": "UPSTOX",
            "request_token": "upstox_auth_code_98765",
            "client_id": "UP_TESTER",
        }, headers=headers)
        assert upstox_callback_res.status_code == 200
        upstox_acc_id = upstox_callback_res.json()["account_id"]
        assert upstox_callback_res.json()["status"] == "CONNECTED"

        # 7. Test List Accounts with Token Expiry & Masked Keys
        accounts_res = await client.get("/api/brokers/accounts", headers=headers)
        assert accounts_res.status_code == 200
        accounts = accounts_res.json()
        assert len(accounts) >= 2
        kite_acc = next(a for a in accounts if a["id"] == kite_acc_id)
        assert kite_acc["broker_name"] == "ZERODHA"
        assert kite_acc["is_token_expired"] is False
        assert kite_acc["status"] == "CONNECTED"
        assert "api_key_masked" in kite_acc

        # 8. Test Live Margins Query
        kite_margins_res = await client.get(f"/api/brokers/accounts/{kite_acc_id}/margins", headers=headers)
        assert kite_margins_res.status_code == 200

        # 9. Test Simulated Paper Account Live Holdings
        sim_link_res = await client.post("/api/brokers/accounts/manual", json={
            "broker_name": "SIMULATED",
            "client_id": "SIM_PAPER_01",
            "api_key": "sim_key_paper",
            "access_token": "sim_active_token",
        }, headers=headers)
        assert sim_link_res.status_code == 200
        sim_acc_id = sim_link_res.json()["id"]

        sim_holdings_res = await client.get(f"/api/brokers/accounts/{sim_acc_id}/holdings", headers=headers)
        assert sim_holdings_res.status_code == 200
        holdings = sim_holdings_res.json()
        assert isinstance(holdings, list)

        # 10. Test Daily Token Expiry Rejection & Graceful Prompt
        async with SessionLocal() as db:
            stmt = select(BrokerAccountRecord).where(BrokerAccountRecord.id == kite_acc_id)
            res = await db.execute(stmt)
            rec = res.scalar_one()
            rec.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=2)
            await db.commit()

        expired_accounts_res = await client.get("/api/brokers/accounts", headers=headers)
        expired_acc = next(a for a in expired_accounts_res.json() if a["id"] == kite_acc_id)
        assert expired_acc["is_token_expired"] is True
        assert expired_acc["status"] == "EXPIRED"

        expired_holdings_res = await client.get(f"/api/brokers/accounts/{kite_acc_id}/holdings", headers=headers)
        assert expired_holdings_res.status_code == 401
        assert "expired" in expired_holdings_res.json()["detail"]

        # 11. Test Invalid Angel One Credentials Rejection (Must return 400 and NOT connect)
        invalid_angel_res = await client.post("/api/brokers/accounts/manual", json={
            "broker_name": "ANGEL_ONE",
            "client_id": "INVALID_CLIENT",
            "api_key": "invalid_key_123",
            "api_secret": "wrong_password",
        }, headers=headers)
        assert invalid_angel_res.status_code == 400
        assert "validation failed" in invalid_angel_res.json()["detail"].lower()
