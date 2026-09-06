"""P0 — DMA / manual-order duplicate-execution & idempotency regression tests.

Verifies the post-fix behavior of POST /api/trades/place and
POST /api/v1/orders/execute-dma against the durable per-user idempotency claim:

 1. A keyed request is durably claimed (PENDING) and executes exactly once.
 2. A repeated same-key request replays the stored result without a second
    broker dispatch or duplicate order/trade/position rows.
 3. The same key belonging to different authenticated users cannot collide.
 4. A concurrent in-flight duplicate gets a deterministic 409.
 5. A completed duplicate returns/reuses the existing result.
 6. In-flight duplicates return a deterministic status.
 7. Broker rejection persists a durable REJECTED order, and retry succeeds.
 8. The BROKER_MODE=live guard remains enforced with no orphan claim.
 9. Unkeyed legacy requests preserve the pre-existing contract.
10. A different user cannot take over another user's idempotency key.
11. The DB model itself enforces the per-user unique key (partial index).
12. No real broker/payment/network call ever occurs — every broker path is
    PAPER (no dispatch) or a monkeypatched fake under BROKER_MODE=live.

SAFETY: these tests never contact a real broker.  LIVE-mode tests replace the
adapter/guard at the module boundary with deterministic fakes, exactly like
the existing live/paper suite.
"""

import asyncio
import uuid as _uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.db.session import init_db, SessionLocal
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord


async def _register(client: AsyncClient) -> tuple[dict, str]:
    """Register a fresh user; return (auth headers, user id)."""
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": f"idem_{_uuid.uuid4().hex[:10]}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "Idempotency Tester",
        },
    )
    assert reg.status_code == 201, reg.text
    data = reg.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["user"]["id"]


async def _create_connected_broker(user_id: str) -> str:
    """Insert a CONNECTED broker account owned by the user (no network)."""
    async with SessionLocal() as session:
        async with session.begin():
            rec = BrokerAccountRecord(
                user_id=user_id,
                broker_name="ZERODHA",
                status="CONNECTED",
                is_active=True,
            )
            rec.set_credentials("k", "s", "t")
            session.add(rec)
            await session.flush()
            return rec.id


async def _order_rows_for(user_id: str, key: str) -> list[OrderRecord]:
    async with SessionLocal() as db:
        return (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == user_id,
                    OrderRecord.client_order_id == key,
                )
            )
        ).scalars().all()


# ── manual /api/trades/place ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_paper_keyed_duplicate_dispatches_exactly_once_and_replays():
    """Requirement 1+2+5: first keyed request executes once; the duplicate
    replays the stored result and creates NO second order/trade/position."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        key = "TST-MAN-PAPER-0001"
        payload = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "PAPER", "client_order_id": key,
        }

        r1 = await client.post("/api/trades/place", json=payload, headers=headers)
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        assert b1["success"] is True and b1["status"] == "FILLED"
        assert "idempotent_replay" not in b1  # first execution, not a replay

        r2 = await client.post("/api/trades/place", json=payload, headers=headers)
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert b2["idempotent_replay"] is True
        assert b2["order_id"] == b1["order_id"]
        assert b2["position_id"] == b1["position_id"]
        assert b2["status"] == "FILLED"

        orders = await _order_rows_for(uid, key)
        assert len(orders) == 1 and orders[0].status == "FILLED"
        assert orders[0].position_id == b2["position_id"]

        async with SessionLocal() as db:
            trades = (
                await db.execute(
                    select(TradeRecord).where(TradeRecord.user_id == uid)
                )
            ).scalars().all()
            positions = (
                await db.execute(
                    select(PositionRecord).where(PositionRecord.user_id == uid)
                )
            ).scalars().all()
        assert len(trades) == 1
        assert len(positions) == 1


@pytest.mark.asyncio
async def test_manual_in_flight_duplicate_gets_deterministic_conflict():
    """Requirement 4+6: an existing PENDING claim yields a deterministic 409
    in-progress response; once the claim is complete the same key replays."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        key = "TST-MAN-INFLIGHT-01"
        async with SessionLocal() as db:
            async with db.begin():
                db.add(
                    OrderRecord(
                        user_id=uid, client_order_id=key,
                        symbol="RELIANCE", side="BUY", quantity=10,
                        order_type="MARKET", mode="PAPER", status="PENDING",
                    )
                )

        payload = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "PAPER", "client_order_id": key,
        }
        conflicted = await client.post("/api/trades/place", json=payload, headers=headers)
        assert conflicted.status_code == 409, conflicted.text
        detail = conflicted.json()["detail"]
        assert detail["status"] == "PENDING"
        assert "in progress" in detail["error"]

        # Resolve the claim to a completed state; the same key must now replay.
        async with SessionLocal() as db:
            claim = (
                await db.execute(
                    select(OrderRecord).where(
                        OrderRecord.user_id == uid,
                        OrderRecord.client_order_id == key,
                    )
                )
            ).scalar_one()
            claim.status = "FILLED"
            claim.broker_order_id = "ORD_TEST_FILLED"
            await db.commit()

        replayed = await client.post("/api/trades/place", json=payload, headers=headers)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["idempotent_replay"] is True
        assert replayed.json()["order_id"] == "ORD_TEST_FILLED"


@pytest.mark.asyncio
async def test_same_key_different_users_cannot_collide():
    """Requirement 3+10: the key namespace is per authenticated user — user B
    with user A's key gets B's OWN claim; neither can replay the other's."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers_a, uid_a = await _register(client)
        headers_b, uid_b = await _register(client)
        key = "TST-CROSS-USER-0001"
        payload_a = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "PAPER", "client_order_id": key,
        }
        payload_b = dict(payload_a, side="SELL")

        ra = await client.post("/api/trades/place", json=payload_a, headers=headers_a)
        rb = await client.post("/api/trades/place", json=payload_b, headers=headers_b)
        assert ra.status_code == 200 and rb.status_code == 200
        assert ra.json()["order_id"] != rb.json()["order_id"]

        # A retries with A's key → A's own result; B retries → B's own result.
        ra2 = await client.post("/api/trades/place", json=payload_a, headers=headers_a)
        rb2 = await client.post("/api/trades/place", json=payload_b, headers=headers_b)
        assert ra2.json()["idempotent_replay"] is True
        assert ra2.json()["order_id"] == ra.json()["order_id"]
        assert rb2.json()["idempotent_replay"] is True
        assert rb2.json()["order_id"] == rb.json()["order_id"]

        async with SessionLocal() as db:
            orders_a = (
                await db.execute(
                    select(OrderRecord).where(
                        OrderRecord.user_id == uid_a,
                        OrderRecord.client_order_id == key,
                    )
                )
            ).scalars().all()
            orders_b = (
                await db.execute(
                    select(OrderRecord).where(
                        OrderRecord.user_id == uid_b,
                        OrderRecord.client_order_id == key,
                    )
                )
            ).scalars().all()
        assert len(orders_a) == 1 and orders_a[0].user_id == uid_a
        assert len(orders_b) == 1 and orders_b[0].user_id == uid_b


@pytest.mark.asyncio
async def test_live_keyed_manual_dispatches_exactly_once(monkeypatch):
    """Requirement 1+2+12: with an opted-in LIVE mode + fake adapter, the first
    keyed request dispatches exactly once; duplicates replay WITHOUT a second
    broker order."""
    await init_db()
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    class _FakeBroker:
        def __init__(self) -> None:
            self.calls = 0

        async def place_order(self, req):  # noqa: ANN001
            self.calls += 1
            return {"broker_order_id": "BROKER-REF-01", "filled_price": 2500.0}

    fake = _FakeBroker()
    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda rec: fake)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        await _create_connected_broker(uid)
        key = "TST-LIVE-ONCE-00001"
        payload = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "LIVE", "client_order_id": key,
        }

        r1 = await client.post("/api/trades/place", json=payload, headers=headers)
        assert r1.status_code == 200, r1.text
        assert fake.calls == 1

        for _ in range(2):
            rn = await client.post("/api/trades/place", json=payload, headers=headers)
            assert rn.status_code == 200
            assert rn.json()["idempotent_replay"] is True
        assert fake.calls == 1, "duplicate requests must never dispatch again"

        orders = await _order_rows_for(uid, key)
        assert len(orders) == 1 and orders[0].status == "FILLED"


@pytest.mark.asyncio
async def test_broker_failure_persists_rejected_then_retry_succeeds(monkeypatch):
    """Requirement 7: broker rejection persists a durable REJECTED order (no
    fabricated FILLED, no duplicate trade/position); a later retry of the same
    key re-claims the SAME order row and succeeds."""
    await init_db()
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    class _FailingBroker:
        async def place_order(self, req):  # noqa: ANN001
            raise RuntimeError("simulated broker outage")

    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda rec: _FailingBroker())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        await _create_connected_broker(uid)
        key = "TST-LIVE-FAIL-00001"
        payload = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "LIVE", "client_order_id": key,
        }

        # Dispatch fails → 502, but the claim must be a durable REJECTED.
        rejected = await client.post("/api/trades/place", json=payload, headers=headers)
        assert rejected.status_code == 502
        async with SessionLocal() as db:
            claim = (
                await db.execute(
                    select(OrderRecord).where(
                        OrderRecord.user_id == uid,
                        OrderRecord.client_order_id == key,
                    )
                )
            ).scalar_one()
            row_id = claim.id
            assert claim.status == "REJECTED"
            assert "outage" in claim.error_message
            trades = (
                await db.execute(
                    select(TradeRecord).where(TradeRecord.user_id == uid)
                )
            ).scalars().all()
            positions = (
                await db.execute(
                    select(PositionRecord).where(PositionRecord.user_id == uid)
                )
            ).scalars().all()
        assert len(trades) == 0
        assert len(positions) == 0

        # Retry with a working adapter → same durable row is re-claimed and
        # finally FILLED (no duplicate row for the key).
        class _GoodBroker:
            async def place_order(self, req):  # noqa: ANN001
                return {"broker_order_id": "BROKER-OK-02", "filled_price": 2600.0}

        monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda rec: _GoodBroker())
        retry = await client.post("/api/trades/place", json=payload, headers=headers)
        assert retry.status_code == 200, retry.text
        assert retry.json()["status"] == "FILLED"

        orders = await _order_rows_for(uid, key)
        assert len(orders) == 1
        assert orders[0].id == row_id
        assert orders[0].status == "FILLED"


@pytest.mark.asyncio
async def test_live_guard_block_leaves_no_orphan_claim():
    """Requirement 8: with BROKER_MODE=simulated, a LIVE keyed request is
    blocked with 403 BEFORE any claim is created — the key stays usable."""
    await init_db()
    # conftest reset_broker_mode guarantees BROKER_MODE == "simulated" here.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        await _create_connected_broker(uid)
        key = "TST-LIVE-GUARDED-01"
        payload = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "LIVE", "client_order_id": key,
        }
        blocked = await client.post("/api/trades/place", json=payload, headers=headers)
        assert blocked.status_code == 403, blocked.text

        rows = await _order_rows_for(uid, key)
        assert rows == [], "a blocked LIVE attempt must never leave a PENDING claim"


@pytest.mark.asyncio
async def test_unkeyed_legacy_request_keeps_prior_contract():
    """Requirement 9: legacy unkeyed requests behave exactly as before — two
    submissions create two orders (non-idempotent by design) with no replay
    marker."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, _ = await _register(client)
        payload = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "PAPER",
        }
        r1 = await client.post("/api/trades/place", json=payload, headers=headers)
        r2 = await client.post("/api/trades/place", json=payload, headers=headers)
        assert r1.status_code == 200 and r2.status_code == 200
        assert "idempotent_replay" not in r1.json()
        assert "idempotent_replay" not in r2.json()
        assert r1.json()["order_id"] != r2.json()["order_id"]


@pytest.mark.asyncio
async def test_idempotency_key_header_accepted_and_mismatch_rejected():
    """Requirement: the Idempotency-Key header is accepted; body/header
    mismatch and malformed keys are rejected with 422."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, _ = await _register(client)
        payload = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "PAPER",
        }
        hdr = {**headers, "Idempotency-Key": "TST-HDR-KEY-0000001"}
        r1 = await client.post("/api/trades/place", json=payload, headers=hdr)
        assert r1.status_code == 200, r1.text
        r2 = await client.post("/api/trades/place", json=payload, headers=hdr)
        assert r2.status_code == 200 and r2.json()["idempotent_replay"] is True

        # Body key + a DIFFERENT header key → deterministic 422.
        mismatch = await client.post(
            "/api/trades/place",
            json={**payload, "client_order_id": "TST-BODY-KEY-000001"},
            headers={**headers, "Idempotency-Key": "TST-HDR-KEY-0000001"},
        )
        assert mismatch.status_code == 422

        # Malformed (too short) body key → 422.
        bad_key = await client.post(
            "/api/trades/place",
            json={**payload, "client_order_id": "short"},
            headers=headers,
        )
        assert bad_key.status_code == 422


# ── DMA /api/v1/orders/execute-dma ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_dma_paper_keyed_duplicate_replays_no_duplicate_rows():
    """Requirement 1+2+5 on the DMA endpoint: first keyed execution fills once;
    the duplicate replays the stored order/position without any new rows."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        key = "TST-DMA-PAPER-0001"
        payload = {
            "symbol": "NIFTY", "side": "BUY", "lots": 1, "product": "MIS",
            "order_type": "MARKET", "mode": "PAPER", "client_order_id": key,
            "stop_loss_pct": 0.5, "take_profit_pct": 1.0,
        }

        r1 = await client.post("/api/v1/orders/execute-dma", json=payload, headers=headers)
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        assert b1["success"] is True and b1["status"] == "FILLED"
        assert b1["quantity"] == 65 and b1["lot_size"] == 65

        r2 = await client.post("/api/v1/orders/execute-dma", json=payload, headers=headers)
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert b2["idempotent_replay"] is True
        assert b2["order_id"] == b1["order_id"]
        assert b2["position_id"] == b1["position_id"]
        assert b2["quantity"] == b1["quantity"]

        orders = await _order_rows_for(uid, key)
        assert len(orders) == 1 and orders[0].status == "FILLED"

        async with SessionLocal() as db:
            trades = (
                await db.execute(
                    select(TradeRecord).where(TradeRecord.user_id == uid)
                )
            ).scalars().all()
            positions = (
                await db.execute(
                    select(PositionRecord).where(PositionRecord.user_id == uid)
                )
            ).scalars().all()
        assert len(trades) == 1
        assert len(positions) == 1
@pytest.mark.asyncio
async def test_concurrent_in_flight_duplicates_cannot_both_claim(monkeypatch):
    """Requirement 4+6+12: while a PENDING claim exists, ANY number of
    concurrent identical requests all receive the deterministic 409 and NONE
    dispatch to the broker."""
    await init_db()
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    class _SlowBroker:
        def __init__(self) -> None:
            self.calls = 0

        async def place_order(self, req):  # noqa: ANN001
            self.calls += 1
            await asyncio.sleep(0.1)
            return {"broker_order_id": "BROKER-SLOW-01", "filled_price": 21000.0}

    fake = _SlowBroker()
    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda rec: fake)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        await _create_connected_broker(uid)
        key = "TST-DMA-CONC-00001"

        # Durable in-flight claim exists BEFORE the two requests arrive.
        async with SessionLocal() as db:
            async with db.begin():
                db.add(
                    OrderRecord(
                        user_id=uid, client_order_id=key,
                        symbol="NIFTY", side="BUY", quantity=65,
                        order_type="MARKET", mode="LIVE", status="PENDING",
                    )
                )

        payload = {
            "symbol": "NIFTY", "side": "BUY", "lots": 1, "product": "MIS",
            "order_type": "MARKET", "mode": "LIVE", "client_order_id": key,
        }

        async def _post() -> int:
            res = await client.post("/api/v1/orders/execute-dma", json=payload, headers=headers)
            return res.status_code

        codes = await asyncio.gather(_post(), _post())
        assert codes == [409, 409]
        assert fake.calls == 0, "in-flight duplicates must never reach a broker"


@pytest.mark.asyncio
async def test_model_enforces_per_user_partial_unique_key():
    """Requirement 11: the ORM metadata carries the durable partial unique
    index — two rows with the same (user_id, client_order_id) are rejected,
    while another user reusing the same key string is accepted."""
    import app.models.trading  # noqa: F401  (register metadata)
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.db.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    def _order(user: str, key: str) -> OrderRecord:
        return OrderRecord(
            user_id=user, client_order_id=key, symbol="NIFTY", side="BUY",
            quantity=65, order_type="MARKET", mode="PAPER",
        )

    async with Session() as s:
        s.add(_order("u-1", "KEY-0000000001"))
        await s.commit()
        s.add(_order("u-1", "KEY-0000000001"))
        with pytest.raises(IntegrityError):
            await s.commit()

    # Per-user scoping: a different user may use the same key string.
    async with Session() as s:
        s.add(_order("u-2", "KEY-0000000001"))
        await s.commit()  # must not raise

    await engine.dispose()
@pytest.mark.asyncio
async def test_dma_live_keyed_dispatches_exactly_once_and_replays_broker_ref(monkeypatch):
    """Requirement 1+2+12 on the DMA+LIVE path: the first keyed request
    dispatches exactly once and persists the broker order reference; the
    duplicate replays the stored broker reference with NO second dispatch."""
    await init_db()
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    class _FakeBroker:
        def __init__(self) -> None:
            self.calls = 0

        async def place_order(self, req):  # noqa: ANN001
            self.calls += 1
            return {"broker_order_id": "DMA-BROKER-REF-77", "filled_price": 21200.0}

    fake = _FakeBroker()
    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda rec: fake)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        await _create_connected_broker(uid)
        key = "TST-DMA-LIVE-00001"
        payload = {
            "symbol": "NIFTY", "side": "BUY", "lots": 1, "product": "MIS",
            "order_type": "MARKET", "mode": "LIVE", "client_order_id": key,
        }

        r1 = await client.post("/api/v1/orders/execute-dma", json=payload, headers=headers)
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        assert b1["status"] == "FILLED"
        assert b1["broker_order_id"] == "DMA-BROKER-REF-77"
        assert fake.calls == 1

        r2 = await client.post("/api/v1/orders/execute-dma", json=payload, headers=headers)
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert b2["idempotent_replay"] is True
        assert b2["broker_order_id"] == "DMA-BROKER-REF-77"
        assert b2["position_id"] == b1["position_id"]
        assert fake.calls == 1, "duplicate DMA request must never dispatch again"

        orders = await _order_rows_for(uid, key)
        assert len(orders) == 1
        assert orders[0].broker_order_id == "DMA-BROKER-REF-77"
# ── REJECTED-retry race: the re-claim must be an atomic compare-and-swap ────


@pytest.mark.asyncio
async def test_manual_concurrent_retry_after_rejected_dispatches_once_only(monkeypatch):
    """Requirement 3+4+7 on the retry path: a REJECTED idempotency key may be
    retried, but when several same-key retries arrive simultaneously the
    atomic re-claim admits exactly ONE winner.  Losing retries get a
    deterministic 409 (in-progress) or a completed replay — never a second
    broker dispatch.

    Pre-fix this test FAILS: the partial unique index
    ``ux_orders_user_client_order_id`` only guards INSERTs of new claims, so
    concurrent retries of the same REJECTED row could both flip it to PENDING,
    both commit (last-writer-wins UPDATE by PK), both return ``claimed`` and
    both dispatch — duplicate real orders.
    """
    await init_db()
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    class _CountingBroker:
        def __init__(self) -> None:
            self.calls = 0

        async def place_order(self, req):  # noqa: ANN001
            self.calls += 1
            return {"broker_order_id": "BROKER-RETRY-01", "filled_price": 2500.0}

    fake = _CountingBroker()
    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda rec: fake)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        await _create_connected_broker(uid)
        key = "TST-RETRY-RACE-01"

        # Durable REJECTED claim left behind by a previously-failed dispatch.
        async with SessionLocal() as db:
            async with db.begin():
                db.add(
                    OrderRecord(
                        user_id=uid, client_order_id=key,
                        symbol="RELIANCE", side="BUY", quantity=10,
                        order_type="MARKET", mode="LIVE", status="REJECTED",
                        error_message="prior simulated broker outage",
                    )
                )

        payload = {
            "symbol": "RELIANCE", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "mode": "LIVE", "client_order_id": key,
        }

        async def _retry() -> tuple[int, dict]:
            res = await client.post("/api/trades/place", json=payload, headers=headers)
            return res.status_code, res.json()

        results = await asyncio.gather(*[_retry() for _ in range(6)])

        assert fake.calls == 1, (
            f"exactly one retry may dispatch; the naive re-claim dispatched {fake.calls}"
        )
        claimed = [
            body for code, body in results
            if code == 200 and not body.get("idempotent_replay")
        ]
        assert len(claimed) == 1, "exactly one retry may claim and execute"
        assert claimed[0]["status"] == "FILLED"

        for code, body in results:
            assert code in (200, 409), code
            if code == 409:
                assert body["detail"]["status"] == "PENDING"

        orders = await _order_rows_for(uid, key)
        assert len(orders) == 1 and orders[0].status == "FILLED"
        async with SessionLocal() as db:
            trades = (
                await db.execute(
                    select(TradeRecord).where(TradeRecord.user_id == uid)
                )
            ).scalars().all()
            positions = (
                await db.execute(
                    select(PositionRecord).where(PositionRecord.user_id == uid)
                )
            ).scalars().all()
        assert len(trades) == 1 and len(positions) == 1
@pytest.mark.asyncio
async def test_dma_concurrent_retry_after_rejected_dispatches_once_only(monkeypatch):
    """Requirement 3+4+12 on the DMA retry path: concurrent same-key retries
    of a REJECTED claim admit exactly ONE broker dispatch; the losers never
    reach the broker (409 in-progress or completed replay)."""
    await init_db()
    monkeypatch.setattr("app.api.trades.assert_live_dispatch_allowed", lambda: None)

    class _CountingBroker:
        def __init__(self) -> None:
            self.calls = 0

        async def place_order(self, req):  # noqa: ANN001
            self.calls += 1
            return {"broker_order_id": "DMA-RETRY-01", "filled_price": 21100.0}

    fake = _CountingBroker()
    monkeypatch.setattr("app.api.trades.get_broker_adapter", lambda rec: fake)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers, uid = await _register(client)
        await _create_connected_broker(uid)
        key = "TST-DMA-RACE-0001"

        async with SessionLocal() as db:
            async with db.begin():
                db.add(
                    OrderRecord(
                        user_id=uid, client_order_id=key,
                        symbol="NIFTY", side="BUY", quantity=65,
                        order_type="MARKET", mode="LIVE", status="REJECTED",
                        error_message="prior simulated broker outage",
                    )
                )

        payload = {
            "symbol": "NIFTY", "side": "BUY", "lots": 1, "product": "MIS",
            "order_type": "MARKET", "mode": "LIVE", "client_order_id": key,
        }

        async def _retry() -> tuple[int, dict]:
            res = await client.post("/api/v1/orders/execute-dma", json=payload, headers=headers)
            return res.status_code, res.json()

        results = await asyncio.gather(*[_retry() for _ in range(6)])

        assert fake.calls == 1, (
            f"exactly one DMA retry may dispatch; the naive re-claim dispatched {fake.calls}"
        )
        claimed = [
            body for code, body in results
            if code == 200 and not body.get("idempotent_replay")
        ]
        assert len(claimed) == 1, "exactly one DMA retry may claim and execute"
        assert claimed[0]["status"] == "FILLED"

        for code, body in results:
            assert code in (200, 409), code
            if code == 409:
                assert body["detail"]["status"] == "PENDING"

        orders = await _order_rows_for(uid, key)
        assert len(orders) == 1 and orders[0].status == "FILLED"