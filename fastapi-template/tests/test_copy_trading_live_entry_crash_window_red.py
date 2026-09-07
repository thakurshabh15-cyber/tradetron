"""RED reproduction — copy-trading LIVE follower entry has NO durable pre-dispatch claim.

Invariant under test (the pattern every other LIVE entry path satisfies —
keyed manual/DMA entry and the autonomous strategy engine):
    BEFORE any broker dispatch for a LIVE follower copy-trade, a PENDING
    OrderRecord with a client_order_id MUST be durably committed, and the
    broker's returned reference MUST be persisted in its own commit, so that a
    process crash between broker acceptance and local finalization leaves a
    recoverable PENDING row the reconciliation engine can read back.

Current behaviour (audited at HEAD f97379b0):
    ``_dispatch_live_follower_order`` places the real broker order and only
    THEN (in the caller ``_execute_single_follower_order``) inserts
    OrderRecord + TradeRecord + PositionRecord and commits them all in ONE
    transaction.  No client_order_id is ever assigned, no PENDING claim is
    committed, and no broker-reference commit is issued.

Consequence:
    A crash after ``place_order`` accepts the REAL order but before that
    combined commit leaves confirmed live broker exposure with ZERO local DB
    rows and NO key — the broker postback reconciler cannot find a row
    (matches by broker_order_id) and the order-reconciliation engine cannot
    scan it (its selector requires status=PENDING AND client_order_id NOT NULL).

This file reproduces the crash window deterministically with a recording fake
broker and a simulated process death right after broker acceptance.  It is
RED on the current code: the invariant assertions fail.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.brokers import BrokerModeBlockedError  # noqa: F401  (documented guard parity)
from app.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.engine.copy_trading import copy_trading_engine
from app.models.broker_account import BrokerAccountRecord
from app.models.copy_trading import CopyFollowerRecord, CopyGroupRecord
@pytest.fixture(autouse=True)
async def _schema_and_simulated_mode():
    await init_db()
    settings.broker_mode = "simulated"
    yield
    settings.broker_mode = "simulated"


class _SimulatedProcessCrash(RuntimeError):
    """Models the process dying after broker acceptance, before local commit."""


class _RecordingBroker:
    """Fake broker: records dispatches and checks whether a durable keyed
    PENDING claim was committed BEFORE the order reached it."""

    def __init__(self, follower_user_id: str, symbol: str):
        self.dispatches = 0
        self.order_ids: list[str] = []
        self.claim_committed_before_dispatch = False
        self._follower_user_id = follower_user_id
        self._symbol = symbol

    async def place_order(self, req):  # noqa: ANN001 - broker protocol object
        # ── Invariant probe (read-only, own session) ──────────────────────
        # A durable PENDING order carrying a client_order_id for this follower
        # + symbol must already be committed if the entry path is hardened the
        # same way the manual/DMA keyed and strategy paths are.
        async with SessionLocal() as probe_db:
            count = await probe_db.scalar(
                select(func.count())
                .select_from(OrderRecord)
                .where(
                    OrderRecord.user_id == self._follower_user_id,
                    OrderRecord.symbol == self._symbol,
                    OrderRecord.status == "PENDING",
                    OrderRecord.client_order_id.is_not(None),
                )
            )
        self.claim_committed_before_dispatch = bool(count)

        # ── Real dispatch simulation ───────────────────────────────────────
        self.dispatches += 1
        oid = f"ZMB_{uuid.uuid4().hex[:10]}"
        self.order_ids.append(oid)
        return {"order_id": oid, "filled_price": 2500.0, "status": "FILLED"}

    async def get_positions(self):
        return []

    async def get_margins(self):
        return {"available_cash": 500000.0, "utilized_margin": 0.0,
                "total_collateral": 500000.0}
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord


async def _seed_live_follower() -> tuple[str, str, str]:
    """Seed master, follower, follower broker, LIVE group + LIVE follower."""
    master_id = str(uuid.uuid4())
    follower_id = str(uuid.uuid4())
    broker_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        for uid, email in (
            (master_id, f"cwe_master_{master_id[:8]}@tradetron.io"),
            (follower_id, f"cwe_follower_{follower_id[:8]}@tradetron.io"),
        ):
            db.add(UserRecord(
                id=uid, email=email,
                hashed_password=hash_password("SecurePassword123!"),
                full_name="Crash Window Tester",
                role="trader", is_active=True, is_verified=True,
                paper_balance=1_000_000.0,
            ))
        await db.flush()
        db.add(BrokerAccountRecord(
            id=broker_id, user_id=follower_id, broker_name="ZERODHA",
            account_name="Follower Live Acct", status="CONNECTED",
            is_active=True,
            token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            client_id="CLIENT_01",
            api_key_encrypted="mock_api_key_encrypted",
            api_secret_encrypted="mock_api_secret_encrypted",
            access_token_encrypted=f"mock_cwe_token_{follower_id[:8]}",
        ))
        await db.flush()
        group = CopyGroupRecord(master_user_id=master_id, name="Crash Window Group")
        db.add(group)
        await db.flush()
        db.add(CopyFollowerRecord(
            group_id=group.id, follower_user_id=follower_id,
            broker_account_id=broker_id, multiplier=1.0,
            status="ACTIVE", mode="LIVE",
        ))
        await db.commit()
    return master_id, follower_id, broker_id


async def _count_rows(model, user_id: str) -> int:
    async with SessionLocal() as db:
        return (await db.execute(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )).scalar_one()


async def test_live_follower_entry_crash_window_is_recoverable(monkeypatch):
    """THE crash-window invariant for the copy-trading LIVE follower entry.

    A confirmed broker dispatch must be backed by a durable keyed PENDING claim
    committed BEFORE dispatch.  Today the fan-out dispatches first and commits
    afterwards — RED.
    """
    master_id, follower_id, broker_id = await _seed_live_follower()
    symbol = "RELIANCE"
    broker = _RecordingBroker(follower_id, symbol)

    settings.broker_mode = "live"
    monkeypatch.setattr(
        "app.engine.copy_trading.get_broker_adapter",
        lambda broker_rec: broker,
    )

    # Simulate the process dying immediately AFTER the broker accepted the
    # real order and returned its reference, but BEFORE the caller's combined
    # FILLED/OPEN/TRADE commit.  The wrapper raises at exactly that point.
    original_dispatch = copy_trading_engine._dispatch_live_follower_order

    async def _crash_after_broker_acceptance(
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
            raise _SimulatedProcessCrash(
                "Simulated process death after broker acceptance, "
                "before local FILLED/OPEN commit"
            )
        return outcome

    monkeypatch.setattr(
        copy_trading_engine, "_dispatch_live_follower_order",
        _crash_after_broker_acceptance,
    )
    master_order = {
        "symbol": symbol, "side": "BUY", "quantity": 10,
        "order_type": "MARKET", "price": 2500.0,
        "filled_price": 2500.0, "mode": "LIVE",
    }
    result = await copy_trading_engine.mirror_trade(
        master_order=master_order, master_user_id=master_id,
    )
    # ── Observable state after the simulated crash ────────────────────────
    assert result["successful_copies"] == 0
    assert result["failed_copies"] == 1
    assert broker.dispatches == 1, "broker must have accepted the LIVE order"
    # A durable PENDING claim with a client_order_id MUST survive the crash
    # (the invariant that makes the copy-trading LIVE path recoverable).  The
    # broker reference is NOT yet persisted (crash lands before the reference
    # commit), so reconciliation resolves this row via Window-C.
    assert await _count_rows(OrderRecord, follower_id) == 1, (
        "A durable PENDING claim must survive the simulated crash so "
        "reconciliation can read it back"
    )
    assert await _count_rows(TradeRecord, follower_id) == 0
    assert await _count_rows(PositionRecord, follower_id) == 0

    # ── Verify the surviving claim is keyed + PENDING ────────────────────
    async with SessionLocal() as db:
        surviving = (await db.execute(
            select(OrderRecord).where(
                OrderRecord.user_id == follower_id,
                OrderRecord.status == "PENDING",
            )
        )).scalars().first()
    assert surviving is not None, "surviving claim must be PENDING"
    assert surviving.client_order_id is not None, (
        "surviving claim must carry the cpy-* idempotency key"
    )
    assert surviving.client_order_id.startswith("cpy-")
    assert surviving.mode == "LIVE"
    assert surviving.broker_order_id is None, (
        "crash lands before the broker-reference commit: reference not yet "
        "persisted (Window-C recovery)"
    )

    # ── THE INVARIANT (fails on current code → RED) ───────────────────────
    assert broker.claim_committed_before_dispatch, (
        "Copy-trading LIVE follower entry must commit a durable PENDING claim "
        "with client_order_id BEFORE broker dispatch (mirroring keyed manual/DMA "
        "entry and the strategy engine).  Current code dispatches first and "
        "commits afterwards in ONE transaction, so a crash between broker "
        "acceptance and the combined commit leaves unrecoverable live exposure: "
        "a real-money order exists at the broker with no local record and no key."
    )


async def test_live_follower_filled_order_has_no_idempotency_key(monkeypatch):
    """Even the happy path books the follower fill WITHOUT client_order_id,
    so the row can never participate in retry/idempotency semantics."""
    master_id, follower_id, broker_id = await _seed_live_follower()
    symbol = "INFY"
    broker = _RecordingBroker(follower_id, symbol)
    settings.broker_mode = "live"
    monkeypatch.setattr(
        "app.engine.copy_trading.get_broker_adapter",
        lambda broker_rec: broker,
    )
    master_order = {
        "symbol": symbol, "side": "BUY", "quantity": 5,
        "order_type": "MARKET", "price": 1600.0,
        "filled_price": 1600.0, "mode": "LIVE",
    }
    result = await copy_trading_engine.mirror_trade(
        master_order=master_order, master_user_id=master_id,
    )
    assert result["successful_copies"] == 1
    async with SessionLocal() as db:
        order = (await db.execute(
            select(OrderRecord).where(OrderRecord.user_id == follower_id)
        )).scalars().first()
        position = (await db.execute(
            select(PositionRecord).where(PositionRecord.user_id == follower_id)
        )).scalars().first()
    assert order is not None
    assert order.client_order_id is not None, (
        "Every LIVE entry must carry a client_order_id (idempotency key) so "
        "the postback/reconciliation machinery can bind and retry it.  Copy "
        "follower entries book FILLED with client_order_id=NULL — RED."
    )
    assert position is not None and position.status == "OPEN"