"""P0 RED→GREEN — Canonical position normalization + Window-C reconciliation.

Tests that each adapter's real-shaped payloads are correctly normalized and
that ``_resolve_window_c`` produces the right outcome for every scenario.

RED evidence: old matcher reads ``pos.get("symbol")`` which returns "" for
Zerodha's ``tradingsymbol``, Angel's ``netqty``, Binance's ``positionAmt``.
Without normalization every real-shaped position is silently skipped.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.engine.order_reconciliation import BrokerOrderReconciliationEngine
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord
from app.models.user import UserRecord

from app.brokers.position_normalizer import (
    normalize_angelone_position,
    normalize_binance_position,
    normalize_simulated_position,
    normalize_upstox_position,
    normalize_zerodha_position,
)


@pytest.fixture(autouse=True)
async def _reset_db():
    await init_db()
    settings.broker_mode = "live"
    yield
    settings.broker_mode = "simulated"


async def _seed_user_and_broker() -> dict:
    uid = str(uuid.uuid4())
    broker_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(UserRecord(
            id=uid, email=f"norm_{uid[:8]}@tradetron.io",
            hashed_password=hash_password("SecurePassword123!"),
            full_name="Norm Tester", role="trader",
            is_active=True, is_verified=True, paper_balance=1_000_000.0,
        ))
        await db.flush()
        db.add(BrokerAccountRecord(
            id=broker_id, user_id=uid, broker_name="ZERODHA",
            account_name="Norm Acct", status="CONNECTED", is_active=True,
            token_expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(days=30),
            client_id="CLIENT_01",
            api_key_encrypted="mock_api_key_encrypted",
            api_secret_encrypted="mock_api_secret_encrypted",
            access_token_encrypted=f"mock_norm_token_{uid[:8]}",
        ))
        await db.commit()
    return {"uid": uid, "broker_id": broker_id}


async def _seed_claim(broker_id, uid, *, symbol="RELIANCE", side="BUY",
                      quantity=10):
    async with SessionLocal() as db:
        order = OrderRecord(
            user_id=uid, client_order_id=f"n-{uuid.uuid4().hex[:40]}",
            broker_account_id=broker_id, strategy_id=str(uuid.uuid4()),
            symbol=symbol, side=side, quantity=quantity, price=2500.0,
            order_type="MARKET", mode="LIVE", status="PENDING",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db.add(order)
        await db.commit()
        return order.id


def _future_now():
    return datetime.now(timezone.utc) + timedelta(minutes=10)


class _CanonicalBroker:
    def __init__(self, positions):
        self._positions = positions
    async def get_positions(self):
        return self._positions


class _RawBroker:
    """Returns raw (un-normalized) positions — simulates pre-fix bug."""
    def __init__(self, raw_positions):
        self._positions = raw_positions
    async def get_positions(self):
        return self._positions

# ── Normalizer unit tests ─────────────────────────────────────────────────

class TestZerodhaNormalization:
    def test_long(self):
        r = normalize_zerodha_position(
            {"tradingsymbol": "RELIANCE", "quantity": 10,
             "average_price": 2500.0, "pnl": 0.0, "product": "CNC"})
        assert r == {"symbol": "RELIANCE", "quantity": 10,
                     "side": "LONG", "average_price": 2500.0}

    def test_short(self):
        r = normalize_zerodha_position(
            {"tradingsymbol": "RELIANCE", "quantity": -5, "average_price": 2600.0})
        assert r["side"] == "SHORT" and r["quantity"] == -5

    def test_zero_dropped(self):
        assert normalize_zerodha_position({"tradingsymbol": "X", "quantity": 0}) is None

    def test_empty_symbol_dropped(self):
        assert normalize_zerodha_position({"tradingsymbol": "", "quantity": 10}) is None

    def test_non_numeric_dropped(self):
        assert normalize_zerodha_position({"tradingsymbol": "X", "quantity": "abc"}) is None

    def test_uppercased(self):
        r = normalize_zerodha_position({"tradingsymbol": "reliance", "quantity": 10})
        assert r["symbol"] == "RELIANCE"


class TestUpstoxNormalization:
    def test_long(self):
        r = normalize_upstox_position(
            {"tradingsymbol": "INFY", "quantity": 15, "buy_price": 1450.5})
        assert r == {"symbol": "INFY", "quantity": 15,
                     "side": "LONG", "average_price": 1450.5}

    def test_short(self):
        r = normalize_upstox_position(
            {"tradingsymbol": "INFY", "quantity": -15, "buy_price": 1500.0})
        assert r["side"] == "SHORT"

    def test_zero_dropped(self):
        assert normalize_upstox_position({"tradingsymbol": "X", "quantity": 0}) is None

    def test_missing_buy_price_defaults_zero(self):
        r = normalize_upstox_position({"tradingsymbol": "INFY", "quantity": 10})
        assert r["average_price"] == 0.0


class TestAngelOneNormalization:
    def test_long_string_qty(self):
        r = normalize_angelone_position(
            {"tradingsymbol": "SBIN", "netqty": "25", "averageprc": 620.75})
        assert r == {"symbol": "SBIN", "quantity": 25,
                     "side": "LONG", "average_price": 620.75}

    def test_short_string_qty(self):
        r = normalize_angelone_position(
            {"tradingsymbol": "SBIN", "netqty": "-10", "averageprc": 630.0})
        assert r["side"] == "SHORT" and r["quantity"] == -10

    def test_int_qty(self):
        r = normalize_angelone_position(
            {"tradingsymbol": "SBIN", "netqty": 25, "averageprc": 620.0})
        assert r["quantity"] == 25

    def test_string_avg_price(self):
        r = normalize_angelone_position(
            {"tradingsymbol": "SBIN", "netqty": 25, "averageprc": "620.75"})
        assert r["average_price"] == 620.75

    def test_zero_dropped(self):
        assert normalize_angelone_position(
            {"tradingsymbol": "SBIN", "netqty": "0"}) is None

    def test_empty_dropped(self):
        assert normalize_angelone_position({}) is None


class TestBinanceNormalization:
    def test_long(self):
        r = normalize_binance_position({"symbol": "BTCUSDT", "positionAmt": "100.0"})
        assert r == {"symbol": "BTCUSDT", "quantity": 100,
                     "side": "LONG", "average_price": 0.0}

    def test_large_qty(self):
        r = normalize_binance_position({"symbol": "BTCUSDT", "positionAmt": "100000.0"})
        assert r["quantity"] == 100000

    def test_zero_dropped(self):
        assert normalize_binance_position({"symbol": "X", "positionAmt": "0.0"}) is None

    def test_empty_symbol_dropped(self):
        assert normalize_binance_position({"symbol": "", "positionAmt": "10.0"}) is None

    def test_fractional_sub_one_truncates_to_zero_dropped(self):
        # 0.5 BTC truncates to 0 → dropped. Window-C then reports PENDING
        # (no confirmed exposure), which is safe — never a false CANCELLED.
        assert normalize_binance_position({"symbol": "BTCUSDT", "positionAmt": "0.5"}) is None


class TestSimulatedNormalization:
    def test_long(self):
        r = normalize_simulated_position(
            {"symbol": "RELIANCE", "quantity": 10, "avg_price": 2500.0})
        assert r == {"symbol": "RELIANCE", "quantity": 10,
                     "side": "LONG", "average_price": 2500.0}

    def test_short(self):
        r = normalize_simulated_position(
            {"symbol": "RELIANCE", "quantity": -10, "avg_price": 2500.0})
        assert r["side"] == "SHORT"

    def test_zero_dropped(self):
        assert normalize_simulated_position(
            {"symbol": "RELIANCE", "quantity": 0}) is None


# ── Integration: Window-C with canonical positions ────────────────────────

@pytest.mark.asyncio
async def test_zerodha_long_filled(monkeypatch):
    """Zerodha-shaped LONG position -> FILLED."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    canonical = [{"symbol": "RELIANCE", "quantity": 10,
                  "side": "LONG", "average_price": 2500.0}]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(canonical))
    oid = await _seed_claim(broker_id, uid)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["filled"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "FILLED"


@pytest.mark.asyncio
async def test_zerodha_short_filled(monkeypatch):
    """Zerodha-shaped SHORT -> FILLED for SELL."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "RELIANCE", "quantity": -10,
                              "side": "SHORT", "average_price": 2600.0}]))
    oid = await _seed_claim(broker_id, uid, side="SELL")
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["filled"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "FILLED"


@pytest.mark.asyncio
async def test_upstox_long_filled(monkeypatch):
    """Upstox-shaped LONG -> FILLED."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "INFY", "quantity": 15,
                              "side": "LONG", "average_price": 1450.50}]))
    oid = await _seed_claim(broker_id, uid, symbol="INFY", quantity=15)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["filled"] >= 1


@pytest.mark.asyncio
async def test_angelone_long_filled(monkeypatch):
    """Angel One-shaped LONG -> FILLED."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "SBIN", "quantity": 25,
                              "side": "LONG", "average_price": 620.75}]))
    oid = await _seed_claim(broker_id, uid, symbol="SBIN", quantity=25)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["filled"] >= 1


@pytest.mark.asyncio
async def test_binance_long_filled(monkeypatch):
    """Binance-shaped LONG -> FILLED."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "BTCUSDT", "quantity": 2,
                              "side": "LONG", "average_price": 0.0}]))
    oid = await _seed_claim(broker_id, uid, symbol="BTCUSDT", quantity=2)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["filled"] >= 1


@pytest.mark.asyncio
async def test_no_exposure_stays_pending(monkeypatch):
    """No matching position -> PENDING."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker([]))
    oid = await _seed_claim(broker_id, uid)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "PENDING"


@pytest.mark.asyncio
async def test_malformed_positions_stay_pending(monkeypatch):
    """Non-dict entries -> PENDING."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(["not-a-dict", 42, None]))
    oid = await _seed_claim(broker_id, uid)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "PENDING"


@pytest.mark.asyncio
async def test_non_list_positions_stay_pending(monkeypatch):
    """Non-list get_positions -> PENDING."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    class _NB:
        async def get_positions(self): return None
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda rec: _NB())
    oid = await _seed_claim(broker_id, uid)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "PENDING"


@pytest.mark.asyncio
async def test_broker_error_stays_pending(monkeypatch):
    """get_positions() raises -> PENDING."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    class _EB:
        async def get_positions(self): raise RuntimeError("timeout")
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda rec: _EB())
    oid = await _seed_claim(broker_id, uid)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "PENDING"


@pytest.mark.asyncio
async def test_opposite_direction_stays_pending(monkeypatch):
    """Wrong side -> PENDING."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "RELIANCE", "quantity": -10,
                              "side": "SHORT", "average_price": 2500.0}]))
    oid = await _seed_claim(broker_id, uid, side="BUY")
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "PENDING"


@pytest.mark.asyncio
async def test_partial_quantity_stays_pending(monkeypatch):
    """Position qty < order qty -> PENDING."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "RELIANCE", "quantity": 5,
                              "side": "LONG", "average_price": 2500.0}]))
    oid = await _seed_claim(broker_id, uid, quantity=10)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "PENDING"


@pytest.mark.asyncio
async def test_exact_qty_filled(monkeypatch):
    """Exact qty match -> FILLED."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "RELIANCE", "quantity": 10,
                              "side": "LONG", "average_price": 2510.0}]))
    oid = await _seed_claim(broker_id, uid, quantity=10)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["filled"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "FILLED"


@pytest.mark.asyncio
async def test_larger_qty_filled(monkeypatch):
    """Position qty > order qty -> FILLED."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "RELIANCE", "quantity": 20,
                              "side": "LONG", "average_price": 2510.0}]))
    oid = await _seed_claim(broker_id, uid, quantity=10)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["filled"] >= 1


@pytest.mark.asyncio
async def test_multi_positions_fills_on_unique_match(monkeypatch):
    """Multiple LONG positions: first confident match -> FILLED."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker([
                            {"symbol": "RELIANCE", "quantity": 10,
                             "side": "LONG", "average_price": 2500.0},
                            {"symbol": "RELIANCE", "quantity": 5,
                             "side": "LONG", "average_price": 2510.0},
                        ]))
    oid = await _seed_claim(broker_id, uid, quantity=10)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["filled"] >= 1


@pytest.mark.asyncio
async def test_wrong_symbol_pending(monkeypatch):
    """Different symbol -> PENDING."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "INFY", "quantity": 10,
                              "side": "LONG", "average_price": 1450.0}]))
    oid = await _seed_claim(broker_id, uid, symbol="RELIANCE")
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1


@pytest.mark.asyncio
async def test_zero_avg_price_falls_back_to_order_price(monkeypatch):
    """avg_price=0 -> uses order price."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _CanonicalBroker(
                            [{"symbol": "RELIANCE", "quantity": 10,
                              "side": "LONG", "average_price": 0.0}]))
    oid = await _seed_claim(broker_id, uid)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["filled"] >= 1
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "FILLED"


# ── RED evidence ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_red_evidence_raw_zerodha_skipped(monkeypatch):
    """RED: raw Zerodha shapes have no 'symbol' key -> not matched."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    raw = [{"tradingsymbol": "RELIANCE", "quantity": 10,
            "average_price": 2500.0}]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _RawBroker(raw))
    oid = await _seed_claim(broker_id, uid)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1
    assert "symbol" not in raw[0] and "tradingsymbol" in raw[0]
    async with SessionLocal() as db:
        assert (await db.get(OrderRecord, oid)).status == "PENDING"


@pytest.mark.asyncio
async def test_red_evidence_raw_angel_skipped(monkeypatch):
    """RED: raw Angel shapes have 'netqty' not 'quantity'."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    raw = [{"tradingsymbol": "SBIN", "netqty": "25", "averageprc": 620.75}]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _RawBroker(raw))
    oid = await _seed_claim(broker_id, uid, symbol="SBIN", quantity=25)
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1
    assert "netqty" in raw[0] and "quantity" not in raw[0]


@pytest.mark.asyncio
async def test_red_evidence_raw_binance_skipped(monkeypatch):
    """RED: raw Binance shapes have 'positionAmt' not 'quantity'."""
    seeded = await _seed_user_and_broker()
    uid, broker_id = seeded["uid"], seeded["broker_id"]
    raw = [{"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "0"}]
    monkeypatch.setattr("app.brokers.get_broker_adapter",
                        lambda rec: _RawBroker(raw))
    oid = await _seed_claim(broker_id, uid, symbol="BTCUSDT")
    s = await BrokerOrderReconciliationEngine().reconcile_once(now=_future_now())
    assert s["unknown"] >= 1
    assert "positionAmt" in raw[0] and "quantity" not in raw[0]


# ── Adapter integration ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_zerodha_adapter_normalizes():
    """Zerodha adapter get_positions() outputs canonical."""
    from app.brokers.zerodha import ZerodhaKiteBroker

    class _FakeKite:
        def positions(self):  # noqa: ANN201
            return {
                "net": [
                    {"tradingsymbol": "RELIANCE", "quantity": 10,
                     "average_price": 2500.0, "pnl": 0.0, "product": "CNC"},
                    {"tradingsymbol": "INFY", "quantity": 0,
                     "average_price": 1450.0, "pnl": 0.0, "product": "CNC"},
                ]
            }

    broker = ZerodhaKiteBroker.__new__(ZerodhaKiteBroker)
    broker._kite = _FakeKite()
    broker._connected = True
    broker.access_token = "mock-token"
    broker._is_connected = True
    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0] == {"symbol": "RELIANCE", "quantity": 10,
                            "side": "LONG", "average_price": 2500.0}
    assert "tradingsymbol" not in positions[0]


@pytest.mark.asyncio
async def test_simulated_adapter_normalizes():
    """SimulatedBroker get_positions() outputs canonical."""
    from app.brokers.simulated import SimulatedBroker
    broker = SimulatedBroker.__new__(SimulatedBroker)
    broker._positions = {
        "RELIANCE": {"quantity": 10, "avg_price": 2500.0},
        "INFY": {"quantity": -5, "avg_price": 1450.0},
    }
    positions = await broker.get_positions()
    assert len(positions) == 2
    rel = next(p for p in positions if p["symbol"] == "RELIANCE")
    assert rel["side"] == "LONG" and "avg_price" not in rel
    infy = next(p for p in positions if p["symbol"] == "INFY")
    assert infy["side"] == "SHORT" and infy["quantity"] == -5

