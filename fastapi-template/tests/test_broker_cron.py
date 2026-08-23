"""Test Suite for Daily Morning Broker TOTP Renewal Cron (8:45 AM IST) & Health Status API."""

import pytest
import pyotp
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import SessionLocal, init_db
from app.core.security import create_access_token, hash_password
from app.models.user import UserRecord
from app.models.broker_account import BrokerAccountRecord, BrokerSessionLogRecord
from app.engine.broker_cron import broker_renewal_engine
import uuid


@pytest.fixture(autouse=True)
async def setup_database():
    """Ensure DB schema is initialized before test execution."""
    await init_db()


@pytest.mark.asyncio
async def test_broker_renewal_engine_and_endpoints():
    client = TestClient(app)

    # 1. Setup Test User and Broker Accounts (Angel One with TOTP, Zerodha, Simulated)
    user_id = str(uuid.uuid4())
    user_email = f"cron_trader_{user_id[:8]}@tradetron.io"

    totp_secret = pyotp.random_base32()
    angel_acc_id = str(uuid.uuid4())
    zerodha_acc_id = str(uuid.uuid4())

    async with SessionLocal() as db:
        user = UserRecord(
            id=user_id,
            email=user_email,
            hashed_password=hash_password("Pass123!"),
            full_name="Cron Trader",
            role="trader",
            is_active=True,
            is_verified=True,
        )
        db.add(user)

        angel_acc = BrokerAccountRecord(
            id=angel_acc_id,
            user_id=user_id,
            broker_name="ANGEL_ONE",
            account_name="Angel SmartAPI Account",
            client_id="ANGEL123",
            api_key_encrypted="",
            status="CONNECTED",
            is_active=True,
        )
        angel_acc.set_credentials(
            api_key="angel_api_key_12345",
            api_secret="angel_password_pass",
            access_token="initial_expired_token",
            totp_secret=totp_secret,
        )
        db.add(angel_acc)

        zerodha_acc = BrokerAccountRecord(
            id=zerodha_acc_id,
            user_id=user_id,
            broker_name="ZERODHA",
            account_name="Zerodha Kite Connect",
            client_id="ZR9988",
            api_key_encrypted="",
            status="CONNECTED",
            is_active=True,
        )
        zerodha_acc.set_credentials(
            api_key="zerodha_api_key",
            api_secret="zerodha_secret",
            access_token="initial_kite_token",
        )
        db.add(zerodha_acc)

        await db.commit()

    token = create_access_token({"sub": user_id, "email": user_email, "role": "trader"})

    # 2. Test direct engine renewal
    engine_res = await broker_renewal_engine.renew_all_broker_sessions(user_id=user_id)
    assert engine_res["total_accounts"] >= 2
    assert engine_res["successful_renewals"] >= 2
    assert engine_res["latency_ms"] >= 0.0

    # 3. Verify session logs were saved in DB
    async with SessionLocal() as db:
        logs = (
            await db.execute(
                pytest.importorskip("sqlalchemy")
                .select(BrokerSessionLogRecord)
                .where(BrokerSessionLogRecord.user_id == user_id)
            )
        ).scalars().all()
        assert len(logs) >= 2
        assert any(l.broker_name == "ANGEL_ONE" and l.status == "SUCCESS" for l in logs)

    # 4. Test Manual Renew All Endpoint
    renew_res = client.post(
        "/api/brokers/renew-all",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert renew_res.status_code == 200
    renew_data = renew_res.json()
    assert renew_data["success"] is True
    assert renew_data["successful_renewals"] >= 2

    # 5. Test Broker Sessions Health Status Endpoint
    health_res = client.get(
        "/api/brokers/health-status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert health_res.status_code == 200
    health_data = health_res.json()
    assert health_data["total_connected"] >= 2
    assert health_data["active_healthy"] >= 2
    assert health_data["next_scheduled_renewal_ist"] == "08:45 AM IST"

    accounts_list = health_data["accounts"]
    angel_entry = next((a for a in accounts_list if a["account_id"] == angel_acc_id), None)
    assert angel_entry is not None
    assert angel_entry["health_status"] == "ACTIVE"
    assert angel_entry["has_totp_configured"] is True
    assert angel_entry["latest_renewal"]["status"] == "SUCCESS"

    # 6. Test Renewal Logs Endpoint
    logs_res = client.get(
        "/api/brokers/renewal-logs",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert logs_res.status_code == 200
    logs_list = logs_res.json()
    assert len(logs_list) >= 2
