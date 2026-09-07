"""Comprehensive adversarial suite for copy-trading LIVE durable entry claims.

Approved design-gate scenarios A-N are covered:

  A. Crash before broker dispatch              - durable PENDING, no broker call
  B. Crash immediately after broker acceptance - PENDING, ref survives, recovery
  C. DB failure after broker acceptance        - ref recoverable, no re-dispatch
  D. Duplicate master signal                  - exactly one dispatch
  E. Concurrent duplicate fan-out            - exactly one claim/dispatch
  F. Missing/ambiguous broker reference      - stay PENDING, no fabricated fill
  G. Successful broker fill                  - FILLED + Trade + Position + links
  H. Broker rejection                        - same claim REJECTED, no dup row
  I. Window-C broker position exists         - canonical finalize on exposure
  J. Window-C no broker exposure            - conservative, stay PENDING
  K. Malformed broker positions              - safe/recoverable, no fabrication
  L. Tenant/account mismatch                 - no mutation, no cross-user access
  M. Partial fill                            - never over-book from partial info
  N. Recovered entry then close              - close exactly once

BROKER_MODE stays simulated unless a test explicitly sets it to "live" for the
focused dispatch being tested; the guard contract is never weakened.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.engine.copy_trading import (
    _claim_copy_follower_order,
    _copy_client_order_id,
    copy_trading_engine,
)
from app.engine.order_reconciliation import BrokerOrderReconciliationEngine
from app.models.broker_account import BrokerAccountRecord
from app.models.copy_trading import CopyFollowerRecord, CopyGroupRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord


@pytest.fixture(autouse=True)
async def _schema_and_simulated_mode():
    """Idempotent schema + deterministic isolation for GLOBAL reconciliation.

    ``reconcile_once()`` scans EVERY stale PENDING keyed LIVE order in the
    shared SQLite database (oldest-first, bounded batch), so leftover rows from
    a previous run or an earlier test in the same run would otherwise consume
    the batch and starve THIS test's own claim (see
    ``test_order_reconciliation._isolate_reconciliation_state`` — same pattern).
    Each test therefore starts from an empty orders/trades/positions/broker
    state so the reconciliation assertions are deterministic.

    SAFETY: pure local SQLite DELETEs on the app's own tables — no broker,
    payment, or network interaction of any kind.
    """
    from sqlalchemy import delete

    await init_db()
    async with SessionLocal() as session:
        async with session.begin():
            # FK-safe delete order: children before parents.
            await session.execute(delete(PositionRecord))
            await session.execute(delete(TradeRecord))
            await session.execute(delete(OrderRecord))
            await session.execute(delete(BrokerAccountRecord))
    settings.broker_mode = "simulated"
    yield
    settings.broker_mode = "simulated"


class _BaseBroker:
    """Default OK broker: confirms every order with id + fill price."""

    def __init__(self) -> None:
        self.dispatches: list[tuple[str, str, int]] = []
        self.order_status_responses: list[dict] = []
        self.positions: list[dict] = []

    async def place_order(self, req):
        self.dispatches.append((req.symbol, req.side, req.quantity))
        return {
            "order_id": f"ZMB_{uuid.uuid4().hex[:10]}",
            "filled_price": 2500.0,
            "status": "FILLED",
        }

    async def get_positions(self):
        return list(self.positions)

    async def get_margins(self):
        return {
            "available_cash": 500000.0,
            "utilized_margin": 0.0,
            "total_collateral": 500000.0,
        }

    async def get_order_status(self, order_id):
        if self.order_status_responses:
            return self.order_status_responses.pop(0)
        return {"status": "FILLED", "average_price": 2500.0}


class _CrashBeforeDispatchBroker(_BaseBroker):
    """Broker that never runs: the process dies before any broker call."""

    async def place_order(self, req):
        pytest.fail("A broker call MUST NOT happen before a durable claim commit")


class _AmbiguousBroker(_BaseBroker):
    """Broker accepts the order but returns no reference and no fill price."""

    async def place_order(self, req):
        self.dispatches.append((req.symbol, req.side, req.quantity))
        return {}


class _RejectingBroker(_BaseBroker):
    """Broker that rejects every order."""

    async def place_order(self, req):
        self.dispatches.append((req.symbol, req.side, req.quantity))
        raise RuntimeError("INSUFFICIENT_MARGIN")


class _NoPriceBroker(_BaseBroker):
    """Broker returns a reference but no confirmed fill price."""

    async def place_order(self, req):
        self.dispatches.append((req.symbol, req.side, req.quantity))
        return {"order_id": f"ZMB_{uuid.uuid4().hex[:10]}", "status": "NEW"}


class _MalformedPositionsBroker(_BaseBroker):
    """Broker whose get_positions() returns non-list / unusable payloads."""

    def __init__(self, payload) -> None:
        super().__init__()
        self._payload = payload

    async def get_positions(self):
        return self._payload


# ── Seeders / helpers ─────────────────────────────────────────────────────────


async def _seed_live_follower(
    broker_status: str = "CONNECTED",
    broker_owner_user_id: str | None = None,
) -> dict:
    """Seed a master, follower, broker, LIVE group + LIVE follower row.

    ``broker_owner_user_id`` lets a test bind the follower to ANOTHER user's
    broker account (case L).
    """
    master_id = str(uuid.uuid4())
    follower_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    broker_id = str(uuid.uuid4())
    owner_id = broker_owner_user_id or follower_id

    async with SessionLocal() as db:
        for uid, email in (
            (master_id, f"dc_master_{master_id[:8]}@tradetron.io"),
            (follower_id, f"dc_follower_{follower_id[:8]}@tradetron.io"),
            (other_id, f"dc_other_{other_id[:8]}@tradetron.io"),
        ):
            db.add(UserRecord(
                id=uid,
                email=email,
                hashed_password=hash_password("SecurePassword123!"),
                full_name="Durable Claim Tester",
                role="trader",
                is_active=True,
                is_verified=True,
                paper_balance=1_000_000.0,
            ))
        await db.flush()
        db.add(BrokerAccountRecord(
            id=broker_id,
            user_id=owner_id,
            broker_name="ZERODHA",
            account_name="Follower Live Acct",
            status=broker_status,
            is_active=True,
            token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            client_id="CLIENT_01",
            api_key_encrypted="mock_api_key_encrypted",
            api_secret_encrypted="mock_api_secret_encrypted",
            access_token_encrypted=f"mock_dc_token_{follower_id[:8]}",
        ))
        await db.flush()
        group = CopyGroupRecord(
            master_user_id=master_id, name="Durable Claim Group"
        )
        db.add(group)
        await db.flush()
        db.add(CopyFollowerRecord(
            group_id=group.id,
            follower_user_id=follower_id,
            broker_account_id=broker_id,
            multiplier=1.0,
            status="ACTIVE",
            mode="LIVE",
        ))
        await db.commit()

    return {
        "master_id": master_id,
        "follower_id": follower_id,
        "broker_id": broker_id,
        "other_id": other_id,
    }


async def _count_rows(model, user_id: str) -> int:
    async with SessionLocal() as db:
        return (await db.execute(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )).scalar_one()


async def _fetch_order(user_id: str) -> OrderRecord | None:
    async with SessionLocal() as db:
        return (await db.execute(
            select(OrderRecord).where(OrderRecord.user_id == user_id)
        )).scalars().first()


async def _fetch_position(user_id: str) -> PositionRecord | None:
    async with SessionLocal() as db:
        return (await db.execute(
            select(PositionRecord).where(PositionRecord.user_id == user_id)
        )).scalars().first()


async def _fetch_trade(user_id: str) -> TradeRecord | None:
    async with SessionLocal() as db:
        return (await db.execute(
            select(TradeRecord).where(TradeRecord.user_id == user_id)
        )).scalars().first()


def _master_dict(
    symbol: str = "INFY",
    side: str = "BUY",
    quantity: int = 10,
    price: float = 2500.0,
    mode: str = "LIVE",
) -> dict:
    """Deterministic master order dict (no persisted id -> dict-derived key)."""
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "order_type": "MARKET",
        "price": price,
        "filled_price": price,
        "mode": mode,
    }


def _set_live_mode_and_broker(
    monkeypatch, broker, *, patch_recon: bool = False
) -> None:
    """Enable BROKER_MODE=live and route the copy-trading (and optionally the
    reconciliation) broker adapter to the fake."""
    settings.broker_mode = "live"
    monkeypatch.setattr(
        "app.engine.copy_trading.get_broker_adapter",
        lambda broker_rec: broker,
    )
    if patch_recon:
        monkeypatch.setattr(
            "app.brokers.get_broker_adapter",
            lambda broker_rec: broker,
        )


async def _reconcile_once(now: datetime | None = None) -> dict:
    return await BrokerOrderReconciliationEngine().reconcile_once(now=now)


def _stale_now() -> datetime:
    """A 'now' far enough in the future that fresh claims look stale."""
    return datetime.now(timezone.utc) + timedelta(minutes=10)


# ── A. Crash before broker dispatch ───────────────────────────────────────────


class _SimulatedCrash(RuntimeError):
    """Models the process dying at a precise point in the entry path."""


@pytest.mark.asyncio
async def test_a_crash_before_broker_dispatch_leaves_claim_no_broker_call(
    monkeypatch,
):
    """A crash between the claim commit and broker dispatch leaves a durable
    PENDING claim, ZERO broker calls, and never a fabricated fill."""
    seeded = await _seed_live_follower()
    symbol = "INFY"
    broker = _CrashBeforeDispatchBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    async def _die_before_dispatch(**kwargs):
        raise _SimulatedCrash("process died before broker dispatch")

    monkeypatch.setattr(
        copy_trading_engine,
        "_dispatch_live_follower_order",
        _die_before_dispatch,
    )

    result = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=10),
        master_user_id=seeded["master_id"],
    )
    assert result["successful_copies"] == 0
    assert len(broker.dispatches) == 0, "broker must NOT be called"
    assert await _count_rows(OrderRecord, seeded["follower_id"]) == 1
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0
    assert await _count_rows(PositionRecord, seeded["follower_id"]) == 0

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None
    assert order.status == "PENDING"
    assert order.mode == "LIVE"
    assert order.client_order_id and order.client_order_id.startswith("cpy-")
    assert order.broker_order_id is None

    # Recovery: no exposure at the broker -> conservative, stays PENDING.
    summary = await _reconcile_once(now=_stale_now())
    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "PENDING", summary
    assert order.status == "PENDING", (
        "no confirmed exposure must never fabricate a fill"
    )


# ── B. Crash after broker acceptance ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_b_crash_after_acceptance_claim_survives_recovery_no_redispatch(
    monkeypatch,
):
    """Crash after broker acceptance binds a recoverable PENDING claim;
    Window-C reconciliation finalizes on confirmed exposure, and the recovered
    fill is never dispatched again."""
    seeded = await _seed_live_follower()
    symbol = "TATA"
    quantity = 5
    broker = _BaseBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    original_dispatch = copy_trading_engine._dispatch_live_follower_order

    async def _crash_after_acceptance(
        follower, symbol, side, calc_qty, order_type, price,
    ):
        outcome = await original_dispatch(
            follower=follower,
            symbol=symbol,
            side=side,
            calc_qty=calc_qty,
            order_type=order_type,
            price=price,
        )
        if outcome.get("success"):
            raise _SimulatedCrash("crash right after broker acceptance")
        return outcome

    monkeypatch.setattr(
        copy_trading_engine, "_dispatch_live_follower_order",
        _crash_after_acceptance,
    )

    result = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=seeded["master_id"],
    )
    assert result["successful_copies"] == 0
    assert len(broker.dispatches) == 1

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "PENDING"
    assert order.broker_order_id is None, "crash before ref commit (Window-C)"
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0

    broker.positions.append(
        {
            "symbol": symbol,
            "quantity": quantity,
            "side": "LONG",
            "average_price": 2500.0,
        }
    )
    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 1, summary

    order = await _fetch_order(seeded["follower_id"])
    assert order.status == "FILLED"
    position = await _fetch_position(seeded["follower_id"])
    assert position is not None and position.status == "OPEN"
    trade = await _fetch_trade(seeded["follower_id"])
    assert trade is not None
    assert trade.order_id == order.id, (
        "Trade must link to the durable claim's OrderRecord id"
    )

    # Duplicate fan-out of the SAME signal must NEVER dispatch again.
    result2 = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=seeded["master_id"],
    )
    assert result2["successful_copies"] == 1
    assert len(broker.dispatches) == 1, "recovered fill must not re-dispatch"


@pytest.mark.asyncio
async def test_b2_crash_after_ref_commit_window_b_recovery(monkeypatch):
    """Crash between the broker-ref commit (Window-B) and finalization is
    recoverable via get_order_status and never re-dispatches."""
    seeded = await _seed_live_follower()
    symbol = "SBIN"
    quantity = 8
    broker = _BaseBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    from app.engine.copy_trading import _persist_copy_follower_broker_ref

    async def _persist_ref_then_crash(*, claim_id, broker_order_id, **kw):
        await _persist_copy_follower_broker_ref(claim_id, broker_order_id)
        raise _SimulatedCrash("crash after broker-ref commit, before finalize")

    monkeypatch.setattr(
        "app.engine.copy_trading._persist_copy_follower_live_fill",
        _persist_ref_then_crash,
    )

    result = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=seeded["master_id"],
    )
    assert result["successful_copies"] == 0
    assert len(broker.dispatches) == 1

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "PENDING"
    assert order.broker_order_id is not None, (
        "broker ref must be durably committed before finalization"
    )

    broker.order_status_responses.append(
        {"status": "FILLED", "average_price": 2500.0}
    )
    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 1, summary
    order = await _fetch_order(seeded["follower_id"])
    assert order.status == "FILLED"
    assert len(broker.dispatches) == 1


# ── D. Duplicate master signal ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_d_duplicate_master_signal_single_dispatch(monkeypatch):
    """The same master signal + same follower must dispatch exactly once."""
    seeded = await _seed_live_follower()
    symbol = "AXIS"
    quantity = 7
    broker = _BaseBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    master = _master_dict(symbol=symbol, quantity=quantity)

    result1 = await copy_trading_engine.mirror_trade(
        master_order=master, master_user_id=seeded["master_id"],
    )
    assert result1["successful_copies"] == 1
    assert len(broker.dispatches) == 1

    # Identical fan-out again (same master dict -> same deterministic key).
    result2 = await copy_trading_engine.mirror_trade(
        master_order=master, master_user_id=seeded["master_id"],
    )
    assert result2["successful_copies"] == 1
    assert len(broker.dispatches) == 1, "duplicate signal must not re-dispatch"

    assert await _count_rows(OrderRecord, seeded["follower_id"]) == 1
    order = await _fetch_order(seeded["follower_id"])
    assert order.status == "FILLED"
    assert len(order.client_order_id) <= 64
    assert order.client_order_id.startswith("cpy-")


@pytest.mark.asyncio
async def test_d2_different_followers_do_not_collide(monkeypatch):
    """Two followers of the same master generate distinct keys and both fill."""
    # Second follower on a separate broker account.
    seed = await _seed_live_follower()
    master_id, first_follower = seed["master_id"], seed["follower_id"]
    second_follower = str(uuid.uuid4())
    second_broker = str(uuid.uuid4())
    symbol = "BAJAJ"
    quantity = 6

    async with SessionLocal() as db:
        db.add(UserRecord(
            id=second_follower,
            email=f"dc2_{second_follower[:8]}@tradetron.io",
            hashed_password=hash_password("SecurePassword123!"),
            full_name="Durable Claim Tester 2",
            role="trader", is_active=True, is_verified=True,
            paper_balance=1_000_000.0,
        ))
        await db.flush()
        db.add(BrokerAccountRecord(
            id=second_broker, user_id=second_follower, broker_name="ZERODHA",
            account_name="Follower2 Live", status="CONNECTED", is_active=True,
            token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            client_id="CLIENT_02",
            api_key_encrypted="mock_k2",
            api_secret_encrypted="mock_s2",
            access_token_encrypted=f"tok2_{second_follower[:8]}",
        ))
        await db.flush()
        group = (await db.execute(
            select(CopyGroupRecord).where(
                CopyGroupRecord.master_user_id == master_id
            )
        )).scalars().first()
        db.add(CopyFollowerRecord(
            group_id=group.id,
            follower_user_id=second_follower,
            broker_account_id=second_broker,
            multiplier=1.0,
            status="ACTIVE",
            mode="LIVE",
        ))
        await db.commit()

    broker = _BaseBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    result = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=master_id,
    )
    assert result["total_followers"] == 2
    assert result["successful_copies"] == 2
    assert len(broker.dispatches) == 2

    async with SessionLocal() as db:
        keys = (await db.execute(
            select(OrderRecord.client_order_id).where(
                OrderRecord.user_id.in_([first_follower, second_follower])
            )
        )).scalars().all()
    assert len(keys) == 2
    assert len(set(keys)) == 2, "followers must have distinct idempotency keys"


# ── E. Concurrent duplicate fan-out ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e_concurrent_duplicate_fanout_single_claim(monkeypatch):
    """Two concurrent mirror_trade calls for the same signal on the same
    follower must result in exactly ONE claim and ONE broker dispatch."""
    seeded = await _seed_live_follower()
    symbol = "ICICI"
    quantity = 9
    broker = _BaseBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    master = _master_dict(symbol=symbol, quantity=quantity)

    r1, r2 = await asyncio.gather(
        copy_trading_engine.mirror_trade(
            master_order=master, master_user_id=seeded["master_id"],
        ),
        copy_trading_engine.mirror_trade(
            master_order=master, master_user_id=seeded["master_id"],
        ),
    )
    # Both calls return logically-successful outcomes (one real, one replay).
    assert r1["successful_copies"] == 1
    assert r2["successful_copies"] == 1
    assert len(broker.dispatches) == 1, "concurrent fan-out must dispatch once"
    assert await _count_rows(OrderRecord, seeded["follower_id"]) == 1
    assert await _count_rows(PositionRecord, seeded["follower_id"]) == 1
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 1


# ── C. DB failure after broker acceptance ─────────────────────────────────────


@pytest.mark.asyncio
async def test_c_db_failure_after_acceptance_recoverable_no_redispatch(
    monkeypatch,
):
    """A DB failure after broker acceptance must leave the claim + broker ref
    recoverable (Window-B) and must NEVER cause a second dispatch."""
    seeded = await _seed_live_follower()
    symbol = "HDFC"
    quantity = 12
    broker = _BaseBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    from app.engine.copy_trading import _persist_copy_follower_broker_ref

    async def _db_commit_fails(*, claim_id, broker_order_id, **kwargs):
        await _persist_copy_follower_broker_ref(claim_id, broker_order_id)
        raise RuntimeError("sqlite3.OperationalError: database is locked")

    monkeypatch.setattr(
        "app.engine.copy_trading._persist_copy_follower_live_fill",
        _db_commit_fails,
    )

    result = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=seeded["master_id"],
    )
    assert result["successful_copies"] == 0
    assert len(broker.dispatches) == 1

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "PENDING"
    assert order.broker_order_id is not None, (
        "the broker reference must survive a DB failure after acceptance"
    )
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0

    broker.order_status_responses.append(
        {"status": "FILLED", "average_price": 2500.0}
    )
    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 1, summary
    order = await _fetch_order(seeded["follower_id"])
    assert order.status == "FILLED"
    assert len(broker.dispatches) == 1, "DB failure must never re-dispatch"


# ── F. Missing/ambiguous broker reference ─────────────────────────────────────


@pytest.mark.asyncio
async def test_f_ambiguous_broker_response_stays_pending_no_fill(monkeypatch):
    """A broker that accepts the order but returns NO reference must NOT get a
    fabricated fill or a synthetic reference; the claim stays PENDING and is
    recoverable by Window-C reconciliation."""
    seeded = await _seed_live_follower()
    symbol = "COAL"
    quantity = 4
    broker = _AmbiguousBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    result = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=seeded["master_id"],
    )
    assert len(broker.dispatches) == 1
    assert result["successful_copies"] == 1

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None
    assert order.status == "PENDING", (
        "ambiguous broker response must never fabricate FILLED"
    )
    assert order.broker_order_id is None, (
        "ambiguous response must never fabricate a broker reference"
    )
    assert order.client_order_id and order.client_order_id.startswith("cpy-")
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0
    assert await _count_rows(PositionRecord, seeded["follower_id"]) == 0

    # Recovery: confirmed live exposure -> finalize (never a second dispatch).
    broker.positions.append(
        {
            "symbol": symbol,
            "quantity": quantity,
            "side": "LONG",
            "average_price": 2500.0,
        }
    )
    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 1, summary
    order = await _fetch_order(seeded["follower_id"])
    assert order.status == "FILLED"
    assert len(broker.dispatches) == 1, "ambiguity must never re-dispatch"


@pytest.mark.asyncio
async def test_f2_reference_but_no_price_is_recoverable(monkeypatch):
    """Reference known but fill state uncertain: persist the ref, stay PENDING,
    then finalize through the Window-B status read."""
    seeded = await _seed_live_follower()
    symbol = "ADANI"
    quantity = 3
    broker = _NoPriceBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    result = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=seeded["master_id"],
    )
    assert result["successful_copies"] == 1
    assert len(broker.dispatches) == 1

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None
    assert order.status == "PENDING"
    assert order.broker_order_id is not None, (
        "known broker ref must be persisted even when the fill price is absent"
    )
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0

    broker.order_status_responses.append(
        {"status": "FILLED", "average_price": 2500.0}
    )
    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 1, summary
    order = await _fetch_order(seeded["follower_id"])
    assert order.status == "FILLED"
# ── G. Successful broker fill ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_g_successful_fill_complete_records(monkeypatch):
    """Happy path: FILLED OrderRecord with idempotency key + broker ref +
    position_id, plus TradeRecord and OPEN PositionRecord via the durable
    claim's OrderRecord id as the trade linkage."""
    seeded = await _seed_live_follower()
    symbol = "RELIANCE"
    quantity = 20
    broker = _BaseBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    result = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=seeded["master_id"],
    )
    assert result["successful_copies"] == 1
    assert result["mirrored"] is True
    assert len(broker.dispatches) == 1

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None
    assert order.status == "FILLED"
    assert order.mode == "LIVE"
    assert order.client_order_id and order.client_order_id.startswith("cpy-")
    assert order.broker_order_id is not None
    assert order.filled_quantity == quantity
    assert order.filled_price == 2500.0

    position = await _fetch_position(seeded["follower_id"])
    assert position is not None
    assert position.status == "OPEN"
    assert position.side == "LONG"
    assert position.quantity == quantity
    assert position.broker_account_id is not None
    assert order.position_id == position.id, "OrderRecord must link to position"

    trade = await _fetch_trade(seeded["follower_id"])
    assert trade is not None
    assert trade.order_id == order.id, (
        "TradeRecord must link via the durable claim's OrderRecord id, never a "
        "synthetic CPY_ORD_* value"
    )
    assert trade.mode == "LIVE"
    assert trade.symbol == symbol


# ── H. Broker rejection ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_h_broker_rejection_cas_same_claim_no_duplicate_row(monkeypatch):
    """A broker rejection must CAS-reject the SAME durable claim (no second
    unkeyed REJECTED row) and never create a position or trade."""
    seeded = await _seed_live_follower()
    symbol = "TITAN"
    quantity = 11
    broker = _RejectingBroker()
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    result = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=seeded["master_id"],
    )
    assert result["successful_copies"] == 0
    assert len(broker.dispatches) == 1

    assert await _count_rows(OrderRecord, seeded["follower_id"]) == 1, (
        "rejection must not create a second REJECTED row"
    )
    order = await _fetch_order(seeded["follower_id"])
    assert order.status == "REJECTED"
    assert order.client_order_id and order.client_order_id.startswith("cpy-")
    assert order.error_message
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0
    assert await _count_rows(PositionRecord, seeded["follower_id"]) == 0

    # Retryable: the SAME claim can be reclaimed for a later genuine retry.
    result2 = await copy_trading_engine.mirror_trade(
        master_order=_master_dict(symbol=symbol, quantity=quantity),
        master_user_id=seeded["master_id"],
    )
    assert result2["successful_copies"] == 0
    assert len(broker.dispatches) == 2, "REJECTED claim is retryable"
    assert await _count_rows(OrderRecord, seeded["follower_id"]) == 1, (
        "retry re-claims the SAME durable row"
    )

# ── I. Window-C broker position exists ────────────────────────────────────────


async def _seed_stale_pending_claim(
    *,
    follower_user_id: str,
    broker_account_id: str,
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    master_id: str = "master-i",
    created_hours_ago: int = 3,
) -> str:
    """Insert a stale, keyed, LIVE PENDING OrderRecord directly.  This models a
    durable claim that was committed before a fatal crash: it carries NO broker
    reference (Window-C) and is already older than the reconciliation stale
    threshold, so it is eligible for read-only exposure recovery."""
    order_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(OrderRecord(
            id=order_id,
            user_id=follower_user_id,
            broker_account_id=broker_account_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type="MARKET",
            price=price,
            mode="LIVE",
            status="PENDING",
            client_order_id=_copy_client_order_id(
                master_id, follower_user_id, symbol, side, quantity,
            ),
            created_at=datetime.now(timezone.utc)
            - timedelta(hours=created_hours_ago),
        ))
        await db.commit()
    return order_id


@pytest.mark.asyncio
async def test_i_window_c_position_exists_canonical_finalize(monkeypatch):
    """When Window-C recovery finds a matching live position at the broker, the
    stale PENDING claim is canonically finalized FILLED + Trade + Position + the
    OrderRecord/Position link, exactly like the direct-entry fill path."""
    seeded = await _seed_live_follower()
    symbol = "WIPRO"
    quantity = 15
    broker = _BaseBroker()
    broker.positions.append({
        "symbol": symbol,
        "quantity": quantity,
        "side": "LONG",
        "average_price": 2180.0,
    })
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    await _seed_stale_pending_claim(
        follower_user_id=seeded["follower_id"],
        broker_account_id=seeded["broker_id"],
        symbol=symbol, side="BUY", quantity=quantity, price=2180.0,
    )

    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 1, summary

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "FILLED"
    assert order.filled_price == 2180.0
    assert order.filled_quantity == quantity
    assert order.client_order_id and order.client_order_id.startswith("cpy-")

    position = await _fetch_position(seeded["follower_id"])
    assert position is not None and position.status == "OPEN"
    assert position.side == "LONG"
    assert position.quantity == quantity
    assert order.position_id == position.id, "finalized claim links to position"

    trade = await _fetch_trade(seeded["follower_id"])
    assert trade is not None
    assert trade.order_id == order.id, "Trade links to the durable claim id"


# ── J. Window-C no broker exposure ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_j_window_c_no_exposure_stays_pending(monkeypatch):
    """Confirmed NO live exposure in Window-C must never fabricate CANCELLED or
    FILLED: the row stays PENDING and no trade/position is created."""
    seeded = await _seed_live_follower()
    symbol = "GODREJ"
    quantity = 9
    broker = _BaseBroker()  # empty positions -> no exposure
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    await _seed_stale_pending_claim(
        follower_user_id=seeded["follower_id"],
        broker_account_id=seeded["broker_id"],
        symbol=symbol, side="BUY", quantity=quantity, price=2450.0,
    )

    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 0, summary
    assert summary["cancelled"] == 0, "never fabricate a cancel from no exposure"

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "PENDING", summary
    assert order.broker_order_id is None
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0
    assert await _count_rows(PositionRecord, seeded["follower_id"]) == 0

# ── K. Malformed broker positions ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_k1_malformed_positions_non_list_safe(monkeypatch):
    """A broker returning a non-list payload from get_positions() must not
    fabricate a fill; the claim stays PENDING and recoverable."""
    seeded = await _seed_live_follower()
    symbol = "DABUR"
    quantity = 6
    broker = _MalformedPositionsBroker(payload="not-a-list")
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    await _seed_stale_pending_claim(
        follower_user_id=seeded["follower_id"],
        broker_account_id=seeded["broker_id"],
        symbol=symbol, side="BUY", quantity=quantity, price=520.0,
    )

    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 0, summary

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "PENDING", summary
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0
    assert await _count_rows(PositionRecord, seeded["follower_id"]) == 0


@pytest.mark.asyncio
async def test_k2_malformed_position_dicts_safe(monkeypatch):
    """Position dicts with unusable/partial fields (missing quantity/price, wrong
    types) are never matched; the claim stays PENDING with no fabrication."""
    seeded = await _seed_live_follower()
    symbol = "MARUTI"
    quantity = 4
    broker = _MalformedPositionsBroker(payload=[
        {"symbol": symbol, "side": "LONG", "quantity": "??", "average_price": 9000.0},  # bad qty
        {"symbol": symbol, "side": "SHORT", "quantity": 100},  # wrong side + no price
        {"symbol": "OTHER", "side": "LONG", "quantity": 100, "average_price": 1.0},  # wrong symbol
        "not-a-dict",  # non-dict entry
    ])
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    await _seed_stale_pending_claim(
        follower_user_id=seeded["follower_id"],
        broker_account_id=seeded["broker_id"],
        symbol=symbol, side="BUY", quantity=quantity, price=9000.0,
    )

    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 0, summary

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "PENDING", summary
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0
    assert await _count_rows(PositionRecord, seeded["follower_id"]) == 0


# ── L. Tenant / account mismatch ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l_tenant_account_mismatch_no_mutation_no_cross_user_access(
    monkeypatch,
):
    """A stale claim whose broker_account is owned by a DIFFERENT user must never
    be reconciled through it: no mutation, no cross-user read, no fabrication."""
    # The follower's subscription references a broker that belongs to ANOTHER user.
    seeded = await _seed_live_follower(
        broker_owner_user_id="some-other-tenant-id",
    )
    assert seeded["broker_id"]  # broker exists but is NOT owned by the follower
    symbol = "AMZN"
    quantity = 5
    broker = _BaseBroker()
    broker.positions.append({
        "symbol": symbol, "quantity": quantity, "side": "LONG",
        "average_price": 4100.0,
    })
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    await _seed_stale_pending_claim(
        follower_user_id=seeded["follower_id"],
        broker_account_id=seeded["broker_id"],  # owned by another user
        symbol=symbol, side="BUY", quantity=quantity, price=4100.0,
    )

    summary = await _reconcile_once(now=_stale_now())
    # Not filled, not rejected/cancelled: account ownership check rejects it.
    assert summary["filled"] == 0, summary

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "PENDING", summary
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0
    assert await _count_rows(PositionRecord, seeded["follower_id"]) == 0


# ── M. Partial fill ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_m_partial_fill_no_over_booking(monkeypatch):
    """A live position whose quantity is LESS than the target must be treated as
    an uncertain (partial/unconfirmed) state: the claim stays PENDING and the
    engine never books a larger position than the broker confirms."""
    seeded = await _seed_live_follower()
    symbol = "BAJAJFINSV"
    target_qty = 20
    partial_qty = 5  # broker exposure < target -> can't confirm full fill
    broker = _BaseBroker()
    broker.positions.append({
        "symbol": symbol,
        "quantity": partial_qty,
        "side": "LONG",
        "average_price": 6400.0,
    })
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    await _seed_stale_pending_claim(
        follower_user_id=seeded["follower_id"],
        broker_account_id=seeded["broker_id"],
        symbol=symbol, side="BUY", quantity=target_qty, price=6400.0,
    )

    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 0, summary

    order = await _fetch_order(seeded["follower_id"])
    assert order is not None and order.status == "PENDING", summary
    assert await _count_rows(PositionRecord, seeded["follower_id"]) == 0, (
        "a partial exposure must never be booked as a full position"
    )
    assert await _count_rows(TradeRecord, seeded["follower_id"]) == 0

# ── N. Recovered entry then close ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n_recovered_entry_then_close_exactly_once(monkeypatch):
    """A Window-C-recovered entry (Position OPEN) can then be closed through
    mirror_close_position exactly once: one FILLED SELL close order, position
    CLOSED with realized PnL, and a second close never double-closes."""
    seeded = await _seed_live_follower()
    symbol = "TCS"
    quantity = 6
    broker = _BaseBroker()
    # NOTE: _BaseBroker.place_order always fills at 2500.0, so seed the entry
    # below that to guarantee a positive realized PnL on the close.
    broker.positions.append({
        "symbol": symbol, "quantity": quantity, "side": "LONG",
        "average_price": 2100.0,
    })
    _set_live_mode_and_broker(monkeypatch, broker, patch_recon=True)

    # Recover the entry from Window-C exposure.
    await _seed_stale_pending_claim(
        follower_user_id=seeded["follower_id"],
        broker_account_id=seeded["broker_id"],
        symbol=symbol, side="BUY", quantity=quantity, price=2100.0,
    )
    summary = await _reconcile_once(now=_stale_now())
    assert summary["filled"] == 1, summary

    position = await _fetch_position(seeded["follower_id"])
    assert position is not None and position.status == "OPEN"

    # First close -> exactly one FILLED SELL close order, position CLOSED.
    close1 = await copy_trading_engine.mirror_close_position(
        symbol=symbol,
        master_user_id=seeded["master_id"],
        exit_price=3500.0,
    )
    assert close1["closed_count"] == 1, close1

    position = await _fetch_position(seeded["follower_id"])
    assert position.status == "CLOSED"
    assert position.realized_pnl is not None and position.realized_pnl > 0, (
        "long closed above entry must book positive realized PnL"
    )

    async with SessionLocal() as db:
        sell_filled = (await db.execute(
            select(func.count()).select_from(OrderRecord).where(
                OrderRecord.user_id == seeded["follower_id"],
                OrderRecord.side == "SELL",
                OrderRecord.status == "FILLED",
            )
        )).scalar_one()
    assert sell_filled == 1, "recovered entry must close with exactly one SELL"

    # Second close over the same (already closed) position -> nothing happens.
    close2 = await copy_trading_engine.mirror_close_position(
        symbol=symbol,
        master_user_id=seeded["master_id"],
        exit_price=3550.0,
    )
    assert close2["closed_count"] == 0, close2

    async with SessionLocal() as db:
        sell_filled_after = (await db.execute(
            select(func.count()).select_from(OrderRecord).where(
                OrderRecord.user_id == seeded["follower_id"],
                OrderRecord.side == "SELL",
                OrderRecord.status == "FILLED",
            )
        )).scalar_one()
    assert sell_filled_after == 1, "a closed position must never close twice"
