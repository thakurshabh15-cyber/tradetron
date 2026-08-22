"""Unit tests for Strictly-Secured Admin Governance, User/KYC Management, Broker Monitor, Revenue & Audit Trails."""

import asyncio
import time
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.db.session import init_db


async def test_admin_governance_suite():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Register Regular Trader User
        uid = int(time.time() * 1000) % 1000000
        trader_res = await client.post("/api/auth/register", json={
            "email": f"trader_reg_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "Regular Trader User",
        })
        assert trader_res.status_code == 201
        trader_token = trader_res.json()["access_token"]
        trader_headers = {"Authorization": f"Bearer {trader_token}"}

        # 2. Register Admin User
        admin_res = await client.post("/api/auth/register", json={
            "email": f"admin_master_{uid}@tradetron.io",
            "password": "AdminSuperSecret123!",
            "full_name": "Master Administrator",
        })
        assert admin_res.status_code == 201
        admin_token_temp = admin_res.json()["access_token"]
        admin_user_id = admin_res.json()["user"]["id"]

        # Promote admin_user_id to 'admin' in database for testing
        from app.db.session import SessionLocal
        from sqlalchemy import select
        from app.models.user import UserRecord

        async with SessionLocal() as db:
            user_stmt = select(UserRecord).where(UserRecord.id == admin_user_id)
            res = await db.execute(user_stmt)
            u = res.scalar_one()
            u.role = "admin"
            await db.commit()

        # 3. Regular Trader attempting to access Admin Overview -> Must get 403 Forbidden
        denied_res = await client.get("/api/admin/overview", headers=trader_headers)
        assert denied_res.status_code == 403
        assert "Administrative privileges required" in denied_res.json()["detail"]

        # 4. Admin Login Route
        admin_login_res = await client.post("/api/admin/login", json={
            "email": f"admin_master_{uid}@tradetron.io",
            "password": "AdminSuperSecret123!",
            "admin_security_pin": "9988",
        })
        assert admin_login_res.status_code == 200
        admin_token = admin_login_res.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        # 5. Admin Overview Command Center
        overview_res = await client.get("/api/admin/overview", headers=admin_headers)
        assert overview_res.status_code == 200
        overview_data = overview_res.json()
        assert "users" in overview_data
        assert "brokers" in overview_data
        assert "strategies" in overview_data
        assert "revenue" in overview_data

        # 6. User Management: List & Filter
        users_res = await client.get("/api/admin/users?limit=10", headers=admin_headers)
        assert users_res.status_code == 200
        users = users_res.json()
        assert len(users) >= 2

        # 7. Suspend & Reactivate Trader User
        target_user_id = trader_res.json()["user"]["id"]
        status_res = await client.post(f"/api/admin/users/{target_user_id}/status", json={
            "is_active": False,
            "reason": "Suspicious login location detected",
        }, headers=admin_headers)
        assert status_res.status_code == 200
        assert status_res.json()["is_active"] is False

        # Reactivate
        reactivate_res = await client.post(f"/api/admin/users/{target_user_id}/status", json={
            "is_active": True,
            "reason": "Identity confirmed by operator",
        }, headers=admin_headers)
        assert reactivate_res.status_code == 200
        assert reactivate_res.json()["is_active"] is True

        # 8. KYC Review Queue & Approval
        kyc_queue_res = await client.get("/api/admin/kyc/queue", headers=admin_headers)
        assert kyc_queue_res.status_code == 200

        review_res = await client.post(f"/api/admin/kyc/{target_user_id}/review", json={
            "decision": "VERIFIED",
            "remarks": "PAN & Aadhaar verified per SEBI guidelines",
        }, headers=admin_headers)
        assert review_res.status_code == 200
        assert review_res.json()["kyc_status"] == "VERIFIED"

        # 9. Broker Connection Monitor
        brokers_res = await client.get("/api/admin/brokers/monitor", headers=admin_headers)
        assert brokers_res.status_code == 200

        # 10. Strategy Risk Oversight
        strat_res = await client.get("/api/admin/strategies/oversight", headers=admin_headers)
        assert strat_res.status_code == 200

        # 11. Revenue & Subscription Dashboard
        rev_res = await client.get("/api/admin/revenue/metrics", headers=admin_headers)
        assert rev_res.status_code == 200
        assert "mrr" in rev_res.json()

        # 12. Filterable Audit Trail Viewer
        audit_res = await client.get("/api/admin/audit-logs?limit=25", headers=admin_headers)
        assert audit_res.status_code == 200
        logs = audit_res.json()
        assert len(logs) >= 1

        # 13. System Telemetry & Provider Latency
        health_res = await client.get("/api/admin/system/health", headers=admin_headers)
        assert health_res.status_code == 200
        assert health_res.json()["status"] == "HEALTHY"

        # 14. Admin User Kill-Switch
        user_kill_res = await client.post(f"/api/admin/kill-switch/user/{target_user_id}", json={
            "reason": "Max leverage breach",
        }, headers=admin_headers)
        assert user_kill_res.status_code == 200
        assert user_kill_res.json()["status"] == "HALTED"

        # 15. Platform Kill-Switch
        platform_kill_res = await client.post("/api/admin/kill-switch/platform", json={
            "reason": "Market-wide circuit trip",
        }, headers=admin_headers)
        assert platform_kill_res.status_code == 200
        assert platform_kill_res.json()["status"] == "HALTED"
