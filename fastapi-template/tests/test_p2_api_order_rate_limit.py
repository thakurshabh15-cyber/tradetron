"""P2 — server-side per-user order-rate cap on the direct REST order endpoints.

The engine ``RiskManager`` only gates engine/webhook-originated orders.  The
authenticated REST order endpoints (``POST /api/trades/order`` and
``POST /api/v1/orders/execute-dma``) previously had NO server-side order-rate
cap, so a caller (or a leaked bearer token) could flood broker dispatch far
beyond ``MAX_ORDERS_PER_MINUTE``.  This is the API-side defense-in-depth
mirror of the engine's own cap.

  - RED: every request executes; the N+1-th order still returns 200.
  - GREEN: after ``settings.max_orders_per_minute`` approved requests in the
    window the next request returns HTTP 429 and creates no order row; each
    user's budget is independent; close-position and auth endpoints are not
    touched.

Note: closing positions is deliberately NOT rate-limited (exits reduce risk).
"""
from __future__ import annotations

import uuid as _uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import settings
from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.trading import OrderRecord


async def _register(client: AsyncClient) -> dict:
    """Register a fresh user and return bearer auth headers."""
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": f"ratelimit_{_uuid.uuid4().hex[:10]}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "Rate Limit Tester",
        },
    )
    assert reg.status_code == 201, reg.text
    data = reg.json()
    # Registration is OTP-verified; the initial token response is valid after
    # the auto-verify path used by the rest of the suite.
    return {"Authorization": f"Bearer {data['access_token']}"}


async def _total_order_rows() -> int:
    async with SessionLocal() as db:
        return len((await db.execute(select(OrderRecord))).scalars().all())


_MANUAL_PAYLOAD = {
    "symbol": "RELIANCE",
    "side": "BUY",
    "quantity": 1,
    "order_type": "MARKET",
    "mode": "PAPER",
}

_DMA_PAYLOAD = {
    "symbol": "NIFTY",
    "side": "BUY",
    "product": "MIS",
    "lots": 1,
    "order_type": "MARKET",
    "mode": "PAPER",
}


@pytest.mark.asyncio
async def test_manual_order_endpoint_rejects_beyond_user_budget(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "max_orders_per_minute", 3)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _register(client)
        before = await _total_order_rows()
        codes = []
        for _ in range(5):
            r = await client.post("/api/trades/order", json=_MANUAL_PAYLOAD, headers=headers)
            codes.append(r.status_code)

        assert codes[:3] == [200, 200, 200], codes
        assert codes[3:] == [429, 429], codes
        assert await _total_order_rows() == before + 3


@pytest.mark.asyncio
async def test_dma_order_endpoint_rejects_beyond_user_budget(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "max_orders_per_minute", 2)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = await _register(client)
        before = await _total_order_rows()
        codes = []
        for _ in range(4):
            r = await client.post("/api/v1/orders/execute-dma", json=_DMA_PAYLOAD, headers=headers)
            codes.append(r.status_code)

        assert codes[:2] == [200, 200], codes
        assert codes[2:] == [429, 429], codes
        assert await _total_order_rows() == before + 2


@pytest.mark.asyncio
async def test_different_users_have_independent_order_budgets(monkeypatch):
    """User B must not inherit user A's consumed budget (keys are per-user)."""
    await init_db()
    monkeypatch.setattr(settings, "max_orders_per_minute", 2)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers_a = await _register(client)
        headers_b = await _register(client)

        codes_a = []
        for _ in range(3):
            r = await client.post("/api/trades/order", json=_MANUAL_PAYLOAD, headers=headers_a)
            codes_a.append(r.status_code)
        assert codes_a == [200, 200, 429], codes_a

        # User B starts with a fresh budget.
        codes_b = []
        for _ in range(2):
            r = await client.post("/api/trades/order", json=_MANUAL_PAYLOAD, headers=headers_b)
            codes_b.append(r.status_code)
        assert codes_b == [200, 200], codes_b