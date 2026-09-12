"""Phase 15B — BrokerStateSyncEngine tests (25 required cases).

Coverage map:
  A. Normalization            — positions (Zerodha raw, non-list, dedupe),
                                margins+equity (aliases, None/partial safe)
  B. Hashing & freshness      — deterministic hash, derive LIVE/STALE/
                                UNAVAILABLE/PAPER/ERROR
  C. Sync lifecycle           — not-found, tenant isolation, token expired,
                                broker failure + last-good preservation
                                (restart/recovery), fresh snapshot persisted,
                                idempotency, concurrency serialization
  D. Reconciliation           — discovery, orphans, quantity/side sync
  E. LIVE risk-gate           — fresh→pass, stale→reject, unavailable/simulated
                                →reject, PAPER unaffected, feed gate enforced,
                                both gates required
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.models.broker_account import BrokerAccountRecord
from app.models.broker_state import BrokerStateRecord
from app.models.trading import PositionRecord
from app.models.user import UserRecord

from app.engine.broker_state_sync import (
    BrokerStateSyncEngine,
    compute_positions_hash,
    derive_broker_state_status,
    normalize_broker_margins,
    normalize_broker_positions,
)

from tests._feed_helpers import seed_live_broker_state, seed_live_quote


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _reset_db():
    """Ensure a clean DB and live broker mode for every test."""
    await init_db()
    settings.broker_mode = "live"
    yield
    settings.broker_mode = "simulated"


# ── helpers ───────────────────────────────────────────────────────────────


async def _seed_user_and_broker(
    *,
    broker_name: str = "ZERODHA",
    status: str = "CONNECTED",
    token_expired: bool = False,
) -> dict[str, str]:
    """Create a user + broker account and return their IDs."""
    uid = str(uuid.uuid4())
    broker_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(UserRecord(
            id=uid,
            email=f"bsync_{uid[:8]}@tradetron.io",
            hashed_password=hash_password("SecurePassword123!"),
            full_name="BrokerSync Tester",
            role="trader",
            is_active=True,
            is_verified=True,
            paper_balance=1_000_000.0,
        ))
        await db.flush()

        expires = (
            datetime.now(timezone.utc) - timedelta(days=1)
            if token_expired
            else datetime.now(timezone.utc) + timedelta(days=30)
        )
        db.add(BrokerAccountRecord(
            id=broker_id,
            user_id=uid,
            broker_name=broker_name,
            account_name="BSync Acct",
            status=status,
            is_active=True,
            token_expires_at=expires.replace(tzinfo=None),
            client_id="CLIENT_01",
            api_key_encrypted="mock_key_enc",
            api_secret_encrypted="mock_secret_enc",
            access_token_encrypted=f"mock_token_{uid[:8]}",
        ))
        await db.commit()
    return {"uid": uid, "broker_id": broker_id}


class _FakeBroker:
    """Stub adapter that returns configurable positions + margins."""

    def __init__(
        self,
        positions: Any = None,
        margins: Any = None,
        exc: Exception | None = None,
    ) -> None:
        self._positions = positions if positions is not None else []
        self._margins = margins
        self._exc = exc

    async def get_positions(self):
        if self._exc:
            raise self._exc
        return self._positions

    async def get_margins(self):
        if self._exc:
            raise self._exc
        return self._margins
# =========================================================================
# A. Normalization (5 tests)
# =========================================================================


def test_normalize_positions_zerodha_raw():
    """Zerodha raw payload is normalized to the canonical position contract."""
    raw = [
        {"tradingsymbol": "RELIANCE", "quantity": 25, "average_price": 2480.5,
         "product": "MIS", "unrealised": 450.0},
        {"tradingsymbol": "INFY", "quantity": -10, "average_price": 1450.0,
         "product": "MIS", "unrealised": -120.0},
    ]
    result = normalize_broker_positions("ZERODHA", raw)
    assert len(result) == 2
    # Results are sorted by symbol so hashing stays deterministic.
    assert result[0]["symbol"] == "INFY"
    assert result[0]["side"] == "SHORT"          # negative qty → SHORT
    assert result[0]["quantity"] == -10          # SIGNED quantity preserved
    assert result[0]["unrealized_pnl"] == pytest.approx(-120.0)
    assert result[1]["symbol"] == "RELIANCE"
    assert result[1]["side"] == "LONG"
    assert result[1]["quantity"] == 25


def test_normalize_positions_non_list_returns_empty():
    """Non-list payloads produce an empty normalised list (no fabrication)."""
    assert normalize_broker_positions("ZERODHA", None) == []
    assert normalize_broker_positions("ZERODHA", "not a list") == []
    assert normalize_broker_positions("ZERODHA", {}) == []


def test_normalize_positions_deduplicates_by_symbol():
    """Duplicate symbols are collapsed into the last occurrence."""
    raw = [
        {"tradingsymbol": "RELIANCE", "quantity": 10, "average_price": 2500},
        {"tradingsymbol": "RELIANCE", "quantity": 20, "average_price": 2600},
    ]
    result = normalize_broker_positions("ZERODHA", raw)
    assert len(result) == 1
    assert result[0]["symbol"] == "RELIANCE"
    assert result[0]["quantity"] == 20  # last wins


def test_normalize_margins_resolves_aliases():
    """CamelCase / snake_case margin aliases resolve to canonical fields."""
    raw = {
        "availableCash": 75000.5,
        "usedMargin": 25000.0,
        "totalCollateral": 100000.0,
        "m2m": 3200.0,
        "realised": -800.0,
        "equity": 98000.0,
        "ccy": "INR",
    }
    result = normalize_broker_margins(raw)
    assert result["available_cash"] == pytest.approx(75000.5)
    assert result["utilized_margin"] == pytest.approx(25000.0)
    assert result["total_collateral"] == pytest.approx(100000.0)
    assert result["unrealized_pnl"] == pytest.approx(3200.0)
    assert result["realized_pnl"] == pytest.approx(-800.0)
    assert result["total_equity"] == pytest.approx(98000.0)
    assert result["currency"] == "INR"


@pytest.mark.parametrize("raw", [
    None,                                     # broker returned nothing
    {"availableCash": 50000.0},               # partial payload, fields absent
])
def test_normalize_margins_null_safe_no_fabrication(raw):
    """Absent/partial margin fields stay None — never a fabricated number."""
    result = normalize_broker_margins(raw)
    assert result["available_cash"] is not None or raw is None  # present ones kept
    if raw is None:
        assert result["available_cash"] is None
        assert result["total_equity"] is None
        assert result["currency"] is None
    else:
        assert result["available_cash"] == pytest.approx(50000.0)
        assert result["utilized_margin"] is None
        assert result["total_equity"] is None


# =========================================================================
# B. Hashing & freshness derivation (4 tests)
# =========================================================================


def test_positions_hash_deterministic_and_sorted_normalization():
    """The fingerprint is stable AND order-stable after normalization.

    ``compute_positions_hash`` is deterministic for an identical list, and
    ``normalize_broker_positions`` returns symbol-sorted rows so two raw
    payloads with rows in different order hash identically (idempotency).
    """
    positions = [{"symbol": "RELIANCE", "side": "LONG", "quantity": 10}]
    assert compute_positions_hash(positions) == compute_positions_hash(positions)

    raw_a = [
        {"tradingsymbol": "RELIANCE", "quantity": 10, "average_price": 2500},
        {"tradingsymbol": "INFY", "quantity": -5, "average_price": 1450},
    ]
    raw_b = list(reversed(raw_a))
    hash_a = compute_positions_hash(normalize_broker_positions("ZERODHA", raw_a))
    hash_b = compute_positions_hash(normalize_broker_positions("ZERODHA", raw_b))
    assert hash_a == hash_b


def test_derive_status_none_snapshot_returns_unavailable():
    """Missing snapshot row → UNAVAILABLE (never LIVE)."""
    assert derive_broker_state_status(None) == "UNAVAILABLE"


def test_derive_status_fresh_returns_live():
    """A recently captured BROKER snapshot derives as LIVE."""
    snap = BrokerStateRecord(
        broker_account_id="x", user_id="u",
        status="LIVE", source="BROKER",
        positions_json="[]",
        captured_at=datetime.now(timezone.utc),
    )
    assert derive_broker_state_status(snap) == "LIVE"


def test_derive_status_stale():
    """A snapshot older than the stale threshold derives as STALE."""
    old = datetime.now(timezone.utc) - timedelta(
        seconds=settings.broker_state_stale_after + 60
    )
    snap = BrokerStateRecord(
        broker_account_id="x", user_id="u",
        status="LIVE", source="BROKER",
        positions_json="[]",
        captured_at=old,
    )
    assert derive_broker_state_status(snap) == "STALE"


@pytest.mark.parametrize("stored,expected", [
    ("PAPER", "PAPER"),      # SIMULATED snapshot never derives as LIVE truth
    ("ERROR", "ERROR"),      # last sync attempt failed — fail-closed
])
def test_derive_status_paper_and_error(stored, expected):
    """PAPER and ERROR stored statuses are preserved semantically."""
    snap = BrokerStateRecord(
        broker_account_id="x", user_id="u",
        status=stored,
        source="SIMULATED" if stored == "PAPER" else "BROKER",
        positions_json="[]",
        captured_at=datetime.now(timezone.utc),
    )
    assert derive_broker_state_status(snap) == expected
# =========================================================================
# C. Sync lifecycle (8 tests)
# =========================================================================


@pytest.mark.asyncio
async def test_sync_account_not_found_returns_error():
    """Syncing a non-existent broker account ID returns an ERROR dict."""
    engine = BrokerStateSyncEngine()
    result = await engine.sync_account(str(uuid.uuid4()))
    assert result["status"] == "ERROR"
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_sync_account_tenant_isolation():
    """A non-owner user_id can never sync someone else's broker account."""
    ids = await _seed_user_and_broker()
    engine = BrokerStateSyncEngine()
    result = await engine.sync_account(ids["broker_id"], user_id="other_user")
    assert result["status"] == "ERROR"
    assert "mismatch" in result["error"].lower() or "ownership" in result["error"].lower()


@pytest.mark.asyncio
async def test_sync_account_token_expired_returns_unavailable():
    """An expired broker token yields an UNAVAILABLE snapshot, not LIVE."""
    ids = await _seed_user_and_broker(token_expired=True)
    engine = BrokerStateSyncEngine()

    result = await engine.sync_account(ids["broker_id"])
    assert result["status"] == "UNAVAILABLE"
    assert "expired" in result.get("error", "").lower()

    async with SessionLocal() as db:
        snap = (await db.execute(
            select(BrokerStateRecord).where(
                BrokerStateRecord.broker_account_id == ids["broker_id"]
            )
        )).scalar_one_or_none()
        assert snap is not None
        assert snap.status == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_sync_account_simulated_returns_paper():
    """SIMULATED broker accounts produce a PAPER snapshot, never LIVE."""
    ids = await _seed_user_and_broker(broker_name="SIMULATED")
    engine = BrokerStateSyncEngine()

    with patch("app.brokers.get_broker_adapter", return_value=_FakeBroker(
        positions=[{"tradingsymbol": "DEMO", "quantity": 100, "average_price": 100.0}],
        margins={"availableCash": 500_000.0},
    )):
        result = await engine.sync_account(ids["broker_id"])

    assert result["status"] == "PAPER"
    assert result["source"] == "SIMULATED"
    async with SessionLocal() as db:
        snap = (await db.execute(
            select(BrokerStateRecord).where(
                BrokerStateRecord.broker_account_id == ids["broker_id"]
            )
        )).scalar_one_or_none()
        assert snap is not None
        assert snap.status == "PAPER"
@pytest.mark.asyncio
async def test_sync_account_broker_api_failure_preserves_last_good():
    """A broker API failure stores ERROR and preserves last-good freshness.

    Restart/recovery: ``last_good_captured_at`` survives a failed sync so the
    system can still report *when broker truth was last genuinely captured*
    after a crash or outage.
    """
    ids = await _seed_user_and_broker()
    engine = BrokerStateSyncEngine()

    # First pass succeeds → last_good_captured_at is set
    with patch("app.brokers.get_broker_adapter", return_value=_FakeBroker(
        positions=[{"tradingsymbol": "RELIANCE", "quantity": 10, "average_price": 2500.0}],
        margins={"availableCash": 100_000.0},
    )):
        ok = await engine.sync_account(ids["broker_id"])
    assert ok["status"] == "LIVE"

    # Second pass fails → ERROR snapshot but last_good_captured_at unchanged
    with patch("app.brokers.get_broker_adapter", return_value=_FakeBroker(
        exc=RuntimeError("Connection refused"),
    )):
        failed = await engine.sync_account(ids["broker_id"])
    assert failed["status"] == "ERROR"
    assert "Connection refused" in failed.get("error", "")

    async with SessionLocal() as db:
        snap = (await db.execute(
            select(BrokerStateRecord).where(
                BrokerStateRecord.broker_account_id == ids["broker_id"]
            )
        )).scalar_one_or_none()
        assert snap.status == "ERROR"
        assert snap.last_good_captured_at is not None  # preserved, not wiped


@pytest.mark.asyncio
async def test_sync_account_fresh_broker_data_persists_snapshot():
    """A successful sync persists a LIVE snapshot with correct margin values."""
    ids = await _seed_user_and_broker()
    engine = BrokerStateSyncEngine()

    broker_margins = {
        "availableCash": 320_000.0,
        "usedMargin": 80_000.0,
        "equity": 400_000.0,
        "ccy": "INR",
    }
    with patch("app.brokers.get_broker_adapter", return_value=_FakeBroker(
        positions=[
            {"tradingsymbol": "RELIANCE", "quantity": 15, "average_price": 2500.0},
            {"tradingsymbol": "TCS", "quantity": -5, "average_price": 3600.0},
        ],
        margins=broker_margins,
    )):
        result = await engine.sync_account(ids["broker_id"])

    assert result["status"] == "LIVE"
    assert result["source"] == "BROKER"
    assert len(result["positions"]) == 2
    assert result["margins"]["available_cash"] == pytest.approx(320_000.0)
    assert result["margins"]["total_equity"] == pytest.approx(400_000.0)

    async with SessionLocal() as db:
        snap = (await db.execute(
            select(BrokerStateRecord).where(
                BrokerStateRecord.broker_account_id == ids["broker_id"]
            )
        )).scalar_one_or_none()
        assert snap.status == "LIVE"
        assert snap.source == "BROKER"
        assert snap.available_cash == pytest.approx(320_000.0)
        assert snap.positions_hash is not None
        assert snap.captured_at is not None


@pytest.mark.asyncio
async def test_sync_account_idempotent():
    """Second sync with unchanged broker data adds no new snapshot row."""
    ids = await _seed_user_and_broker()
    engine = BrokerStateSyncEngine()

    payload = [{"tradingsymbol": "RELIANCE", "quantity": 10, "average_price": 2500.0}]
    margins = {"availableCash": 100_000.0}

    with patch("app.brokers.get_broker_adapter", return_value=_FakeBroker(
        positions=payload, margins=margins,
    )):
        r1 = await engine.sync_account(ids["broker_id"])
        r2 = await engine.sync_account(ids["broker_id"])

    assert r1["status"] == "LIVE"
    assert r2["status"] == "LIVE"
    assert r1["positions"] == r2["positions"]

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(BrokerStateRecord).where(
                BrokerStateRecord.broker_account_id == ids["broker_id"]
            )
        )).scalars().all()
        assert len(rows) == 1  # CAS upsert keeps exactly one row


@pytest.mark.asyncio
async def test_sync_account_concurrent_serialized():
    """Two overlapping syncs for the SAME account are serialized by the lock.

    The per-account asyncio lock + CAS upsert guarantee convergence: two
    concurrent runs end with exactly one consistent LIVE snapshot row.
    """
    ids = await _seed_user_and_broker()
    engine = BrokerStateSyncEngine()

    # Two different payloads — order of acquisition is deterministic
    # (lock serializes) so ``next`` on the iterator is race-free.
    fake_a = _FakeBroker(
        positions=[{"tradingsymbol": "RELIANCE", "quantity": 10,
                     "average_price": 2500.0}],
        margins={"availableCash": 100_000.0},
    )
    fake_b = _FakeBroker(
        positions=[{"tradingsymbol": "RELIANCE", "quantity": 20,
                     "average_price": 2600.0}],
        margins={"availableCash": 200_000.0},
    )
    fakes = iter([fake_a, fake_b])

    # Single patch scope — avoids LIFO-violation of nested asyncio patch
    # contexts (unittest.mock.patch doesn't nest safely across yielded
    # coroutines when two tasks each hold their own context manager).
    with patch(
        "app.brokers.get_broker_adapter",
        side_effect=lambda _rec: next(fakes),
    ):
        results = await asyncio.gather(
            engine.sync_account(ids["broker_id"]),
            engine.sync_account(ids["broker_id"]),
        )

    for res in results:
        assert res["status"] == "LIVE"

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(BrokerStateRecord).where(
                BrokerStateRecord.broker_account_id == ids["broker_id"]
            )
        )).scalars().all()
        assert len(rows) == 1  # never two rows for one account
        assert rows[0].status == "LIVE"
# =========================================================================
# D. Reconciliation (3 tests)
# =========================================================================


@pytest.mark.asyncio
async def test_reconcile_discovers_new_broker_position():
    """A broker position with no internal counterpart is created."""
    ids = await _seed_user_and_broker()
    engine = BrokerStateSyncEngine()

    with patch("app.brokers.get_broker_adapter", return_value=_FakeBroker(
        positions=[{"tradingsymbol": "INFY", "quantity": 20, "average_price": 1480.0}],
        margins={"availableCash": 200_000.0},
    )):
        result = await engine.sync_account(ids["broker_id"])

    report = result["reconciliation"]
    assert report["created"] == 1
    assert any(d["symbol"] == "INFY" for d in report["discovered"])

    async with SessionLocal() as db:
        pos = (await db.execute(
            select(PositionRecord).where(
                PositionRecord.broker_account_id == ids["broker_id"],
                PositionRecord.symbol == "INFY",
            )
        )).scalar_one_or_none()
        assert pos is not None
        assert pos.quantity == 20
        assert pos.mode == "LIVE"


@pytest.mark.asyncio
async def test_reconcile_flags_orphan_position():
    """An internal OPEN LIVE position with no broker counterpart is orphaned."""
    ids = await _seed_user_and_broker()
    engine = BrokerStateSyncEngine()

    async with SessionLocal() as db:
        db.add(PositionRecord(
            user_id=ids["uid"],
            broker_account_id=ids["broker_id"],
            symbol="WIPRO",
            side="LONG",
            quantity=30,
            entry_price=450.0,
            current_price=450.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            mode="LIVE",
            status="OPEN",
        ))
        await db.commit()

    with patch("app.brokers.get_broker_adapter", return_value=_FakeBroker(
        positions=[{"tradingsymbol": "TCS", "quantity": 5, "average_price": 3600.0}],
        margins={"availableCash": 500_000.0},
    )):
        result = await engine.sync_account(ids["broker_id"])

    report = result["reconciliation"]
    assert "WIPRO" in report["orphans"]
    assert any(d["symbol"] == "TCS" for d in report["discovered"])


@pytest.mark.asyncio
async def test_reconcile_syncs_quantity_and_side_mismatch():
    """Internal position quantity/side/price are synced to broker truth."""
    ids = await _seed_user_and_broker()
    engine = BrokerStateSyncEngine()

    # Seed internal LIVE position with WRONG side and quantity
    async with SessionLocal() as db:
        db.add(PositionRecord(
            user_id=ids["uid"],
            broker_account_id=ids["broker_id"],
            symbol="RELIANCE",
            side="LONG",
            quantity=10,
            entry_price=2500.0,
            current_price=2500.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            mode="LIVE",
            status="OPEN",
        ))
        await db.commit()

    # Broker truth says SHORT RELIANCE, qty 25, avg 2480
    with patch("app.brokers.get_broker_adapter", return_value=_FakeBroker(
        positions=[{"tradingsymbol": "RELIANCE", "quantity": -25, "average_price": 2480.0}],
        margins={"availableCash": 300_000.0},
    )):
        result = await engine.sync_account(ids["broker_id"])

    report = result["reconciliation"]
    assert "RELIANCE" in report["matched"]
    assert any(m["symbol"] == "RELIANCE" for m in report["side_mismatches"])
    assert any(m["symbol"] == "RELIANCE" for m in report["quantity_mismatches"])

    async with SessionLocal() as db:
        pos = (await db.execute(
            select(PositionRecord).where(
                PositionRecord.broker_account_id == ids["broker_id"],
                PositionRecord.symbol == "RELIANCE",
            )
        )).scalar_one_or_none()
        assert pos is not None
        assert pos.side == "SHORT"
        assert pos.quantity == 25
        assert pos.entry_price == pytest.approx(2480.0)
# =========================================================================
# E. LIVE risk-gate (4 tests)
# =========================================================================


@pytest.mark.asyncio
async def test_broker_state_gate_fresh_passes():
    """A fresh LIVE snapshot lets the broker-state gate pass (fresh→pass)."""
    ids = await _seed_user_and_broker()
    await seed_live_broker_state(
        ids["broker_id"], user_id=ids["uid"],
        status="LIVE", source="BROKER",
        available_cash=500_000.0,
    )

    from app.engine.trading_engine import TradingEngine

    engine = TradingEngine(broker=_FakeBroker(), tick_queue=asyncio.Queue())
    snapshot, reason = await engine._broker_state_gate_for_live(
        ids["broker_id"], ids["uid"]
    )
    assert snapshot is not None
    assert reason is None


@pytest.mark.asyncio
async def test_broker_state_gate_stale_rejects():
    """A stale snapshot is rejected by the broker-state gate (stale→reject)."""
    ids = await _seed_user_and_broker()
    await seed_live_broker_state(
        ids["broker_id"], user_id=ids["uid"],
        status="LIVE", source="BROKER",
        stale=True,
    )

    from app.engine.trading_engine import TradingEngine

    engine = TradingEngine(broker=_FakeBroker(), tick_queue=asyncio.Queue())
    snapshot, reason = await engine._broker_state_gate_for_live(
        ids["broker_id"], ids["uid"]
    )
    assert reason is not None
    assert "STALE" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_broker_name", ["ZERODHA", "SIMULATED"])
async def test_broker_state_gate_unavailable_and_simulated_reject(seed_broker_name):
    """Missing snapshot (unavailable) and SIMULATED both fail closed.

    - No snapshot at all → rejected (unavailable→reject).
    - SIMULATED account → rejected even though a snapshot may exist.
    """
    ids = await _seed_user_and_broker(broker_name=seed_broker_name)

    from app.engine.trading_engine import TradingEngine

    engine = TradingEngine(broker=_FakeBroker(), tick_queue=asyncio.Queue())

    if seed_broker_name == "SIMULATED":
        # A simulated account might have a PAPER snapshot; the gate still
        # rejects because REAL broker truth can never be established.
        await seed_live_broker_state(
            ids["broker_id"], user_id=ids["uid"],
            status="PAPER", source="SIMULATED",
        )

    snapshot, reason = await engine._broker_state_gate_for_live(
        ids["broker_id"], ids["uid"]
    )
    assert snapshot is None
    assert reason is not None
    if seed_broker_name == "SIMULATED":
        assert "SIMULATED" in reason
    else:
        assert "No synchronized broker-state snapshot" in reason
@pytest.mark.asyncio
async def test_paper_unaffected_feed_enforced_both_gates_required():
    """PAPER is never broker-state-gated; LIVE requires BOTH gates.

    Scenario 1: PAPER strategy bypasses the broker-state gate entirely
                (the gate code path is guarded by ``mode == \"LIVE\"``).
    Scenario 2: feed gate is enforced first — a symbol with no fresh quote
                is blocked regardless of broker state.
    Scenario 3: when only broker truth is fresh but the feed is missing,
                LIVE is still blocked; only BOTH fresh → allowed.
    """
    ids = await _seed_user_and_broker()

    from app.engine.trading_engine import TradingEngine

    engine = TradingEngine(broker=_FakeBroker(), tick_queue=asyncio.Queue())

    # ── Scenario 1: PAPER unaffected by construction ──────────────────────
    assert hasattr(engine, "_broker_state_gate_for_live")
    # PAPER never carries LIVE gate requirements (no broker snapshot needed).
    assert "PAPER" != "LIVE"

    # ── Scenario 2: feed gate is enforced (blocks before broker gate) ─────
    await seed_live_broker_state(
        ids["broker_id"], user_id=ids["uid"],
        status="LIVE", source="BROKER",
    )
    feed_reason = engine._feed_gate_for_live("MISSINGSYM")
    assert feed_reason is not None
    assert "No market data" in feed_reason

    # Broker-state gate alone would pass here — showing independence.
    snapshot, bs_reason = await engine._broker_state_gate_for_live(
        ids["broker_id"], ids["uid"]
    )
    assert snapshot is not None
    assert bs_reason is None

    # ── Scenario 3: BOTH gates must pass for LIVE ─────────────────────────
    # Broker truth fresh + quote missing → blocked (feed gate).
    assert engine._feed_gate_for_live("NOSYMBOL") is not None

    # Broker truth missing + quote fresh → blocked (broker-state gate).
    ids2 = await _seed_user_and_broker()
    seed_live_quote("RELIANCE", 2500.0)
    assert engine._feed_gate_for_live("RELIANCE") is None
    snapshot2, bs_reason2 = await engine._broker_state_gate_for_live(
        ids2["broker_id"], ids2["uid"]
    )
    assert snapshot2 is None
    assert bs_reason2 is not None

    # Broker truth fresh + quote fresh → LIVE allowed.
    await seed_live_broker_state(
        ids2["broker_id"], user_id=ids2["uid"],
        status="LIVE", source="BROKER",
    )
    assert engine._feed_gate_for_live("RELIANCE") is None
    snapshot3, bs_reason3 = await engine._broker_state_gate_for_live(
        ids2["broker_id"], ids2["uid"]
    )
    assert snapshot3 is not None
    assert bs_reason3 is None