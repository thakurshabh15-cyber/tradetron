"""Unit tests for User Profile (with base64 photo) and Notification Preferences API."""

import asyncio
import time
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.db.session import init_db


async def test_user_profile_crud():
    """Verify GET and PUT for User Profile including base64 photo for the authenticated user."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uid = int(time.time() * 1000) % 1000000
        reg_res = await client.post("/api/auth/register", json={
            "email": f"profile_user_{uid}@gmail.com",
            "password": "SecurePassword123!",
            "full_name": "Rishabh Thakur",
        })
        assert reg_res.status_code == 201
        token = reg_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. GET Profile
        res = await client.get("/api/user/profile", headers=headers)
        assert res.status_code == 200
        profile = res.json()
        assert profile["email"] == f"profile_user_{uid}@gmail.com"
        assert profile["full_name"] == "Rishabh Thakur"

        # 2. PUT Profile with base64 image
        sample_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        put_res = await client.put(
            "/api/user/profile",
            json={
                "full_name": "Rishabh Thakur (Updated)",
                "profile_photo": sample_b64,
            },
            headers=headers,
        )
        assert put_res.status_code == 200
        updated = put_res.json()
        assert updated["full_name"] == "Rishabh Thakur (Updated)"
        assert updated["profile_photo"] == sample_b64

        # 3. GET Profile after update
        get_res = await client.get("/api/user/profile", headers=headers)
        assert get_res.status_code == 200
        assert get_res.json()["full_name"] == "Rishabh Thakur (Updated)"
        assert get_res.json()["profile_photo"] == sample_b64


async def test_notification_preferences_crud():
    """Verify GET and PUT for Notification Preferences (Telegram, email, push)."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uid = int(time.time() * 1000) % 1000000
        reg_res = await client.post("/api/auth/register", json={
            "email": f"notif_user_{uid}@gmail.com",
            "password": "SecurePassword123!",
            "full_name": "Notif User",
        })
        assert reg_res.status_code == 201
        token = reg_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. GET Preferences
        res = await client.get("/api/user/notifications", headers=headers)
        assert res.status_code == 200
        prefs = res.json()
        assert prefs["email_enabled"] is True
        assert prefs["email_address"] == f"notif_user_{uid}@gmail.com"

        # 2. PUT Preferences
        update_res = await client.put(
            "/api/user/notifications",
            json={
                "email_enabled": True,
                "email_address": f"custom_notif_{uid}@gmail.com",
                "telegram_enabled": True,
                "telegram_chat_id": "@tradetron_vip_alerts",
                "push_enabled": True,
                "order_executed_notify": True,
                "trade_closed_notify": True,
                "sl_tp_trigger_notify": True,
                "price_alert_notify": True,
            },
            headers=headers,
        )
        assert update_res.status_code == 200
        updated = update_res.json()
        assert updated["email_address"] == f"custom_notif_{uid}@gmail.com"
        assert updated["telegram_enabled"] is True
        assert updated["telegram_chat_id"] == "@tradetron_vip_alerts"
