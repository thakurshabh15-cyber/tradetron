"""Phase 3-A regression tests: object-level authorization on position close (IDOR).

Verifies the P1 fix in ``app/api/trades.py`` → ``close_position()``:

- unauthenticated close requests are rejected (401) — authentication now mandatory
- a second authenticated user CANNOT close another user's open position (403)
- malformed bearer tokens are rejected (401)
- the position owner can still close their own position (200) — valid behavior preserved

Before the fix, ``close_position`` accepted ``Optional[User]`` and fetched the
position with a bare ``db.get(PositionRecord, position_id)`` — no ownership
scope — so any authenticated (or even unauthenticated) caller could square off
another user's LIVE position, dispatch real broker close orders against foreign
state, and mutate another tenant's realized PnL.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import init_db
from app.main import app


@pytest.fixture(autouse=True)
def _local_auth_state(monkeypatch):
    """Use the in-memory OTP/rate-limit path so registration is deterministic
    even when a shared staging Redis is reachable (mirrors test_production_auth)."""
    from app.core import security as _sec

    monkeypatch.setattr(_sec, "_redis", lambda: None)
    yield


async def _register(client: AsyncClient, tag: str) -> dict:
    """Register a fresh synthetic user and return the TokenResponse payload."""
    uid = int(time.time() * 1000) % 1000000
    email = f"p3a_{tag}_{uid}@tradetron.io"
    res = await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "SecurePassword123!",
            "full_name": f"P3A {tag}",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


async def _place_paper_order(client: AsyncClient, headers: dict) -> str:
    """Open a PAPER-market long position for the authenticated user."""
    res = await client.post(
        "/api/trades/order",
        json={
            "symbol": "RELIANCE",
            "side": "BUY",
            "quantity": 25,
            "order_type": "MARKET",
            "mode": "PAPER",
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["success"] is True
    pos_id = data.get("position_id")
    assert pos_id, "place_manual_order should return a position_id"
    return pos_id


@pytest.mark.asyncio
async def test_close_position_requires_authentication():
    """An unauthenticated caller must be rejected with 401."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tokens = await _register(client, "anonclose")
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        pos_id = await _place_paper_order(client, headers)

        # No Authorization header at all → must fail before touching the position
        res = await client.post(f"/api/trades/positions/{pos_id}/close")
        assert res.status_code == 401
        assert "Authorization" in res.json().get("detail", "")

        # Position must remain OPEN — the owner can still see it
        pos_res = await client.get("/api/trades/positions", headers=headers)
        assert pos_res.status_code == 200
        assert any(p["id"] == pos_id and p["status"] == "OPEN" for p in pos_res.json())


@pytest.mark.asyncio
async def test_user_a_cannot_close_user_b_position():
    """Cross-user position close (IDOR) must be rejected with 403."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uid = int(time.time() * 1000) % 1000000

        # User A owns the position
        reg_a = await _register(client, f"ownera{uid}")
        headers_a = {"Authorization": f"Bearer {reg_a['access_token']}"}
        pos_id = await _place_paper_order(client, headers_a)

        # User B (attacker) attempts to close A's position
        reg_b = await _register(client, f"attackerb{uid}")
        headers_b = {"Authorization": f"Bearer {reg_b['access_token']}"}
        res = await client.post(f"/api/trades/positions/{pos_id}/close", headers=headers_b)
        assert res.status_code == 403, res.text
        assert "Not authorized" in res.json().get("detail", "")

        # Position untouched and still OWNED by A
        pos_res = await client.get("/api/trades/positions", headers=headers_a)
        assert pos_res.status_code == 200
        mine = [p for p in pos_res.json() if p["id"] == pos_id]
        assert mine and mine[0]["status"] == "OPEN"

        # And invisible to attacker B (user-scoped listing)
        pos_res_b = await client.get("/api/trades/positions", headers=headers_b)
        assert pos_res_b.status_code == 200
        assert all(p["id"] != pos_id for p in pos_res_b.json())


@pytest.mark.asyncio
async def test_close_position_rejects_malformed_token():
    """A malformed/garbage bearer token must be rejected with 401."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tokens = await _register(client, "malformed")
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        pos_id = await _place_paper_order(client, headers)

        res = await client.post(
            f"/api/trades/positions/{pos_id}/close",
            headers={"Authorization": "Bearer not.a.jwt"},
        )
        assert res.status_code == 401
        assert "expired" in res.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_owner_can_close_own_position():
    """The position owner must still be able to close their own position."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tokens = await _register(client, "ownerok")
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        pos_id = await _place_paper_order(client, headers)

        res = await client.post(f"/api/trades/positions/{pos_id}/close", headers=headers)
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["success"] is True
        assert data["status"] == "CLOSED"
        assert data["position_id"] == pos_id

        # Closed position no longer appears in open positions
        pos_res = await client.get("/api/trades/positions", headers=headers)
        assert pos_res.status_code == 200
        assert not any(p["id"] == pos_id for p in pos_res.json())