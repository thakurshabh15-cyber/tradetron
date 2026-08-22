"""Test Dashboard Summary and Task Completion."""

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_dashboard_summary_and_complete_task():
    """Verify GET /api/dashboard/summary and POST /api/dashboard/complete-task."""
    # 1. Test Summary
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

    # 2. Test Complete Task
    task_res = client.post(
        "/api/dashboard/complete-task",
        json={"task_id": "subscription_setup", "completed": True},
    )
    assert task_res.status_code == 200, task_res.text
    task_data = task_res.json()
    assert task_data["success"] is True
    assert task_data["task_id"] == "subscription_setup"
    assert task_data["is_completed"] is True
