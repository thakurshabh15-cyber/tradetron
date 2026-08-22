"""Unit tests for User Profile (with base64 photo) and Notification Preferences API."""

import asyncio
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import init_db

# Ensure all ORM tables are created
asyncio.run(init_db())

client = TestClient(app)


def test_user_profile_crud():
    """Verify GET and PUT for User Profile including base64 photo."""
    # 1. GET Profile
    res = client.get("/api/user/profile")
    assert res.status_code == 200
    profile = res.json()
    assert "email" in profile
    assert "full_name" in profile

    # 2. PUT Profile with base64 image
    sample_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    put_res = client.put(
        "/api/user/profile",
        json={
            "full_name": "Alexander Hamilton",
            "profile_photo": sample_b64,
        },
    )
    assert put_res.status_code == 200
    updated = put_res.json()
    assert updated["full_name"] == "Alexander Hamilton"
    assert updated["profile_photo"] == sample_b64

    # 3. GET Profile after update
    get_res = client.get("/api/user/profile")
    assert get_res.status_code == 200
    assert get_res.json()["full_name"] == "Alexander Hamilton"
    assert get_res.json()["profile_photo"] == sample_b64


def test_notification_preferences_crud():
    """Verify GET and PUT for Notification Preferences (Telegram, email, push)."""
    # 1. GET Preferences
    res = client.get("/api/user/notifications")
    assert res.status_code == 200
    prefs = res.json()
    assert "email_enabled" in prefs
    assert "telegram_enabled" in prefs
    assert "push_enabled" in prefs

    # 2. PUT Preferences
    update_res = client.put(
        "/api/user/notifications",
        json={
            "email_enabled": True,
            "email_address": "trader.vip@tradetron.ai",
            "telegram_enabled": True,
            "telegram_chat_id": "@tradetron_vip_alerts",
            "push_enabled": True,
            "order_executed_notify": True,
            "trade_closed_notify": True,
            "sl_tp_trigger_notify": True,
            "price_alert_notify": True,
        },
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["email_address"] == "trader.vip@tradetron.ai"
    assert updated["telegram_enabled"] is True
    assert updated["telegram_chat_id"] == "@tradetron_vip_alerts"
