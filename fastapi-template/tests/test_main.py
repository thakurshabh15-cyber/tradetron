import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def test_api_health_route(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "broker_mode" in data
    # 93ae91e7: engine_strategies_loaded must be present for quarantine observability
    assert "engine_strategies_loaded" in data
    # New: condition_health field for condition-contract health visibility
    assert "condition_health" in data
    ch = data["condition_health"]
    assert "total" in ch
    assert "valid" in ch
    assert "quarantined" in ch
    assert ch["total"] == ch["valid"] + ch["quarantined"]


def test_api_engine_status_route(client):
    """The /api/engine/status observability endpoint must exist and return
    a comprehensive runtime snapshot including condition-contract health."""
    response = client.get("/api/engine/status")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "engine_running" in data
    assert "broker_mode" in data
    assert "engine_strategies_loaded" in data
    assert "engine_strategies_quarantined" in data
    assert "condition_health" in data
    assert data["broker_mode"] == "simulated"  # safety invariant


def test_healthz_route(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_metrics_route(client):
    """/metrics must expose the condition-contract rejection counter
    (tradetron_strategy_condition_errors_total) added by 93ae91e7."""
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert "tradetron_http_requests_total" in body
    assert "tradetron_engine_state" in body
    assert "tradetron_broker_mode_live" in body
    assert "tradetron_ws_channels" in body
    # The condition-contract counter from the KeyError('value') fix
    assert "tradetron_strategy_condition_errors_total" in body

