"""V4 regression tests: broker-unlink + LIVE position close safety.

Targets two linked P1 financial-correctness defects:

  1. ``unlink_broker_account`` (DELETE /api/brokers/accounts/{id}) must NOT
     delete a broker connection that still routes an OPEN position.  Deleting
     it leaves real exchange exposure whose ``positions.broker_account_id`` is
     either dangling (SQLite, FKs unenforced) or FK ``SET NULL``'d (PostgreSQL).

  2. ``close_position`` must fail CLOSED for a LIVE OPEN position whose broker
     account can no longer be resolved (deleted / disconnected / NULLed), rather
     than silently booking realized PnL WITHOUT squaring off the real position -
     a fabricated LIVE close.

All broker dispatch is faked; nothing contacts a real broker or network, and
BROKER_MODE stays "simulated" throughout.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.auth import create_access_token
from app.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import PositionRecord
from app.models.user import UserRecord


@pytest.fixture(autouse=True)
async def _reset_db_and_simulated_mode():
    """Ensure schema exists and LIVE dispatch is blocked by default."""
    await init_db()
    settings.broker_mode = "simulated"
    yield
    settings.broker_mode = "simulated"


async def _create_user(db, tag: str = "unlink") -> UserRecord:
    uid = str(uuid.uuid4())
    rec = UserRecord(
        id=uid,
        email=f"{tag}_{uid[:8]}@tradetron.io",
        hashed_password=hash_password("SecurePassword123!"),
        full_name="Broker Unlink Safety Tester",
        role="trader",
        is_active=True,
        is_verified=True,
        paper_balance=1_000_000.0,
    )
    db.add(rec)
    await db.flush()
    return rec


async def _create_broker_account(db, user_id: str) -> BrokerAccountRecord:
    rec = BrokerAccountRecord(
        user_id=user_id,
        broker_name="SIMULATED",
        account_name="Safe Account",
        client_id="CLIENT_01",
        status="CONNECTED",
        is_active=True,
    )
    rec.set_credentials(
        api_key="UNLINKKEY123",
        api_secret="UNLINKSECRET123",
        access_token="UNLINKTOKEN123",
    )
    db.add(rec)
    await db.flush()
    return rec


async def _create_open_position(
    db, user_id: str, broker_account_id: str, mode: str = "LIVE"
) -> PositionRecord:
    pos = PositionRecord(
        id=str(uuid.uuid4()),
        user_id=user_id,
        broker_account_id=broker_account_id,
        symbol="RELIANCE",
        side="LONG",
        quantity=10,
        entry_price=2500.0,
        current_price=2520.0,
        mode=mode,
        status="OPEN",
    )
    db.add(pos)
    await db.flush()
    return pos


async def _seed(user_tag: str) -> dict:
    """Seed a user + broker + OPEN position bound to the broker."""
    async with SessionLocal() as db:
        user = await _create_user(db, user_tag)
        broker = await _create_broker_account(db, user.id)
        position = await _create_open_position(db, user.id, broker.id)
        await db.commit()
        return {
            "user_id": user.id,
            "broker_id": broker.id,
            "position_id": position.id,
            "token": create_access_token({"sub": user.id}),
        }


# ── 1. unlink_broker_account must not delete an account with OPEN exposure ──


@pytest.mark.asyncio
async def test_unlink_blocked_with_open_position():
    """DELETE of a broker account holding an OPEN position is rejected (409)."""
    seeded = await _seed("guard")
    headers = {"Authorization": f"Bearer {seeded['token']}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.delete(
            f"/api/brokers/accounts/{seeded['broker_id']}", headers=headers
        )
    assert res.status_code == 409, res.text
    assert "OPEN" in res.json()["detail"]

    # Broker row must survive the rejected delete.
    async with SessionLocal() as db:
        broker_row = (
            await db.execute(
                select(BrokerAccountRecord).where(
                    BrokerAccountRecord.id == seeded["broker_id"]
                )
            )
        ).scalar_one_or_none()
        assert broker_row is not None, "broker account must not be deleted"
        pos_row = (
            await db.execute(
                select(PositionRecord).where(
                    PositionRecord.id == seeded["position_id"]
                )
            )
        ).scalar_one()
        assert pos_row.status == "OPEN"


@pytest.mark.asyncio
async def test_unlink_succeeds_for_closed_position_only():
    """A broker account with only CLOSED positions may be unlinked."""
    seeded = await _seed("guardclosed")

    async with SessionLocal() as db:
        pos = (
            await db.execute(
                select(PositionRecord).where(
                    PositionRecord.id == seeded["position_id"]
                )
            )
        ).scalar_one()
        pos.status = "CLOSED"
        await db.commit()

    headers = {"Authorization": f"Bearer {seeded['token']}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.delete(
            f"/api/brokers/accounts/{seeded['broker_id']}", headers=headers
        )
    assert res.status_code == 200, res.text

    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(BrokerAccountRecord).where(
                    BrokerAccountRecord.id == seeded["broker_id"]
                )
            )
        ).scalar_one_or_none()
        assert row is None, "broker account should now be deleted"



# ── 2. close_position must fail CLOSED for a LIVE position with no broker ──


@pytest.mark.asyncio
async def test_live_close_with_missing_broker_never_fabricates_close():
    """A LIVE OPEN position whose broker row is gone returns 503, not a fake close.

    Simulates the dangling FK (SQLite) / FK SET NULL (PostgreSQL) case that the
    unlink guard now prevents, and any legacy row already orphaned before the
    fix.
    """
    seeded = await _seed("orphan")

    # Simulate the broker row being removed (dangling/NULLed reference).
    async with SessionLocal() as db:
        broker_row = (
            await db.execute(
                select(BrokerAccountRecord).where(
                    BrokerAccountRecord.id == seeded["broker_id"]
                )
            )
        ).scalar_one()
        await db.delete(broker_row)
        await db.commit()

    headers = {"Authorization": f"Bearer {seeded['token']}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/trades/positions/{seeded['position_id']}/close", headers=headers
        )
    # Fail closed: 503, position NOT marked CLOSED, no PnL booked.
    assert res.status_code == 503, res.text
    assert "broker account" in res.json()["detail"].lower()

    async with SessionLocal() as db:
        pos = (
            await db.execute(
                select(PositionRecord).where(
                    PositionRecord.id == seeded["position_id"]
                )
            )
        ).scalar_one()
        assert pos.status == "OPEN", "position must NOT be fabricated to CLOSED"
        assert pos.realized_pnl == 0.0, "no PnL may be booked without a real close"


@pytest.mark.asyncio
async def test_live_close_happy_path_with_resolvable_broker(monkeypatch):
    """A LIVE OPEN position with a resolvable broker still closes successfully."""
    seeded = await _seed("happy")

    class _FakeBroker:
        async def place_order(self, req):  # noqa: ANN001
            return {"broker_order_id": "CLOSE-REF-99", "filled_price": 2530.0}

    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda rec: _FakeBroker())
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    headers = {"Authorization": f"Bearer {seeded['token']}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/trades/positions/{seeded['position_id']}/close", headers=headers
        )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "CLOSED"

    async with SessionLocal() as db:
        pos = (
            await db.execute(
                select(PositionRecord).where(
                    PositionRecord.id == seeded["position_id"]
                )
            )
        ).scalar_one()
        assert pos.status == "CLOSED"
        assert pos.realized_pnl > 0, "positive move should book positive PnL"

