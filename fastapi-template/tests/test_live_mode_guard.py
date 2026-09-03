"""Phase 3 — LIVE/PAPER separation guard tests.

Verifies the fail-safe that real broker order dispatch is hard-blocked when
the deployment is NOT in ``BROKER_MODE=live``, even if a user holds a connected
broker account and explicitly requests ``mode=\"LIVE\"``.
"""
import asyncio
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from app.main import app
from app.db.session import init_db, SessionLocal
from app.models.broker_account import BrokerAccountRecord
from app.models.user import UserRecord
from app.brokers import (
    BrokerModeBlockedError,
    assert_live_dispatch_allowed,
    live_dispatch_allowed,
)


# ── Unit tests for the guard primitive ───────────────────────────────────

def test_guard_primitive_blocked_in_simulated_mode(monkeypatch):
    monkeypatch.setattr("app.config.settings.broker_mode", "simulated")
    assert live_dispatch_allowed() is False
    with pytest.raises(BrokerModeBlockedError):
        assert_live_dispatch_allowed()


def test_guard_primitive_allowed_in_live_mode(monkeypatch):
    monkeypatch.setattr("app.config.settings.broker_mode", "live")
    assert live_dispatch_allowed() is True
    assert_live_dispatch_allowed()  # must not raise


# ── API-level guard tests ────────────────────────────────────────────────

async def _register_and_connect_broker(client) -> str:
    """Register a fresh user, return an auth header."""
    uid = int(time.time() * 1000) % 10_000_000
    email = f"guard_{uid}_{uuid.uuid4().hex[:6]}@tradetron.io"
    reg = await client.post("/api/auth/register", json={
        "email": email,
        "password": "SecurePassword123!",
        "full_name": "Guard Tester",
    })
    assert reg.status_code == 201, reg.text
    user = reg.json()["user"]
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Attach a CONNECTED broker owned by this user so the LIVE gate sees it.
    async with SessionLocal() as session:
        async with session.begin():
            rec = BrokerAccountRecord(
                user_id=user["id"],
                broker_name="ZERODHA",
                status="CONNECTED",
                is_active=True,
            )
            rec.set_credentials("k", "s", "t")
            session.add(rec)
    return headers, user["id"]


@pytest.mark.asyncio
async def test_live_order_blocked_when_broker_mode_simulated(monkeypatch):
    """A LIVE order must be rejected with 403 while BROKER_MODE is simulated,
    EVEN with a connected broker account."""
    monkeypatch.setattr("app.config.settings.broker_mode", "simulated")
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, _ = await _register_and_connect_broker(client)
        res = await client.post(
            "/api/trades/order",
            json={"symbol": "RELIANCE", "side": "BUY", "quantity": 10,
                  "order_type": "MARKET", "mode": "LIVE"},
            headers=headers,
        )
        assert res.status_code == 403, res.text
        assert "BROKER_MODE" in res.json()["detail"]


@pytest.mark.asyncio
async def test_live_order_allowed_when_broker_mode_live(monkeypatch):
    """With BROKER_MODE=live and a connected broker, LIVE dispatch proceeds
    (broker place_order is mocked to avoid a real network call)."""
    monkeypatch.setattr("app.config.settings.broker_mode", "live")
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, _ = await _register_and_connect_broker(client)

        fake_broker = AsyncMock()
        fake_broker.place_order = AsyncMock(return_value={
            "filled_price": 2500.0, "status": "FILLED", "broker_order_id": "LIVE1",
        })

        with patch("app.api.trades.get_broker_adapter", return_value=fake_broker):
            res = await client.post(
                "/api/trades/order",
                json={"symbol": "RELIANCE", "side": "BUY", "quantity": 10,
                      "order_type": "MARKET", "mode": "LIVE"},
                headers=headers,
            )
        assert res.status_code == 200, res.text
        assert res.json()["success"] is True
        assert fake_broker.place_order.await_count == 1


@pytest.mark.asyncio
async def test_paper_order_never_blocked_when_simulated(monkeypatch):
    """PAPER orders must always work, even with BROKER_MODE=simulated."""
    monkeypatch.setattr("app.config.settings.broker_mode", "simulated")
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Register a user WITHOUT a broker — PAPER never needs one.
        uid = int(time.time() * 1000) % 10_000_000
        reg = await client.post("/api/auth/register", json={
            "email": f"paper_{uid}_{uuid.uuid4().hex[:6]}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "Paper Tester",
        })
        assert reg.status_code == 201
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        res = await client.post(
            "/api/trades/order",
            json={"symbol": "RELIANCE", "side": "BUY", "quantity": 10,
                  "order_type": "MARKET", "mode": "PAPER"},
            headers=headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["success"] is True
