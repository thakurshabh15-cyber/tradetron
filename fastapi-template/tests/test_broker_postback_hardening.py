"""V3 regression tests: broker postback hardening.

Target: 'a broker postback that can mutate financial state MUST NOT be accepted
merely because it has valid JSON/schema' and every mutating postback must be
cryptographically verified AND broker-account-bound:

  10. A validly-signed postback is accepted through the existing verifier path
      and reconciles the matching order.
  11. A missing/invalid signature is rejected (HTTP 401) BEFORE any financial
      mutation.
  12. A signature valid for provider X can never mutate account B's order
      (cross-account / cross-tenant events are ignored without mutation).
  13. A tampered payload/signature is rejected.
  14. The queued worker path refuses to mutate an order that cannot be bound to a
      CONNECTED, tenant-owned broker account.
  15. Existing legitimate postback behavior (the signed happy path) continues to
      pass; local (dev/test) unsigned mode keeps its documented bypass.

All requests go through the main app (\x27app.main\x27) with mocked/absent external
services; BROKER_MODE stays "simulated" and no real broker/payment/network is
ever contacted.
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
from app.webhooks.handlers.broker_postback import handle_broker_postback
from app.webhooks.queue.redis_streams import QueuedWebhook
from app.webhooks.validation.schemas import WebhookEnvelope

TEST_ZERODHA_API_KEY = "test_zerodha_key"
TEST_ZERODHA_API_SECRET = "test_zerodha_secret"

# Tests run against the persistent trading.db; track user ids created by this
# module so each test can remove exactly its own rows (unique random boids keep
# event lookups colliding-free regardless).
_CREATED_USER_IDS: list[str] = []


@pytest.fixture(autouse=True)
async def _cleanup_postback_data():
    """Remove every row this module created, per test, from the shared DB."""
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
    """Deterministic hard configuration: signature verification ON, simulated mode."""
    await init_db()
    settings.environment = "testing"
    settings.webhook_local_mode = False
    settings.broker_mode = "simulated"
    settings.zerodha_api_key = TEST_ZERODHA_API_KEY
    settings.zerodha_api_secret = TEST_ZERODHA_API_SECRET
    yield
    settings.broker_mode = "simulated"


def _zerodha_checksum(payload: dict) -> str:
    """Compute the Zerodha postback checksum over the payload WITHOUT the checksum key."""
    without = {k: v for k, v in payload.items() if k != "checksum"}
    return hashlib.sha256(
        f"{TEST_ZERODHA_API_KEY}{json.dumps(without, separators=(',', ':'))}{TEST_ZERODHA_API_SECRET}".encode()
    ).hexdigest()


_NO_ACCOUNT = "<NO_ACCOUNT>"


async def _seed_order(
    broker_order_id: str = "KITE-ORD-8811",
    broker_status: str = "CONNECTED",
    broker_account_id: str | None = _NO_ACCOUNT,
    order_user_id: str | None = None,
) -> dict:
    """Create a user + broker account + OPEN LIVE OrderRecord bound to that account.

    By default the order is bound to the freshly-created broker. Pass
    ``broker_account_id=None`` explicitly to seed an order with NO broker
    account, or pass an account id to bind it to an existing account.
    """
    async with SessionLocal() as db:
        uid = str(uuid.uuid4())
        user = UserRecord(
            id=uid,
            email=f"postback_{uid[:8]}@tradetron.io",
            hashed_password=hash_password("Pass12345!"),
            full_name="Postback Tester",
            role="trader",
            is_active=True,
            is_verified=True,
            paper_balance=1_000_000.0,
        )
        db.add(user)
        await db.flush()

        broker = BrokerAccountRecord(
            user_id=user.id,
            broker_name="ZERODHA",
            account_name="Postback Account",
            client_id="CLIENT_Z",
            status=broker_status,
            is_active=True,
        )
        broker.set_credentials(
            api_key="POSTKEY123", api_secret="POSTSECRET123", access_token="POSTTOKEN123"
        )
        db.add(broker)
        await db.flush()

        if broker_account_id == _NO_ACCOUNT:
            order_broker_id = broker.id
        elif broker_account_id is None:
            order_broker_id = None
        else:
            order_broker_id = broker_account_id

        order = OrderRecord(
            user_id=order_user_id or user.id,
            broker_account_id=order_broker_id,
            broker_order_id=broker_order_id,
            symbol="NIFTY50",
            side="BUY",
            quantity=10,
            price=2400.0,
            filled_quantity=0,
            status="OPEN",
            mode="LIVE",
        )
        db.add(order)
        await db.commit()
        _CREATED_USER_IDS.append(user.id)
        return {"user_id": user.id, "broker_id": broker.id, "order_id": order.id}


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


def _make_postback_payload(
    broker_order_id: str = "KITE-ORD-8811",
    broker_account_id: str | None = None,
    signed: bool = True,
    tamper_after_signing: bool = False,
) -> dict:
    payload = {
        "order_id": broker_order_id,
        "status": "COMPLETE",
        "tradingsymbol": "NIFTY50",
        "filled_quantity": 50,
        "average_price": 24850.0,
    }
    if broker_account_id is not None:
        payload["broker_account_id"] = broker_account_id
    if tamper_after_signing:
        signed_payload = dict(payload)
        signed_payload["checksum"] = _zerodha_checksum(signed_payload)
        payload["filled_quantity"] = 999
        payload["checksum"] = signed_payload["checksum"]
        return payload
    if signed:
        payload["checksum"] = _zerodha_checksum(payload)
    else:
        payload["checksum"] = "0" * 64
    return payload


def _unique_boid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
@pytest.mark.asyncio
async def test_unsigned_direct_postback_rejected_no_mutation():
    """Item 11: missing signature => 401 BEFORE any financial mutation.

    (Pre-fix this endpoint accepted ANY unsigned postback, marked the order
    FILLED and created a CLOSED TradeRecord - this test fails pre-fix.)
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        boid = _unique_boid("PB-UNSIGNED")
        seeded = await _seed_order(broker_order_id=boid)
        payload = {
            "order_id": boid,
            "status": "COMPLETE",
            "tradingsymbol": "NIFTY50",
            "filled_quantity": 50,
            "average_price": 24850.0,
        }
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 401, resp.text

        order = await _fetch_order(seeded["order_id"])
        assert order.status == "OPEN", "order must NOT be mutated by an unsigned postback"
        assert order.filled_quantity == 0
        assert order.filled_price is None
        assert await _fetch_postback_trades(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_invalid_signature_direct_postback_rejected():
    """Item 13: invalid/tampered signature => 401 and no mutation."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        boid = _unique_boid("PB-INVALID")
        seeded = await _seed_order(broker_order_id=boid)
        payload = _make_postback_payload(boid, signed=False)  # wrong checksum
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 401, resp.text

        order = await _fetch_order(seeded["order_id"])
        assert order.status == "OPEN"
        assert await _fetch_postback_trades(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_tampered_payload_after_signing_rejected():
    """Item 13: tampering filled_quantity invalidates the carried signature."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        boid = _unique_boid("PB-TAMPER")
        seeded = await _seed_order(broker_order_id=boid)
        payload = _make_postback_payload(boid, tamper_after_signing=True)
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 401, resp.text

        order = await _fetch_order(seeded["order_id"])
        assert order.status == "OPEN"
        assert order.filled_quantity == 0
        assert await _fetch_postback_trades(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_valid_signed_postback_reconciles_order():
    """Item 10/15: a validly-signed postback reconciles the matching order."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        boid = _unique_boid("PB-VALID")
        seeded = await _seed_order(broker_order_id=boid)
        payload = _make_postback_payload(
            boid, broker_account_id=seeded["broker_id"], signed=True,
        )
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["reconciled_status"] == "FILLED"
        assert body["event_processed"] is True
        assert body["status"] == "ok"

        order = await _fetch_order(seeded["order_id"])
        assert order.status == "FILLED"
        assert order.filled_quantity == 50
        assert order.filled_price == 24850.0

        trades = await _fetch_postback_trades(seeded["user_id"])
        assert len(trades) == 1
        assert trades[0].mode == "LIVE"
        assert trades[0].exit_reason == "BROKER_POSTBACK_FILL"
        assert trades[0].user_id == seeded["user_id"]
        assert trades[0].order_id == seeded["order_id"]
@pytest.mark.asyncio
async def test_signed_postback_cross_account_ignored_without_mutation():
    """Item 12: a valid provider signature can never mutate another account's order."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        boid = _unique_boid("PB-CROSS")
        seeded = await _seed_order(broker_order_id=boid)

        # Second, different broker account (still a valid ZERODHA account).
        async with SessionLocal() as db:
            other_broker = BrokerAccountRecord(
                user_id=seeded["user_id"],
                broker_name="ZERODHA",
                account_name="Other Postback Account",
                client_id="CLIENT_Z2",
                status="CONNECTED",
                is_active=True,
            )
            other_broker.set_credentials(
                api_key="POSTKEY456", api_secret="POSTSECRET456", access_token="POSTTOKEN456"
            )
            db.add(other_broker)
            await db.commit()
            other_broker_id = other_broker.id

        # Signed correctly, but naming a broker_account_id that does NOT match the
        # order's account => the event is ignored (no mutation of account B's order).
        payload = _make_postback_payload(
            boid, broker_account_id=other_broker_id, signed=True,
        )
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["event_processed"] is False
        assert body["reason"] == "account_mismatch"

        order = await _fetch_order(seeded["order_id"])
        assert order.status == "OPEN", "cross-account postback must NOT mutate the order"
        assert order.filled_quantity == 0
        assert await _fetch_postback_trades(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_worker_postback_refuses_unbound_or_disconnected_order():
    """Item 14: the queued worker never mutates an order it cannot bind to a
    CONNECTED, tenant-owned broker account."""
    disc_boid = _unique_boid("PB-WORKER-DISC")
    none_boid = _unique_boid("PB-WORKER-NONE")
    # Case A: order bound to a DISCONNECTED broker account.
    seeded_a = await _seed_order(
        broker_order_id=disc_boid, broker_status="DISCONNECTED",
    )
    # Case B: order with NO broker account at all (nothing to bind).
    seeded_b = await _seed_order(
        broker_order_id=none_boid, broker_account_id=None,
    )

    for broker_order_id, expected_reason in [
        (disc_boid, "order_not_bound_to_connected_account"),
        (none_boid, "order_not_bound_to_connected_account"),
    ]:
        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id=f"evt_{broker_order_id}",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={
                    "broker_order_id": broker_order_id,
                    "status": "FILLED",
                    "symbol": "NIFTY50",
                    "filled_quantity": 50,
                    "average_price": 24850.0,
                },
                idempotency_key=f"zerodha:evt_{broker_order_id}",
            )
        )
        await handle_broker_postback(webhook)

    order_a = await _fetch_order(seeded_a["order_id"])
    assert order_a.status == "OPEN", "disconnected-account order must not be filled"
    order_b = await _fetch_order(seeded_b["order_id"])
    assert order_b.status == "OPEN", "unbound order must not be filled"
    assert await _fetch_postback_trades(seeded_a["user_id"]) == []
    assert await _fetch_postback_trades(seeded_b["user_id"]) == []


@pytest.mark.asyncio
async def test_local_mode_keeps_unsigned_bypass_for_dev():
    """Item 15 support: WEBHOOK_LOCAL_MODE keeps its documented unsigned bypass."""
    settings.webhook_local_mode = True
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            boid = _unique_boid("PB-LOCAL")
            seeded = await _seed_order(broker_order_id=boid)
            payload = {
                "order_id": boid,
                "status": "COMPLETE",
                "tradingsymbol": "NIFTY50",
                "filled_quantity": 50,
                "average_price": 24850.0,
            }
            resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
            assert resp.status_code == 200, resp.text
            assert resp.json()["event_processed"] is True

            order = await _fetch_order(seeded["order_id"])
            assert order.status == "FILLED"
    finally:
        settings.webhook_local_mode = False