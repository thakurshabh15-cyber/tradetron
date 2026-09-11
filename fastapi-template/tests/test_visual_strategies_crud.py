"""Unit tests for the Visual Strategy persistence API (create/list/patch/delete).

Covers the tenant-scoped CRUD contract for ``/api/visual-strategies``:

* authenticated create + list
* partial PATCH toggles ``is_active`` (deploy state) in place
* PATCH/DELETE are strictly user-scoped — another tenant's strategy is 404
* anonymous callers are rejected with 401
"""

import asyncio
import uuid

from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.user import UserRecord

asyncio.run(init_db())

client = TestClient(app)


def _fresh_user_headers(tag: str = "vs") -> dict:
    """Create a unique authenticated user and return bearer-token headers."""
    uid = str(uuid.uuid4())

    async def _seed():
        async with SessionLocal() as session:
            session.add(
                UserRecord(
                    id=uid,
                    email=f"{tag}-{uid[:8]}@tradetron.io",
                    hashed_password="x",
                    full_name="Visual Strategy Tester",
                    role="trader",
                    is_active=True,
                    is_verified=True,
                )
            )
            await session.commit()

    asyncio.run(_seed())
    token = create_access_token({"sub": uid, "email": f"{tag}-{uid[:8]}@tradetron.io", "role": "trader"})
    return {"Authorization": f"Bearer {token}"}


def _sample_visual_strategy() -> dict:
    return {
        "name": "Short Straddle Test",
        "underlying": "nifty50",
        "entry_conditions": [{"indicator": "RSI", "operator": "lt", "value": 45, "period": 14}],
        "exit_conditions": {"target_profit": 4000, "max_loss": 2000},
        "legs": [
            {"type": "CE", "strike": "ATM", "action": "SELL", "lots": 1},
            {"type": "PE", "strike": "ATM", "action": "SELL", "lots": 1},
        ],
        "is_active": False,
        "mode": "PAPER",
    }


def test_anonymous_visual_strategies_rejected():
    """Create/list/patch/delete all require a bearer token (private API)."""
    assert client.get("/api/visual-strategies").status_code == 401
    assert client.post("/api/visual-strategies", json=_sample_visual_strategy()).status_code == 401
    assert client.patch("/api/visual-strategies/some-id", json={"is_active": True}).status_code == 401
    assert client.delete("/api/visual-strategies/some-id").status_code == 401


def test_visual_strategy_create_list_patch_delete():
    """Full lifecycle: create, list, partially PATCH is_active, then delete."""
    headers = _fresh_user_headers("lifecycle")

    # 1. Create
    res = client.post("/api/visual-strategies", json=_sample_visual_strategy(), headers=headers)
    assert res.status_code == 201
    created = res.json()
    assert created["id"]
    assert created["underlying"] == "NIFTY50"  # normalized to uppercase
    assert created["is_active"] is False
    assert created["mode"] == "PAPER"

    # 2. List returns the created record
    list_res = client.get("/api/visual-strategies", headers=headers)
    assert list_res.status_code == 200
    ids = [item["id"] for item in list_res.json()]
    assert created["id"] in ids

    # 3. Partial PATCH toggles deploy state (real backend toggle)
    patch_res = client.patch(
        f"/api/visual-strategies/{created['id']}",
        json={"is_active": True},
        headers=headers,
    )
    assert patch_res.status_code == 200
    updated = patch_res.json()
    assert updated["is_active"] is True
    assert updated["name"] == created["name"]  # other fields untouched

    # 4. PATCH also supports editing content
    edit_res = client.patch(
        f"/api/visual-strategies/{created['id']}",
        json={"name": "Renamed Strategy", "legs": [{"type": "PE", "strike": "OTM1", "action": "BUY", "lots": 3}]},
        headers=headers,
    )
    assert edit_res.status_code == 200
    edited = edit_res.json()
    assert edited["name"] == "Renamed Strategy"
    assert edited["legs"][0]["lots"] == 3

    # 5. Delete
    del_res = client.delete(f"/api/visual-strategies/{created['id']}", headers=headers)
    assert del_res.status_code == 204
    gone = client.get("/api/visual-strategies", headers=headers)
    assert all(item["id"] != created["id"] for item in gone.json())


def test_visual_strategy_patch_delete_are_tenant_scoped():
    """User B must never patch or delete user A's visual strategy (404)."""
    headers_a = _fresh_user_headers("scopeda")
    headers_b = _fresh_user_headers("scopedb")

    res = client.post("/api/visual-strategies", json=_sample_visual_strategy(), headers=headers_a)
    assert res.status_code == 201
    vid = res.json()["id"]

    # B cannot toggle A's strategy
    assert client.patch(f"/api/visual-strategies/{vid}", json={"is_active": True}, headers=headers_b).status_code == 404
    # B cannot delete A's strategy
    assert client.delete(f"/api/visual-strategies/{vid}", headers=headers_b).status_code == 404

    # A's list is unaffected by B's attempts
    list_a = client.get("/api/visual-strategies", headers=headers_a).json()
    item = next(i for i in list_a if i["id"] == vid)
    assert item["is_active"] is False


def test_visual_strategy_create_validation():
    """Creation requires a name and at least one leg."""
    headers = _fresh_user_headers("val")

    bad = client.post("/api/visual-strategies", json={"name": "", "underlying": "NIFTY", "legs": []}, headers=headers)
    assert bad.status_code == 422

    ok = client.post("/api/visual-strategies", json={"name": "OK", "underlying": "NIFTY", "legs": [{"type": "CE", "strike": "ATM", "action": "BUY", "lots": 1}]}, headers=headers)
    assert ok.status_code == 201