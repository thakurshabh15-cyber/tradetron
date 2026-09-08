"""Test Dashboard Summary and Task Completion."""

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_dashboard_summary_and_complete_task():
    """Verify GET /api/dashboard/summary and POST /api/dashboard/complete-task.

    Task completion is an authenticated, per-user mutation: anonymous callers
    must be rejected (401) and the completed state must be scoped to the
    caller's own identity.
    """
    # 1. Anonymous summary is still a public global view
    res = client.get("/api/dashboard/summary")
    assert res.status_code == 200, res.text
    data = res.json()

    assert "weekReturn" in data
    assert "monthReturn" in data
    assert "topStrategies" in data
    assert "pendingTasks" in data
    assert "engineStatus" in data
    assert isinstance(data["topStrategies"], list)
    assert isinstance(data["pendingTasks"], list)

    # 1b. Anonymous task completion must be rejected (security)
    anon_res = client.post(
        "/api/dashboard/complete-task",
        json={"task_id": "subscription_setup", "completed": True},
    )
    assert anon_res.status_code == 401, anon_res.text

    # 2. Register & authenticate a user (unique email so repeat runs don't clash)
    import time

    unique = f"dash-task-{int(time.time())}"
    reg = client.post(
        "/api/auth/register",
        json={
            "email": f"{unique}@test.com",
            "password": "SecurePass1!",
            "full_name": "Dashboard Tester",
        },
    )
    assert reg.status_code in (200, 201), reg.text
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 3. Complete a task as the authenticated user
    task_res = client.post(
        "/api/dashboard/complete-task",
        headers=headers,
        json={"task_id": "subscription_setup", "completed": True},
    )
    assert task_res.status_code == 200, task_res.text
    task_data = task_res.json()
    assert task_data["success"] is True
    assert task_data["task_id"] == "subscription_setup"
    assert task_data["is_completed"] is True
