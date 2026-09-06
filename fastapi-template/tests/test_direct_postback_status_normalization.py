"""P1 regression tests: direct REST broker postback status normalization.

Target: the direct ``/api/brokers/postback/{broker_name}`` ingress
(``app.api.brokers.broker_postback_webhook``) must reduce raw broker statuses to
the canonical order vocabulary BEFORE handing them to the shared reconciler
``reconcile_broker_postback`` - mirroring the queued webhook worker
(``app.webhooks.handlers.broker_postback``) and reusing the SAME
``app.engine.order_reconciliation.normalize_broker_status`` (single token-set
source of truth, no duplicated status-token logic).

This closes the direct-REST booking gap: a raw Upstox/Angel ``COMPLETE`` or
``COMPLETED`` direct postback previously fell through the reconciler's
``norm_status == \"FILLED\"`` check and never booked a TradeRecord +
PositionRecord.  The regression tests pin:

  - non-Zerodha ``COMPLETE`` / ``COMPLETED`` / ``FILLED`` -> ``FILLED``;
  - ``REJECTED`` -> ``REJECTED``, ``CANCELLED``/``CANCELED``/``EXPIRED`` -> ``CANCELLED``;
  - recognized open states -> ``OPEN``;
  - UNKNOWN -> fail-closed (HTTP 200, ``event_processed`` False, no mutation);
  - duplicate fill delivery stays idempotent;
  - Zerodha direct path is NOT a regression (COMPLETE still books a fill).

All flows go through the main app; BROKER_MODE stays "simulated" and no real
broker/payment/network is ever contacted.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord

TEST_ZERODHA_API_KEY = "test_zerodha_key"
TEST_ZERODHA_API_SECRET = "test_zerodha_secret"

_CREATED_USER_IDS: list[str] = []

@pytest.fixture(autouse=True)
async def _cleanup_postback_data():
    _CREATED_USER_IDS.clear()
    yield
    async with SessionLocal() as db:
        for uid in set(_CREATED_USER_IDS):
            await db.execute(delete(TradeRecord).where(TradeRecord.user_id == uid))
            await db.execute(delete(OrderRecord).where(OrderRecord.user_id == uid))
            await db.execute(delete(PositionRecord).where(PositionRecord.user_id == uid))
            await db.execute(
                delete(BrokerAccountRecord).where(BrokerAccountRecord.user_id == uid)
            )
            await db.execute(delete(UserRecord).where(UserRecord.id == uid))
        await db.commit()
    _CREATED_USER_IDS.clear()


@pytest.fixture(autouse=True)
async def _init_db_and_settings():
    await init_db()
    settings.environment = "testing"
    settings.broker_mode = "simulated"
    settings.zerodha_api_key = TEST_ZERODHA_API_KEY
    settings.zerodha_api_secret = TEST_ZERODHA_API_SECRET
    # Default: dev-bypass so non-Zerodha providers need no verifier config; the
    # Zerodha regression test explicitly enables signature verification.
    settings.webhook_local_mode = True
    yield
    settings.broker_mode = "simulated"
    settings.webhook_local_mode = False


def _zerodha_checksum(payload: dict) -> str:
    """Zerodha postback checksum over the payload WITHOUT the checksum key."""
    without = {k: v for k, v in payload.items() if k != "checksum"}
    return hashlib.sha256(
        f"{TEST_ZERODHA_API_KEY}{json.dumps(without, separators=(',', ':'))}{TEST_ZERODHA_API_SECRET}".encode()
    ).hexdigest()


async def _seed_order(order_status: str = "OPEN", broker_name: str = "ZERODHA") -> dict:
    async with SessionLocal() as db:
        uid = str(uuid.uuid4())
        user = UserRecord(
            id=uid,
            email=f"directnorm_{uid[:8]}@tradetron.io",
            hashed_password=hash_password("Pass12345!"),
            full_name="Direct Norm Tester",
            role="trader",
            is_active=True,
            is_verified=True,
            paper_balance=1_000_000.0,
        )
        db.add(user)
        await db.flush()

        broker = BrokerAccountRecord(
            user_id=user.id,
            broker_name=broker_name,
            account_name="Direct Norm Account",
            client_id=f"CLIENT_{broker_name}_NORM",
            status="CONNECTED",
            is_active=True,
        )
        broker.set_credentials(
            api_key="POSTKEY123", api_secret="POSTSECRET123", access_token="POSTTOKEN123"
        )
        db.add(broker)
        await db.flush()

        broker_order_id = f"DIRECT-{uuid.uuid4().hex[:10]}"
        order = OrderRecord(
            user_id=user.id,
            broker_account_id=broker.id,
            broker_order_id=broker_order_id,
            symbol="NIFTY50",
            side="BUY",
            quantity=10,
            price=2400.0,
            filled_quantity=0,
            status=order_status,
            mode="LIVE",
        )
        db.add(order)
        await db.commit()
        _CREATED_USER_IDS.append(user.id)
        return {
            "user_id": user.id,
            "broker_id": broker.id,
            "order_id": order.id,
            "broker_order_id": broker_order_id,
        }


async def _fetch_order(order_id: str) -> OrderRecord:
    async with SessionLocal() as db:
        return await db.get(OrderRecord, order_id)


async def _fetch_postback_trades(user_id: str) -> list[TradeRecord]:
    async with SessionLocal() as db:
        res = await db.execute(
            select(TradeRecord).where(
                TradeRecord.user_id == user_id,
                TradeRecord.exit_reason == "BROKER_POSTBACK_FILL",
            )
        )
        return list(res.scalars().all())


async def _fetch_postback_positions(user_id: str) -> list[PositionRecord]:
    async with SessionLocal() as db:
        res = await db.execute(
            select(PositionRecord).where(PositionRecord.user_id == user_id)
        )
        return list(res.scalars().all())


async def _post_direct(
    broker_name: str,
    seeded: dict,
    status: str,
    filled_quantity: int = 50,
    average_price: float = 24850.0,
    signed: bool = True,
) -> httpx.Response:
    """POST a direct broker postback and return the HTTP response.

    For Zerodha the event is a Kite payload signed with the real checksum; for
    non-Zerodha brokers the signature check is bypassed via webhook_local_mode.
    """
    payload: dict = {
        "order_id": seeded["broker_order_id"],
        "status": status,
        "tradingsymbol": "NIFTY50",
        "filled_quantity": filled_quantity,
        "average_price": average_price,
        "broker_account_id": seeded["broker_id"],
    }
    if signed:
        payload["checksum"] = _zerodha_checksum(payload)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(f"/api/brokers/postback/{broker_name}", json=payload)


# ── Non-Zerodha raw canonicalization ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_direct_nonzerodha_complete_reaches_filled_path():
    """Upstox/Angel raw COMPLETE direct postback reaches the FILLED booking
    branch and books exactly ONE TradeRecord + PositionRecord (pre-fix: dropped)."""
    seeded = await _seed_order(broker_name="UPSTOX_PRO")
    resp = await _post_direct("UPSTOX_PRO", seeded, "COMPLETE")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reconciled_status"] == "FILLED"
    assert body["event_processed"] is True

    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    assert order.filled_quantity == 50
    assert order.filled_price == 24850.0
    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 trade, got {len(trades)}"
    positions = await _fetch_postback_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 position, got {len(positions)}"


@pytest.mark.asyncio
async def test_direct_nonzerodha_completed_maps_to_filled():
    """COMPLETED (Upstox vocabulary) maps to FILLED through the direct path."""
    seeded = await _seed_order(broker_name="UPSTOX_PRO")
    resp = await _post_direct("UPSTOX_PRO", seeded, "COMPLETED")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reconciled_status"] == "FILLED"
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    assert len(await _fetch_postback_trades(seeded["user_id"])) == 1


@pytest.mark.asyncio
async def test_direct_nonzerodha_filled_stays_filled():
    """FILLED remains FILLED - canonicalization must not disturb the form."""
    seeded = await _seed_order(broker_name="ANGEL_ONE")
    resp = await _post_direct("ANGEL_ONE", seeded, "FILLED")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reconciled_status"] == "FILLED"
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    assert len(await _fetch_postback_trades(seeded["user_id"])) == 1


@pytest.mark.asyncio
async def test_direct_nonzerodha_rejected_remains_terminal():
    """REJECTED stays terminal rejection; no fabricated fill/trade/position."""
    seeded = await _seed_order(broker_name="UPSTOX_PRO")
    resp = await _post_direct("UPSTOX_PRO", seeded, "REJECTED")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reconciled_status"] == "REJECTED"
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "REJECTED"
    assert await _fetch_postback_trades(seeded["user_id"]) == []
    assert await _fetch_postback_positions(seeded["user_id"]) == []



@pytest.mark.asyncio
async def test_direct_nonzerodha_cancellation_remains_terminal():
    """CANCELLED / CANCELED / EXPIRED all reduce to CANCELLED, no fill booked."""
    for raw in ("CANCELLED", "CANCELED", "EXPIRED"):
        seeded = await _seed_order(broker_name="ANGEL_ONE")
        resp = await _post_direct("ANGEL_ONE", seeded, raw)
        assert resp.status_code == 200, resp.text
        assert resp.json()["reconciled_status"] == "CANCELLED", raw
        order = await _fetch_order(seeded["order_id"])
        assert order.status == "CANCELLED", raw
        assert await _fetch_postback_trades(seeded["user_id"]) == [], raw
    assert await _fetch_postback_positions(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_direct_nonzerodha_open_does_not_fabricate_fill():
    """A recognized non-terminal OPEN/PARTIALLY_FILLED status does not fabricate a fill."""
    for raw in ("OPEN", "PARTIALLY_FILLED", "NEW"):
        seeded = await _seed_order(broker_name="UPSTOX_PRO")
        resp = await _post_direct("UPSTOX_PRO", seeded, raw)
        assert resp.status_code == 200, resp.text
        assert resp.json()["reconciled_status"] == "OPEN", raw
        order = await _fetch_order(seeded["order_id"])
        assert order.status == "OPEN", raw
        assert await _fetch_postback_trades(seeded["user_id"]) == [], raw
    assert await _fetch_postback_positions(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_direct_nonzerodha_unknown_status_fails_closed_no_mutation():
    """An UNKNOWN status does NOT fabricate a fill and does NOT guess a terminal
    state - the order row and ledgers are left untouched (reason=unknown_status)."""
    seeded = await _seed_order(broker_name="UPSTOX_PRO")
    resp = await _post_direct("UPSTOX_PRO", seeded, "SOME_UNKNOWN_STATUS")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["event_processed"] is False
    assert body["reason"] == "unknown_status"
    assert body["reconciled_status"] is None

    order = await _fetch_order(seeded["order_id"])
    assert order.status == "OPEN"
    assert order.filled_quantity == 0
    assert await _fetch_postback_trades(seeded["user_id"]) == []
    assert await _fetch_postback_positions(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_direct_nonzerodha_missing_status_fails_closed():
    """A missing/empty status maps to UNKNOWN -> fail-closed, no mutation."""
    seeded = await _seed_order(broker_name="UPSTOX_PRO")
    payload = {
        "order_id": seeded["broker_order_id"],
        "status": "",
        "broker_account_id": seeded["broker_id"],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/brokers/postback/UPSTOX_PRO", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_processed"] is False
    assert resp.json()["reason"] == "unknown_status"
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "OPEN"
    assert await _fetch_postback_trades(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_direct_duplicate_filled_complete_delivery_idempotent():
    """Duplicate FILLED/COMPLETE deliveries of the same fill book exactly ONE
    TradeRecord + PositionRecord (direct path, non-Zerodha)."""
    seeded = await _seed_order(broker_name="UPSTOX_PRO")
    for raw in ("COMPLETE", "FILLED", "COMPLETE"):
        resp = await _post_direct("UPSTOX_PRO", seeded, raw)
        assert resp.status_code == 200, resp.text
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 TradeRecord, got {len(trades)}"
    positions = await _fetch_postback_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"


# ── Zerodha direct path: no regression from adding canonicalization ───────────


@pytest.mark.asyncio
async def test_direct_zerodha_complete_still_books_fill_no_regression():
    """Signed Zerodha COMPLETE still reaches the FILLED booking branch after the
    canonicalization gate (idempotent over process_postback output)."""
    settings.webhook_local_mode = False
    seeded = await _seed_order(broker_name="ZERODHA")
    resp = await _post_direct("ZERODHA", seeded, "COMPLETE")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reconciled_status"] == "FILLED"
    assert body["event_processed"] is True

    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 trade, got {len(trades)}"
    positions = await _fetch_postback_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 position, got {len(positions)}"

