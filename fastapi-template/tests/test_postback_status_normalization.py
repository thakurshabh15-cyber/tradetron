"""P1 regression tests: queued broker postback status normalization.

Target: the queued webhook path (``handle_broker_postback``) must normalize raw
broker statuses to the canonical order vocabulary BEFORE passing them to the
shared reconciler ``reconcile_broker_postback``, so that:

  - Zerodha "COMPLETE" / "COMPLETED" queued events reach the FILLED
    reconciliation path (the exact booking gap this closes - previously a raw
    "COMPLETE" fell through the reconciler's ``norm_status == "FILLED"`` check
    and never booked a TradeRecord + PositionRecord);
  - "FILLED" stays "FILLED";
  - "REJECTED" / "CANCELLED" / "CANCELED" remain terminal (no fill fabricated);
  - OPEN / NEW / PENDING don't fabricate a fill;
  - an UNKNOWN status does NOT fabricate a fill and does NOT guess a terminal
    state - it is dropped safely without any order/trade/position mutation;
  - a normalized FILLED event creates exactly ONE TradeRecord + PositionRecord,
    and a duplicate normalized FILLED delivery stays idempotent;
  - the queued and direct REST paths converge on the same reconciler.

All flows go through ``handle_broker_postback`` (queued worker) and the main app
(direct REST); BROKER_MODE stays "simulated", no real broker/payment/network is
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
from app.engine.order_reconciliation import normalize_broker_status
from app.webhooks.handlers.broker_postback import handle_broker_postback
from app.webhooks.queue.redis_streams import QueuedWebhook
from app.webhooks.validation.schemas import WebhookEnvelope

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


async def _seed_order(order_status: str = "OPEN") -> dict:
    """Create a user + CONNECTED broker + LIVE OrderRecord bound to that account."""
    async with SessionLocal() as db:
        uid = str(uuid.uuid4())
        user = UserRecord(
            id=uid,
            email=f"norm_{uid[:8]}@tradetron.io",
            hashed_password=hash_password("Pass12345!"),
            full_name="Normalization Tester",
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
            account_name="Normalization Account",
            client_id="CLIENT_NORM",
            status="CONNECTED",
            is_active=True,
        )
        broker.set_credentials(
            api_key="POSTKEY123", api_secret="POSTSECRET123", access_token="POSTTOKEN123"
        )
        db.add(broker)
        await db.flush()

        broker_order_id = f"KITE-{uuid.uuid4().hex[:10]}"
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


def _queued(seeded: dict, raw_status: str, **payload_overrides) -> QueuedWebhook:
    payload = {
        "broker_order_id": seeded["broker_order_id"],
        "status": raw_status,
        "symbol": "NIFTY50",
        "filled_quantity": 50,
        "average_price": 24850.0,
    }
    payload.update(payload_overrides)
    return QueuedWebhook(
        envelope=WebhookEnvelope(
            event_id=f"evt_{uuid.uuid4().hex}",
            event_type="order_update",
            timestamp=datetime.now(timezone.utc),
            provider="zerodha",
            payload=payload,
            idempotency_key=f"zerodha:evt_{seeded['broker_order_id']}",
        )
    )



# ── Unit: normalization table ────────────────────────────────────────────────


def test_normalize_broker_status_table():
    """The canonical reduction table holds: COMPLETE/COMPLETED -> FILLED, etc."""
    assert normalize_broker_status("COMPLETE") == "FILLED"
    assert normalize_broker_status("COMPLETED") == "FILLED"
    assert normalize_broker_status("FILLED") == "FILLED"
    assert normalize_broker_status("filled") == "FILLED"
    assert normalize_broker_status("REJECTED") == "REJECTED"
    assert normalize_broker_status("CANCELLED") == "CANCELLED"
    assert normalize_broker_status("CANCELED") == "CANCELLED"
    assert normalize_broker_status("EXPIRED") == "CANCELLED"
    assert normalize_broker_status("OPEN") == "OPEN"
    assert normalize_broker_status("NEW") == "OPEN"
    assert normalize_broker_status("PENDING") == "OPEN"
    assert normalize_broker_status("PARTIALLY_FILLED") == "OPEN"
    # Unknown / missing -> None (never guessed, never a fabricated fill).
    assert normalize_broker_status("BOGUS_STATUS") is None
    assert normalize_broker_status("") is None
    assert normalize_broker_status(None) is None


# ── Queued path integration ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queued_complete_reaches_filled_path():
    """Zerodha COMPLETE queued event reaches the FILLED reconciliation path and
    books exactly ONE TradeRecord + ONE PositionRecord (pre-fix: dropped)."""
    seeded = await _seed_order()
    await handle_broker_postback(_queued(seeded, "COMPLETE"))
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    assert order.filled_quantity == 50
    assert order.filled_price == 24850.0
    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 trade, got {len(trades)}"
    positions = await _fetch_postback_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 position, got {len(positions)}"


@pytest.mark.asyncio
async def test_queued_completed_maps_to_filled():
    """COMPLETED (Upstox vocabulary) maps to FILLED through the queued path."""
    seeded = await _seed_order()
    await handle_broker_postback(_queued(seeded, "COMPLETED"))
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    assert len(await _fetch_postback_trades(seeded["user_id"])) == 1


@pytest.mark.asyncio
async def test_queued_filled_stays_filled():
    """FILLED remains FILLED - normalization must not disturb the canonical form."""
    seeded = await _seed_order()
    await handle_broker_postback(_queued(seeded, "FILLED"))
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    assert len(await _fetch_postback_trades(seeded["user_id"])) == 1


@pytest.mark.asyncio
async def test_queued_rejected_remains_terminal_no_fill():
    """REJECTED stays a terminal rejection; no fill/trade is fabricated."""
    seeded = await _seed_order()
    await handle_broker_postback(_queued(seeded, "REJECTED"))
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "REJECTED"
    assert await _fetch_postback_trades(seeded["user_id"]) == []
    assert await _fetch_postback_positions(seeded["user_id"]) == []



@pytest.mark.asyncio
async def test_queued_cancelled_and_canceled_remain_terminal():
    """CANCELLED / CANCELED both remain terminal cancellation; no fill fabricated."""
    for raw in ("CANCELLED", "CANCELED"):
        seeded = await _seed_order()
        await handle_broker_postback(_queued(seeded, raw))
        order = await _fetch_order(seeded["order_id"])
        assert order.status == "CANCELLED", raw
        assert await _fetch_postback_trades(seeded["user_id"]) == []
    assert await _fetch_postback_positions(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_queued_open_non_terminal_does_not_fabricate_fill():
    """A recognized non-terminal OPEN/NEW/PENDING status does not fabricate a fill."""
    for raw in ("OPEN", "NEW", "PENDING"):
        seeded = await _seed_order()
        await handle_broker_postback(_queued(seeded, raw))
        order = await _fetch_order(seeded["order_id"])
        assert order.status == "OPEN", raw
        assert await _fetch_postback_trades(seeded["user_id"]) == []
        assert await _fetch_postback_positions(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_queued_unknown_status_fails_safe_no_mutation():
    """An unknown status does NOT fabricate a fill and does NOT guess a terminal
    state - the order row and ledgers are left untouched."""
    seeded = await _seed_order()
    await handle_broker_postback(_queued(seeded, "SOME_UNKNOWN_STATUS"))
    order = await _fetch_order(seeded["order_id"])
    # Order stays in its pre-event state (still OPEN, not guessed terminal).
    assert order.status == "OPEN"
    assert order.filled_quantity == 0
    assert await _fetch_postback_trades(seeded["user_id"]) == []
    assert await _fetch_postback_positions(seeded["user_id"]) == []


@pytest.mark.asyncio
async def test_duplicate_normalized_filled_delivery_idempotent():
    """Duplicate COMPLETE deliveries of the same fill book exactly ONE TradeRecord."""
    seeded = await _seed_order()
    for _ in range(2):
        await handle_broker_postback(_queued(seeded, "COMPLETE"))
    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 TradeRecord, got {len(trades)}"
    positions = await _fetch_postback_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"

@pytest.mark.asyncio
async def test_direct_rest_path_unchanged_and_converges_on_same_reconciler():
    """The direct REST path is UNCHANGED by this fix: a signed Zerodha COMPLETE
    direct postback still normalizes via ``process_postback`` and converges with
    the queued path (identical canonical FILLED booking through the shared
    ``reconcile_broker_postback`` - exactly ONE TradeRecord + PositionRecord)."""
    seeded = await _seed_order()
    payload = {
        "order_id": seeded["broker_order_id"],
        "status": "COMPLETE",
        "tradingsymbol": "NIFTY50",
        "filled_quantity": 50,
        "average_price": 24850.0,
        "broker_account_id": seeded["broker_id"],
    }
    payload["checksum"] = _zerodha_checksum(payload)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["reconciled_status"] == "FILLED"
        assert resp.json()["event_processed"] is True

    order = await _fetch_order(seeded["order_id"])
    assert order.status == "FILLED"
    assert order.filled_quantity == 50
    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 TradeRecord, got {len(trades)}"
    positions = await _fetch_postback_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"
