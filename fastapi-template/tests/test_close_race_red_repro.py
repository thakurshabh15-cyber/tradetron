"""RED reproduction of the P1 concurrent-close race — PRE-FIX close logic.

This test does NOT touch the production ``close_position`` endpoint.  It
instead re-implements the OLD (pre-CAS) close body *exactly* as it existed
before the atomic CAS fix, and drives two concurrent close attempts against the
same OPEN LIVE position using the real app DB session/engine (same config
conftest uses).

Deterministic barrier: the fake broker's ``place_order`` blocks on an
``asyncio.Event`` until BOTH concurrent requests have entered the broker
dispatch.  With the OLD read-then-dispatch-then-commit logic (no
``UPDATE ... WHERE status='OPEN'`` gate) BOTH callers observe ``OPEN`` and BOTH
dispatch a real broker close — the exact race identified in the audit.

This test is expected to RED (fail) for the reason that matters: it proves two
concurrent callers can both dispatch, book duplicate trades and double-mutate
realized PnL.  It exists as regression evidence; the production endpoint since
carries a CAS gate that makes it impossible.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.models.trading import PositionRecord, TradeRecord
from app.models.user import UserRecord


@pytest.fixture(autouse=True)
async def _reset_db_and_simulated_mode():
    await init_db()
    settings.broker_mode = "simulated"
    yield
    settings.broker_mode = "simulated"


async def _seed(position_qty: float = 10.0, entry: float = 2500.0) -> dict:
    uid = str(uuid.uuid4())
    pos_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(UserRecord(
            id=uid,
            email=f"red_{uid[:8]}@tradetron.io",
            hashed_password=hash_password("SecurePassword123!"),
            full_name="RED Race Tester",
            role="trader",
            is_active=True,
            is_verified=True,
            paper_balance=1_000_000.0,
        ))
        await db.flush()
        db.add(PositionRecord(
            id=pos_id,
            user_id=uid,
            symbol="RELIANCE",
            side="LONG",
            quantity=position_qty,
            entry_price=entry,
            current_price=2520.0,
            realized_pnl=0.0,
            unrealized_pnl=200.0,
            mode="LIVE",
            status="OPEN",
        ))
        await db.commit()
    return {"user_id": uid, "position_id": pos_id}


# --------------------------------------------------------------------------
# OLD (pre-CAS) close body — faithfully reproduced from the original
# close_position() BEFORE the atomic CAS gate was added.
# --------------------------------------------------------------------------


class _OldClose:
    """State shared by both concurrent tasks (dispatch counter + barrier)."""

    def __init__(self):
        self.dispatch_count = 0
        self.both_at_broker = asyncio.Event()
        self.release_broker = asyncio.Event()

    async def broker_place_order(self) -> dict:
        # Deterministic barrier: both requests must reach the broker dispatch
        # (i.e. both already saw status == OPEN) before either may finish.
        self.dispatch_count += 1
        if self.dispatch_count >= 2:
            self.both_at_broker.set()
        await asyncio.wait_for(self.release_broker.wait(), timeout=15)
        # Broker fills at a higher price -> positive realized PnL.
        return {"broker_order_id": "RED-1", "status": "FILLED", "filled_price": 2550.0}


async def _old_close(position_id: str, state: _OldClose, user_id: str) -> dict:
    """The original body: read, guard, dispatch, mark CLOSED, book, commit."""
    async with SessionLocal() as db:
        # 1. READ (no CAS) — the vulnerability: both callers see OPEN
        pos = await db.get(PositionRecord, position_id)
        if pos is None or pos.status != "OPEN":
            return {"dispatched": False, "status_code": 404}

        is_long = pos.side in ("LONG", "BUY")

        # 2. BROKER DISPATCH — barrier forces both here before release
        broker_resp = await state.broker_place_order()
        exit_price = float(broker_resp.get("filled_price") or pos.current_price)

        # 3. Book realized PnL + trade (DUPLICATE on the second caller)
        delta = (exit_price - pos.entry_price) if is_long else (pos.entry_price - exit_price)
        realized_pnl = round(delta * pos.quantity, 2)

        pos.status = "CLOSED"
        pos.closed_at = datetime.now(timezone.utc)
        pos.current_price = exit_price
        pos.realized_pnl = realized_pnl
        pos.unrealized_pnl = 0.0

        trade = TradeRecord(
            id=str(uuid.uuid4()),
            order_id=f"EXIT_{int(datetime.now(timezone.utc).timestamp())}",
            strategy_name="Manual Position Exit",
            symbol=pos.symbol,
            side="SELL" if is_long else "BUY",
            quantity=pos.quantity,
            price=exit_price,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl=realized_pnl,
            pnl_pct=round((delta / pos.entry_price) * 100, 2) if pos.entry_price else 0.0,
            exit_reason="MANUAL_CLOSE",
            mode=pos.mode,
            user_id=pos.user_id,
        )
        db.add(trade)

        # 4. COMMIT
        await db.commit()
        return {"dispatched": True, "status_code": 200, "pnl": realized_pnl}


@pytest.mark.asyncio
async def test_red_pre_fix_logic_double_dispatch():
    """
    RED: with the OLD (pre-CAS) logic, two concurrent closes of the same OPEN
    LIVE position BOTH dispatch a broker order and BOTH book PnL/trade.
    """
    seeded = await _seed(position_qty=10.0, entry=2500.0)
    state = _OldClose()

    async def _attempt():
        return await _old_close(seeded["position_id"], state, seeded["user_id"])

    t1 = asyncio.create_task(_attempt())
    t2 = asyncio.create_task(_attempt())

    # Let the barrier fill if BOTH truly reach the broker (proving both read OPEN).
    try:
        await asyncio.wait_for(state.both_at_broker.wait(), timeout=8)
    except asyncio.TimeoutError:
        pass
    state.release_broker.set()

    r1, r2 = await asyncio.gather(t1, t2)

    redispatches = sum(1 for r in (r1, r2) if r["dispatched"])

    # Inspect final DB state
    async with SessionLocal() as db:
        pos = await db.get(PositionRecord, seeded["position_id"])
        trade_count = (
            await db.execute(
                select(func.count()).select_from(TradeRecord).where(
                    TradeRecord.user_id == seeded["user_id"]
                )
            )
        ).scalar_one()
        final_pnl = pos.realized_pnl if pos else None
        final_status = pos.status if pos else None

    # ── THE AUDIT FINDING, QUANTIFIED ─────────────────────────────────────
    print("\n[RED] concurrent manual close — OLD (pre-CAS) logic:")
    print(f"    broker place_order() calls  : {state.dispatch_count}")
    print(f"    dispatch attempts           : {redispatches}")
    print(f"    trades booked               : {trade_count}")
    print(f"    final position status       : {final_status}")
    print(f"    final realized pnl          : {final_pnl}")
    print(f"    request A result            : {r1}")
    print(f"    request B result            : {r2}")

    # The vulnerability: BOTH callers dispatched and BOTH booked.
    assert state.dispatch_count == 2, (
        "expected BOTH callers to dispatch (double-close) under pre-fix logic; "
        f"got {state.dispatch_count}"
    )
    assert trade_count == 2, (
        "expected duplicate TradeRecords under pre-fix logic; "
        f"got {trade_count}"
    )

