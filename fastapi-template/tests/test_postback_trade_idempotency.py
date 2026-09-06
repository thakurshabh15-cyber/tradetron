"""V4 regression tests: broker postback trade-ledger idempotency.

Target: ``app.brokers.postback.reconcile_broker_postback`` (used by BOTH the
direct REST postback endpoint ``/api/brokers/postback/{broker_name}`` and the
queued webhook worker) must book EXACTLY ONE TradeRecord per order fill.

Defect (P1): a FILLED broker postback created a ``TradeRecord`` UNCONDITIONALLY
on every delivery. A broker HTTP retry / redelivery, the direct + queued paths
both firing for the same event, or a fill webhook arriving AFTER the order was
already finalized FILLED locally (REST / DMA / copy-trading entry path all
create a local TradeRecord) double-booked the SAME fill into the trade ledger:
duplicate trades, inflated PnL and duplicate report rows.

Regression coverage:
  1. Two IDENTICAL FILLED postbacks (duplicate delivery) book exactly ONE
     TradeRecord.
  2. A FILLED postback arriving after the order is ALREADY FILLED locally
     (with a local TradeRecord already on the ledger) books NO second trade.
  3. The queued worker path is equally idempotent (shared reconciler).
  4. The normal single FILLED postback still books exactly ONE TradeRecord
     (positive path preserved).
  5. Belt-and-braces: an OPEN order that already carries a
     BROKER_POSTBACK_FILL trade does not get a second one.

All requests go through the main app; BROKER_MODE stays "simulated" and no real
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
from app.webhooks.handlers.broker_postback import handle_broker_postback
from app.webhooks.queue.redis_streams import QueuedWebhook
from app.webhooks.validation.schemas import WebhookEnvelope

TEST_ZERODHA_API_KEY = "test_zerodha_key"
TEST_ZERODHA_API_SECRET = "test_zerodha_secret"

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


async def _seed_order(order_status: str = "OPEN", with_local_trade: bool = False) -> dict:
    """Create a user + broker account + LIVE OrderRecord bound to that account.

    ``order_status`` is the seeded order state before the postback arrives.
    ``with_local_trade`` simulates the local REST/DMA entry path having already
    booked a TradeRecord (its ``order_id`` field holds the synthetic local order
    string, as the entry path does - NOT the order UUID pk).
    """
    async with SessionLocal() as db:
        uid = str(uuid.uuid4())
        user = UserRecord(
            id=uid,
            email=f"idem_{uid[:8]}@tradetron.io",
            hashed_password=hash_password("Pass12345!"),
            full_name="Postback Idempotency Tester",
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
            account_name="Idempotency Account",
            client_id="CLIENT_IDEM",
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
        await db.flush()

        if with_local_trade:
            local_trade = TradeRecord(
                user_id=user.id,
                order_id=f"LOCAL_{uuid.uuid4().hex[:8]}",
                symbol="NIFTY50",
                side="BUY",
                quantity=10,
                price=2400.0,
                entry_price=2400.0,
                pnl=0.0,
                mode="LIVE",
            )
            db.add(local_trade)

        await db.commit()
        _CREATED_USER_IDS.append(user.id)
        return {
            "user_id": user.id,
            "broker_id": broker.id,
            "order_id": order.id,
            "broker_order_id": broker_order_id,
        }


async def _fetch_postback_trades(user_id: str) -> list[TradeRecord]:
    async with SessionLocal() as db:
        res = await db.execute(
            select(TradeRecord).where(
                TradeRecord.user_id == user_id,
                TradeRecord.exit_reason == "BROKER_POSTBACK_FILL",
            )
        )
        return list(res.scalars().all())


def _make_postback_payload(broker_order_id: str, broker_account_id: str | None = None) -> dict:
    payload = {
        "order_id": broker_order_id,
        "status": "COMPLETE",
        "tradingsymbol": "NIFTY50",
        "filled_quantity": 50,
        "average_price": 24850.0,
    }
    if broker_account_id is not None:
        payload["broker_account_id"] = broker_account_id
    payload["checksum"] = _zerodha_checksum(payload)
    return payload

# ---- 1. duplicate FILLED delivery books exactly ONE TradeRecord ------------


@pytest.mark.asyncio
async def test_duplicate_filled_postback_books_single_trade():
    """Two IDENTICAL signed FILLED postbacks must not double-book the fill.

    Pre-fix this failed: each delivery created a TradeRecord, corrupting the
    ledger with two identical trades for one fill.
    """
    seeded = await _seed_order(order_status="OPEN")
    payload = _make_postback_payload(
        seeded["broker_order_id"], broker_account_id=seeded["broker_id"],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        r2 = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json()["event_processed"] is True
        assert r2.json()["event_processed"] is True

    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 TradeRecord, got {len(trades)}"
    assert trades[0].quantity == 50
    assert trades[0].price == 24850.0
    assert trades[0].order_id == seeded["order_id"]


# ---- 2. FILLED postback after local finalization books NO second trade -----


@pytest.mark.asyncio
async def test_filled_postback_after_local_finalization_books_no_second_trade():
    """A fill webhook arriving AFTER local REST/DMA finalization (order already
    FILLED locally + a local TradeRecord on the ledger) must not book a second
    trade for the same fill."""
    seeded = await _seed_order(order_status="FILLED", with_local_trade=True)
    payload = _make_postback_payload(
        seeded["broker_order_id"], broker_account_id=seeded["broker_id"],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["event_processed"] is True

    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 0, f"expected NO duplicate postback trade, got {len(trades)}"

    # The ledger still holds exactly the ONE local trade from the entry path.
    async with SessionLocal() as db:
        all_trades = (
            await db.execute(select(TradeRecord).where(TradeRecord.user_id == seeded["user_id"]))
        ).scalars().all()
    assert len(all_trades) == 1, f"expected exactly 1 total trade, got {len(all_trades)}"

# ---- 3. queued worker path is equally idempotent (shared reconciler) -------


@pytest.mark.asyncio
async def test_queued_worker_duplicate_delivery_books_single_trade():
    """The queued worker routes through the same reconciler: two deliveries of
    the same FILLED event produce exactly one TradeRecord."""
    seeded = await _seed_order(order_status="OPEN")

    for _ in range(2):
        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id=f"evt_{uuid.uuid4().hex}",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={
                    "broker_order_id": seeded["broker_order_id"],
                    "status": "FILLED",
                    "symbol": "NIFTY50",
                    "filled_quantity": 50,
                    "average_price": 24850.0,
                },
                idempotency_key=f"zerodha:evt_{seeded['broker_order_id']}",
            )
        )
        await handle_broker_postback(webhook)

    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 TradeRecord, got {len(trades)}"


# ---- 4. positive path preserved: single FILLED postback still books ONE trade


@pytest.mark.asyncio
async def test_single_filled_postback_still_books_one_trade():
    """The normal single FILLED postback (OPEN -> FILLED) must still book exactly
    one TradeRecord - the guard must not suppress legitimate first fills."""
    seeded = await _seed_order(order_status="OPEN")
    payload = _make_postback_payload(
        seeded["broker_order_id"], broker_account_id=seeded["broker_id"],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["event_processed"] is True

    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1
    assert trades[0].order_id == seeded["order_id"]
    assert trades[0].mode == "LIVE"
    assert trades[0].exit_reason == "BROKER_POSTBACK_FILL"

# ---- 5. belt-and-braces: pre-existing postback trade suppresses a re-book ---


@pytest.mark.asyncio
async def test_open_order_with_existing_postback_trade_not_rebooked():
    """Even if the order status were OPEN (finalize/commit inconsistency), an
    existing BROKER_POSTBACK_FILL trade for the order prevents a second one."""
    seeded = await _seed_order(order_status="OPEN")

    # Manually attach a postback trade to the OPEN order (simulates a partial
    # commit/state anomaly), then deliver the same FILLED event again.
    async with SessionLocal() as db:
        existing = TradeRecord(
            user_id=seeded["user_id"],
            order_id=seeded["order_id"],
            symbol="NIFTY50",
            side="BUY",
            quantity=50,
            price=24850.0,
            entry_price=24850.0,
            exit_price=24850.0,
            mode="LIVE",
            exit_reason="BROKER_POSTBACK_FILL",
        )
        db.add(existing)
        await db.commit()

    payload = _make_postback_payload(
        seeded["broker_order_id"], broker_account_id=seeded["broker_id"],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text

    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 TradeRecord, got {len(trades)}"

# ---- 6. P1 position-ledger consistency: FILLED postback opens a position ----

async def _fetch_positions(user_id: str) -> list[PositionRecord]:
    async with SessionLocal() as db:
        res = await db.execute(
            select(PositionRecord).where(PositionRecord.user_id == user_id)
        )
        return list(res.scalars().all())


async def _fetch_order(order_id: str) -> OrderRecord | None:
    async with SessionLocal() as db:
        return await db.get(OrderRecord, order_id)


@pytest.mark.asyncio
async def test_filled_postback_opens_linked_position():
    """A broker-confirmed FILLED postback on a PENDING/OPEN LIVE order must
    book the matching OPEN PositionRecord (not just the TradeRecord).

    Defect (P1): the postback FILLED path booked ONLY a TradeRecord.  Every
    other confirmed-fill finalizer (order reconciliation engine, REST / DMA /
    copy-trading entry) creates a PositionRecord too, so a crash-window fill
    confirmed via postback silently vanished from the position ledger: no
    /positions row, no unrealized PnL, nothing to square off, and copy
    trading's mirror_close_position found no follower position to close.
    """
    seeded = await _seed_order(order_status="OPEN")
    payload = _make_postback_payload(
        seeded["broker_order_id"], broker_account_id=seeded["broker_id"],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["event_processed"] is True

    positions = await _fetch_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"
    pos = positions[0]
    assert pos.user_id == seeded["user_id"]
    assert pos.broker_account_id == seeded["broker_id"]
    assert pos.symbol == "NIFTY50"
    assert pos.side == "LONG"
    assert pos.quantity == 50          # broker-confirmed filled_quantity
    assert pos.entry_price == 24850.0  # broker-confirmed average_price
    assert pos.current_price == 24850.0
    assert pos.mode == "LIVE"
    assert pos.status == "OPEN"
    assert pos.opened_at is not None

    # The order must be linked to the position (same contract as every other
    # confirmed-fill finalizer).
    order = await _fetch_order(seeded["order_id"])
    assert order is not None
    assert order.status == "FILLED"
    assert order.position_id == pos.id


@pytest.mark.asyncio
async def test_sell_filled_postback_opens_short_position():
    """A SELL fill opens a SHORT position with broker-confirmed price/qty."""
    seeded = await _seed_order(order_status="OPEN")
    async with SessionLocal() as db:
        order = await db.get(OrderRecord, seeded["order_id"])
        order.side = "SELL"
        await db.commit()

    payload = _make_postback_payload(
        seeded["broker_order_id"], broker_account_id=seeded["broker_id"],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text

    positions = await _fetch_positions(seeded["user_id"])
    assert len(positions) == 1
    assert positions[0].side == "SHORT"
    assert positions[0].quantity == 50
    assert positions[0].entry_price == 24850.0


@pytest.mark.asyncio
async def test_duplicate_filled_postback_books_single_position():
    """Two IDENTICAL FILLED postbacks book exactly ONE TradeRecord AND exactly
    ONE PositionRecord - the position ledger must be as idempotent as the
    trade ledger."""
    seeded = await _seed_order(order_status="OPEN")
    payload = _make_postback_payload(
        seeded["broker_order_id"], broker_account_id=seeded["broker_id"],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        r2 = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert r1.status_code == 200 and r2.status_code == 200

    positions = await _fetch_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"
    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 1, f"expected 1 TradeRecord, got {len(trades)}"


@pytest.mark.asyncio
async def test_filled_postback_after_local_finalization_books_no_second_position():
    """A fill webhook arriving AFTER local REST/DMA finalization (order FILLED,
    local trade + linked PositionRecord on the ledger) must not book a second
    trade OR a second position."""
    seeded = await _seed_order(order_status="FILLED", with_local_trade=True)
    # Simulate the local entry path's linked open position.
    async with SessionLocal() as db:
        order = await db.get(OrderRecord, seeded["order_id"])
        local_pos = PositionRecord(
            user_id=seeded["user_id"],
            broker_account_id=seeded["broker_id"],
            symbol="NIFTY50",
            side="LONG",
            quantity=10,
            entry_price=2400.0,
            current_price=2400.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            mode="LIVE",
            status="OPEN",
            opened_at=datetime.now(timezone.utc),
        )
        db.add(local_pos)
        await db.flush()
        order.position_id = local_pos.id
        await db.commit()

    payload = _make_postback_payload(
        seeded["broker_order_id"], broker_account_id=seeded["broker_id"],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["event_processed"] is True

    positions = await _fetch_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"
    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 0, "no BROKER_POSTBACK_FILL trade may be re-booked"


@pytest.mark.asyncio
async def test_open_order_with_linked_position_not_rebooked():
    """Belt-and-braces sibling: even if the order status were OPEN (finalize /
    commit inconsistency) with a position already linked (order.position_id set
    by another finalizer), a FILLED postback must not create a second position
    or a duplicate postback trade."""
    seeded = await _seed_order(order_status="OPEN")
    async with SessionLocal() as db:
        order = await db.get(OrderRecord, seeded["order_id"])
        existing_pos = PositionRecord(
            user_id=seeded["user_id"],
            broker_account_id=seeded["broker_id"],
            symbol="NIFTY50",
            side="LONG",
            quantity=10,
            entry_price=2400.0,
            current_price=2400.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            mode="LIVE",
            status="OPEN",
            opened_at=datetime.now(timezone.utc),
        )
        db.add(existing_pos)
        await db.flush()
        order.position_id = existing_pos.id
        await db.commit()

    payload = _make_postback_payload(
        seeded["broker_order_id"], broker_account_id=seeded["broker_id"],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/brokers/postback/ZERODHA", json=payload)
        assert resp.status_code == 200, resp.text

    positions = await _fetch_positions(seeded["user_id"])
    assert len(positions) == 1, f"expected 1 PositionRecord, got {len(positions)}"
    trades = await _fetch_postback_trades(seeded["user_id"])
    assert len(trades) == 0, "no duplicate postback trade may be booked"
