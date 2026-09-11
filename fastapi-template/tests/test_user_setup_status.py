"""Test /api/user/setup-status — AUTH REQUIRED + TENANT-SCOPED.

Final-hardening regression suite for the setup-checklist endpoints.

What these tests prove:
  1. Anonymous callers are rejected (401) on BOTH GET and PATCH. The previous
     implementation accepted unauthenticated mutations of a process-global
     dict that was shared by every tenant and hardcoded Marketplace/Broker
     as "Complete".
  2. An authenticated caller ALWAYS gets a 200 shape with all three tasks.
  3. PATCH persists ONLY the caller's own override; another tenant's state is
     completely unaffected (tenant isolation).
  4. Fresh users are not shown a fabricated "Complete": with no broker
     account, no strategy and no active subscription, all tasks derive
     "Pending" (progress 0%) until the user actually does the work or
     explicitly toggles their own checklist.
"""

import asyncio
import time

from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import app

# Ensure all ORM tables are created (including user_setup_tasks).  Running this
# file standalone otherwise fails with
# ``sqlite3.OperationalError: no such table: user_setup_tasks``.
asyncio.run(init_db())

client = TestClient(app)


def _register(prefix):
    unique = f"{prefix}-{int(time.time() * 1000)}"
    reg = client.post(
        "/api/auth/register",
        json={
            "email": f"{unique}@test.com",
            "password": "SecurePass1!",
            "full_name": "Setup Status Tester",
        },
    )
    assert reg.status_code in (200, 201, 202), reg.text
    body = reg.json()
    token = body.get("access_token")
    assert token, f"register did not return access_token: {body}"
    return {"Authorization": f"Bearer {token}"}


def test_anonymous_get_rejected():
    """The endpoint is auth-gated — anonymous reads must 401."""
    res = client.get("/api/user/setup-status")
    assert res.status_code == 401, res.text


def test_anonymous_patch_rejected():
    """Anonymous callers must NOT be able to mutate any setup state."""
    res = client.patch(
        "/api/user/setup-status",
        json={"task_id": "subscription_setup", "status": "Complete"},
    )
    assert res.status_code == 401, res.text


def test_authenticated_get_returns_expected_shape():
    """An authenticated caller gets the full response shape."""
    headers = _register("setup-get")
    res = client.get("/api/user/setup-status", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()

    assert "marketplace_setup" in data
    assert "broker_setup" in data
    assert "subscription_setup" in data
    assert "tasks" in data
    assert "overall_progress_pct" in data
    assert data["marketplace_setup"]["title"] == "Marketplace Setup"
    assert data["broker_setup"]["title"] == "Broker Setup"
    assert data["subscription_setup"]["title"] == "Subscription Setup"


def test_fresh_user_not_fabricated_complete():
    """A brand-new user with no broker/strategy/subscription sees honest
    Pending state (0% progress), never a fabricated 'Complete'."""
    headers = _register("setup-fresh")
    res = client.get("/api/user/setup-status", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["marketplace_setup"]["status"] == "Pending"
    assert data["broker_setup"]["status"] == "Pending"
    assert data["subscription_setup"]["status"] == "Pending"
    assert data["overall_progress_pct"] == 0
    assert data["completed_count"] == 0


def test_patch_persists_own_override():
    """An authenticated user can toggle their OWN task override."""
    headers = _register("setup-patch")
    res = client.patch(
        "/api/user/setup-status",
        headers=headers,
        json={"task_id": "subscription_setup", "status": "Complete"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["subscription_setup"]["status"] == "Complete"
    assert data["subscription_setup"]["completed_at"] is not None
    assert data["completed_count"] == 1

    # Re-fetch — the override must survive (persisted, not ephemeral).
    res2 = client.get("/api/user/setup-status", headers=headers)
    assert res2.status_code == 200, res2.text
    assert res2.json()["subscription_setup"]["status"] == "Complete"


def test_tenant_isolation():
    """User A toggling their checklist must not leak into user B's state."""
    headers_a = _register("setup-iso-a")
    headers_b = _register("setup-iso-b")

    # A marks marketplace setup Complete.
    res_a = client.patch(
        "/api/user/setup-status",
        headers=headers_a,
        json={"task_id": "marketplace_setup", "status": "Complete"},
    )
    assert res_a.status_code == 200, res_a.text
    assert res_a.json()["marketplace_setup"]["status"] == "Complete"

    # B's state must be completely untouched.
    res_b = client.get("/api/user/setup-status", headers=headers_b)
    assert res_b.status_code == 200, res_b.text
    b_data = res_b.json()
    assert b_data["marketplace_setup"]["status"] == "Pending"
    assert b_data["completed_count"] == 0


def test_patch_pending_persists():
    """Toggling back to Pending clears the completed_at override."""
    headers = _register("setup-pending")
    client.patch(
        "/api/user/setup-status",
        headers=headers,
        json={"task_id": "broker_setup", "status": "Complete"},
    )
    res = client.patch(
        "/api/user/setup-status",
        headers=headers,
        json={"task_id": "broker_setup", "status": "Pending"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["broker_setup"]["status"] == "Pending"
    assert data["broker_setup"]["completed_at"] is None
