"""Test Marketplace Listing, Filtering, Symbol Search, Deploy, Pause, and Publish."""

import asyncio
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import init_db

client = TestClient(app)


def test_marketplace_suite():
    """Verify marketplace pagination, filtering, symbol search, deployment, and publishing."""
    asyncio.run(init_db())

    # 1. Test Marketplace Listing & Pagination
    res = client.get("/api/strategies/marketplace?page=1&limit=3")
    assert res.status_code == 200, res.text
    data = res.json()
    assert "items" in data
    assert "total" in data
    assert "totalPages" in data
    assert len(data["items"]) <= 3
    assert data["page"] == 1

    # 2. Test Category Filtering
    cat_res = client.get("/api/strategies/marketplace?category=Momentum")
    assert cat_res.status_code == 200
    cat_data = cat_res.json()
    for item in cat_data["items"]:
        assert item["category"] == "Momentum"

    # 3. Test Symbol Search
    sym_res = client.get("/api/strategies/marketplace?symbol=NVDA")
    assert sym_res.status_code == 200
    sym_data = sym_res.json()
    for item in sym_data["items"]:
        assert "NVDA" in [s.upper() for s in item.get("symbols", [])]

    # 4. Create a strategy to test deploy and publish
    strat_res = client.post(
        "/api/strategies",
        json={
            "name": "Marketplace Source Test Algo",
            "symbols": ["AAPL", "MSFT"],
            "conditions": [{"indicator": "PRICE", "operator": "gt", "value": 200.0, "period": 14}],
            "action": {"side": "BUY", "quantity": 10, "order_type": "MARKET"},
            "enabled": True,
        },
    )
    assert strat_res.status_code == 201
    created_strat = strat_res.json()
    strat_id = created_strat["id"]

    # 5. Test Deploy Strategy
    deploy_res = client.post(
        f"/api/strategies/{strat_id}/deploy",
        json={
            "execution_mode": "PAPER",
            "broker_name": "Simulated",
            "multiplier": 2.5,
            "capital_allocated": 15000.0,
        },
    )
    assert deploy_res.status_code == 200, deploy_res.text
    deploy_data = deploy_res.json()
    assert deploy_data["success"] is True
    assert deploy_data["multiplier"] == 2.5
    assert deploy_data["status"] == "RUNNING"

    # 6. Test Pause Strategy
    pause_res = client.post(f"/api/strategies/{strat_id}/pause")
    assert pause_res.status_code == 200, pause_res.text
    pause_data = pause_res.json()
    assert pause_data["success"] is True
    assert pause_data["status"] == "PAUSED"

    # 7. Test Publish to Marketplace
    publish_res = client.post(
        "/api/strategies/marketplace/publish",
        json={
            "strategy_id": strat_id,
            "creator_name": "Pro Trader X",
            "category": "Momentum",
            "pricing_type": "FREE",
            "price": 0.0,
            "description": "Verified momentum algo tested on Apple and Microsoft.",
        },
    )
    assert publish_res.status_code == 200, publish_res.text
    pub_data = publish_res.json()
    assert pub_data["success"] is True
    assert "marketplace_id" in pub_data
