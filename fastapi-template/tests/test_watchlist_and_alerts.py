"""Unit tests for Watchlist CRUD and Price Alerts API."""

import asyncio
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import init_db

# Ensure all ORM tables are created
asyncio.run(init_db())

client = TestClient(app)


import time

def test_watchlist_crud():
    """Verify GET, POST, and DELETE on /api/watchlist."""
    # 1. GET Watchlist (seeded defaults)
    res = client.get("/api/watchlist")
    assert res.status_code == 200
    items = res.json()
    assert len(items) >= 1
    symbols = [it["symbol"] for it in items]
    assert "AAPL" in symbols

    # 2. POST Add symbol
    test_sym = f"TST_{int(time.time() * 1000) % 100000}"
    add_res = client.post(
        "/api/watchlist",
        json={"symbol": test_sym, "notes": "Dynamic Test Stock"},
    )
    assert add_res.status_code == 201
    added = add_res.json()
    assert added["symbol"] == test_sym
    assert added["notes"] == "Dynamic Test Stock"

    # 3. Duplicate prevention
    dup_res = client.post("/api/watchlist", json={"symbol": test_sym})
    assert dup_res.status_code == 400

    # 4. DELETE Remove symbol
    del_res = client.delete(f"/api/watchlist/{added['id']}")
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] == test_sym


def test_price_alerts_crud():
    """Verify Alert creation, listing, toggling, and deletion."""
    # 1. Create Alert
    create_res = client.post(
        "/api/watchlist/alerts",
        json={
            "symbol": "NVDA",
            "condition": "ABOVE",
            "target_price": 135.50,
        },
    )
    assert create_res.status_code == 201
    alert = create_res.json()
    assert alert["symbol"] == "NVDA"
    assert alert["condition"] == "ABOVE"
    assert alert["target_price"] == 135.50
    assert alert["is_active"] is True

    # 2. List Alerts
    list_res = client.get("/api/watchlist/alerts/list")
    assert list_res.status_code == 200
    all_alerts = list_res.json()
    assert any(a["id"] == alert["id"] for a in all_alerts)

    # 3. Toggle Alert
    toggle_res = client.patch(f"/api/watchlist/alerts/{alert['id']}/toggle")
    assert toggle_res.status_code == 200
    assert toggle_res.json()["is_active"] is False

    # 4. Delete Alert
    del_res = client.delete(f"/api/watchlist/alerts/{alert['id']}")
    assert del_res.status_code == 200
    assert del_res.json()["deleted_id"] == alert["id"]
