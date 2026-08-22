"""Test /api/user/setup-status endpoint."""

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_get_setup_status():
    """Test retrieving user setup status."""
    res = client.get("/api/user/setup-status")
    assert res.status_code == 200
    data = res.json()

    assert "marketplace_setup" in data
    assert "broker_setup" in data
    assert "subscription_setup" in data
    assert "tasks" in data
    assert "overall_progress_pct" in data

    assert data["marketplace_setup"]["title"] == "Marketplace Setup"
    assert data["broker_setup"]["title"] == "Broker Setup"
    assert data["subscription_setup"]["title"] == "Subscription Setup"


def test_patch_setup_status_toggle():
    """Test toggling setup tasks between Complete and Pending."""
    # 1. Set Subscription Setup to Complete
    res = client.patch(
        "/api/user/setup-status",
        json={"task_id": "subscription_setup", "status": "Complete"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["subscription_setup"]["status"] == "Complete"
    assert data["overall_progress_pct"] == 100
    assert data["completed_count"] == 3

    # 2. Set Subscription Setup back to Pending
    res = client.patch(
        "/api/user/setup-status",
        json={"task_id": "subscription_setup", "status": "Pending"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["subscription_setup"]["status"] == "Pending"
    assert data["overall_progress_pct"] == 66
    assert data["completed_count"] == 2
