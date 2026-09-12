"""Phase 15C — exchange-level protective orders (fail-closed LIVE behavior).

Invariants pinned here:

* NO FABRICATION — a LIVE position reaches PROTECTED only after every
  configured leg returned a genuine ``broker_protective_order_id``.
* Fail-closed — an adapter lacking native SL/TP support (or no CONNECTED
  broker) yields PROTECTION_FAILED with ``fail_closed=True`` and never creates
  a fabricated protective row.
* PAPER untouched — no protective rows are ever created for PAPER positions.
* Idempotent — one row per (position, leg); an unchanged retry never
  re-dispatches.  A moved level cancels + re-places ONLY the changed leg.
* Crash-hardened — a reference-less PENDING_PLACEMENT row older than the crash
  window is failed closed for MANUAL REVIEW (never re-poked); a fresh
  in-flight row is never touched.
* Broker truth — a broker-reported FILLED advances the row to COMPLETE and the
  position honestly to STOP_TRIGGERED / TARGET_TRIGGERED.
* Orphans resolved locally only — never auto-closed at the broker.
* Tenant isolation — user B can neither read nor mutate user A's protection.

SAFETY: no real broker/payment/network call.  Every adapter is a deterministic
fake (or the real simulated adapter whose capability probe is False).
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.brokers.base import (
    PROTECTIVE_ORDER_TYPE_SL_LIMIT,
    PROTECTIVE_ORDER_TYPE_SL_MARKET,
    PROTECTIVE_ORDER_TYPE_TP_LIMIT,
    BrokerProtectionCapability,
)
from app.config import settings
from app.db.session import SessionLocal, init_db
from app.engine.protective_orders import (
    CRASH_WINDOW_STALE_SECONDS,
    LEG_STOP_LOSS,
    LEG_TAKE_PROFIT,
    PROTECTION_STATE_FAILED,
    PROTECTION_STATE_PAPER,
    PROTECTION_STATE_PENDING,
    PROTECTION_STATE_PROTECTED,
    PROTECTION_STATE_STOP_TRIGGERED,
    PROTECTION_STATE_TARGET_TRIGGERED,
    PROTECTION_STATE_UNPROTECTED,
    ProtectiveOrderManager,
)
from app.models.broker_account import BrokerAccountRecord
from app.models.protective_order import ProtectiveOrderRecord
from app.models.trading import PositionRecord
from app.schemas.trading import OrderRequest


asyncio.run(init_db())

_ENGINE = ProtectiveOrderManager()


class _FakeProtectiveBroker:
    """Deterministic fake adapter with DECLARED native SL/TP support."""

    def __init__(self) -> None:
        self.placed: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.status_map: dict[str, str] = {}
        self.next_ref = 1

    def supports_native_protection(self) -> BrokerProtectionCapability:
        return BrokerProtectionCapability(
            adapter="FakeProtectiveBroker",
            native_sl=True,
            native_tp=True,
            bracket=True,
            replace=True,
            order_types=(
                PROTECTIVE_ORDER_TYPE_SL_MARKET,
                PROTECTIVE_ORDER_TYPE_SL_LIMIT,
                PROTECTIVE_ORDER_TYPE_TP_LIMIT,
            ),
            tested=True,
            reason="deterministic test adapter",
        )

    async def place_order(self, order: OrderRequest) -> dict:
        self.placed.append(order)
        ref = f"FAKE-{self.next_ref:04d}"
        self.next_ref += 1
        self.status_map[ref] = "OPEN"
        return {"broker_protective_order_id": ref, "status": "OPEN"}

    async def cancel_order(self, broker_order_id: str) -> dict:
        self.cancelled.append(broker_order_id)
        self.status_map[broker_order_id] = "CANCELLED"
        return {"status": "CANCELLED", "broker_order_id": broker_order_id}

    async def get_order_status(self, broker_order_id: str) -> dict:
        return {
            "status": self.status_map.get(broker_order_id, "UNKNOWN"),
            "broker_order_id": broker_order_id,
        }

    async def modify_order(self, broker_order_id, quantity=None, price=None) -> dict:
        return {"status": "MODIFIED"}

    async def connect(self) -> None:
        return None

    async def get_positions(self) -> list:
        return []

    async def get_margins(self) -> dict:
        return {}

    async def get_holdings(self) -> list:
        return []


# -- fake-broker-end --

async def _seed_user_and_broker(broker_name: str = "ZERODHA") -> tuple[str, str]:
    uid = str(_uuid.uuid4())
    async with SessionLocal() as db:
        acc = BrokerAccountRecord(
            user_id=uid, broker_name=broker_name,
            status="CONNECTED", is_active=True,
        )
        acc.set_credentials("k", "s", "t")
        db.add(acc)
        await db.flush()
        await db.commit()
        return uid, acc.id


async def _seed_position(
    user_id: str,
    broker_account_id: str,
    *,
    mode: str = "LIVE",
    sl: float | None = 100.0,
    tp: float | None = 200.0,
    status: str = "OPEN",
    protection_state: str = PROTECTION_STATE_UNPROTECTED,
) -> str:
    async with SessionLocal() as db:
        pos = PositionRecord(
            id=str(_uuid.uuid4()), user_id=user_id,
            broker_account_id=broker_account_id,
            symbol="RELIANCE", side="LONG", quantity=10,
            entry_price=150.0, current_price=150.0,
            stop_loss_price=sl, take_profit_price=tp,
            mode=mode, status=status, protection_state=protection_state,
        )
        db.add(pos)
        await db.commit()
        return pos.id


async def _seed_protective_row(
    position_id: str,
    user_id: str,
    broker_account_id: str,
    *,
    leg: str = LEG_STOP_LOSS,
    status: str = "PENDING_PLACEMENT",
    broker_ref: str | None = None,
    created_seconds_ago: float = 0.0,
    trigger_price: float = 100.0,
) -> str:
    async with SessionLocal() as db:
        row = ProtectiveOrderRecord(
            id=str(_uuid.uuid4()),
            position_id=position_id,
            user_id=user_id,
            broker_account_id=broker_account_id,
            leg=leg,
            side="SELL",
            symbol="RELIANCE",
            quantity=10,
            trigger_price=trigger_price,
            limit_price=trigger_price,
            order_type=PROTECTIVE_ORDER_TYPE_SL_LIMIT,
            broker_protective_order_id=broker_ref,
            status=status,
            created_at=datetime.now(timezone.utc)
            - timedelta(seconds=created_seconds_ago),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)
        await db.commit()
        return row.id


async def _position_state(position_id: str) -> str:
    async with SessionLocal() as db:
        pos = await db.get(PositionRecord, position_id)
        return pos.protection_state if pos is not None else "MISSING"


async def _protective_rows(
    position_id: str,
) -> list[ProtectiveOrderRecord]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ProtectiveOrderRecord).where(
                    ProtectiveOrderRecord.position_id == position_id
                )
            )
        ).scalars().all()
        return list(rows)


@pytest.fixture(autouse=True)
def _test_broker_mode():
    settings.broker_mode = "simulated"
    yield


# -- test-seed-imports --


# ── LIVE placement ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_ensure_places_both_legs_and_marks_protected(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(user_id, acc_id)

    outcome = await _ENGINE.ensure_position_protection(pos_id)

    assert outcome.ok is True
    assert outcome.state == PROTECTION_STATE_PROTECTED
    assert (await _position_state(pos_id)) == PROTECTION_STATE_PROTECTED
    rows = await _protective_rows(pos_id)
    assert {r.leg for r in rows} == {LEG_STOP_LOSS, LEG_TAKE_PROFIT}
    assert all(r.status == "PLACED" for r in rows)
    assert all(r.broker_protective_order_id for r in rows)
    assert len(broker.placed) == 2  # genuine broker evidence, both legs


@pytest.mark.asyncio
async def test_live_ensure_unsupported_fails_closed_without_fabrication(monkeypatch):
    from app.brokers.base import BrokerProtectionCapability as _Cap

    class _Unsupported:
        def supports_native_protection(self):
            return _Cap(
                adapter="UnsupportedBroker",
                native_sl=False,
                native_tp=False,
                reason="adapter has no exchange-side representation",
            )

    broker = _Unsupported()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(user_id, acc_id)

    outcome = await _ENGINE.ensure_position_protection(pos_id)

    assert outcome.ok is False
    assert outcome.fail_closed is True
    assert outcome.state == PROTECTION_STATE_FAILED
    assert (await _position_state(pos_id)) == PROTECTION_STATE_FAILED
    # No fabricated protective rows.
    assert await _protective_rows(pos_id) == []


@pytest.mark.asyncio
async def test_live_without_sltp_is_honest_unprotected(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(user_id, acc_id, sl=None, tp=None)

    outcome = await _ENGINE.ensure_position_protection(pos_id)

    assert outcome.ok is True
    assert outcome.state == PROTECTION_STATE_UNPROTECTED
    assert await _protective_rows(pos_id) == []
    assert broker.placed == []


@pytest.mark.asyncio
async def test_paper_position_never_gets_protective_rows(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(user_id, acc_id, mode="PAPER")

    outcome = await _ENGINE.ensure_position_protection(pos_id)

    assert outcome.ok is True
    assert outcome.state == PROTECTION_STATE_PAPER
    assert await _protective_rows(pos_id) == []
    assert broker.placed == []


# ── idempotency / duplicates ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_is_idempotent_no_duplicate_no_redispatch(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(user_id, acc_id)

    first = await _ENGINE.ensure_position_protection(pos_id)
    second = await _ENGINE.ensure_position_protection(pos_id)

    assert first.ok and first.state == PROTECTION_STATE_PROTECTED
    rows = await _protective_rows(pos_id)
    assert len(rows) == 2  # exactly one row per leg
    assert len(broker.placed) == 2  # nothing re-dispatched on the retry
    assert second.ok and second.state == PROTECTION_STATE_PROTECTED

    # Unique-index backstop: a second live row for the same leg is rejected.
    row = rows[0]
    with pytest.raises(IntegrityError):
        async with SessionLocal() as db:
            db.add(
                ProtectiveOrderRecord(
                    id=str(_uuid.uuid4()),
                    position_id=pos_id,
                    user_id=user_id,
                    broker_account_id=acc_id,
                    leg=row.leg,
                    side="SELL",
                    symbol="RELIANCE",
                    quantity=10,
                    trigger_price=100.0,
                    order_type=PROTECTIVE_ORDER_TYPE_SL_LIMIT,
                    status="PLACED",
                )
            )
            await db.flush()


@pytest.mark.asyncio
async def test_level_change_replaces_only_changed_leg(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(user_id, acc_id, sl=100.0, tp=200.0)

    await _ENGINE.ensure_position_protection(pos_id)
    sl_row = (await _protective_rows(pos_id))[0]
    old_sl_ref = sl_row.broker_protective_order_id
    assert sl_row.leg == LEG_STOP_LOSS
    assert len(broker.placed) == 2

    outcome = await _ENGINE.replace_position_protection(
        pos_id, new_sl=90.0, new_tp=None
    )

    assert outcome.ok is True
    assert outcome.state == PROTECTION_STATE_PROTECTED
    assert old_sl_ref in broker.cancelled  # old SL cancelled at the broker
    rows = await _protective_rows(pos_id)
    assert len(rows) == 2
    assert len(broker.placed) == 3  # only SL re-placed; TP untouched
    sl_row_new = next(r for r in rows if r.leg == LEG_STOP_LOSS)
    assert sl_row_new.trigger_price == 90.0
    assert sl_row_new.status == "PLACED"

# -- tests-part2 --

# ── crash recovery ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_reference_less_pending_fails_closed_for_manual_review(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(
        user_id, acc_id, protection_state=PROTECTION_STATE_PENDING
    )
    await _seed_protective_row(
        pos_id, user_id, acc_id,
        broker_ref=None,
        created_seconds_ago=CRASH_WINDOW_STALE_SECONDS + 60,
    )

    report = await _ENGINE.reconcile_once()

    assert (await _position_state(pos_id)) == PROTECTION_STATE_FAILED
    rows = await _protective_rows(pos_id)
    assert rows[0].status == "FAILED"
    assert "manual review" in (rows[0].last_error or "").lower()
    assert broker.placed == []  # NEVER re-poked blind
    assert any(
        item["position_id"] == pos_id for item in report["failed_closed"]
    )
    assert pos_id not in report["recovered"]  # manual-review gated


@pytest.mark.asyncio
async def test_fresh_in_flight_pending_is_not_mutated(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(
        user_id, acc_id, protection_state=PROTECTION_STATE_PENDING
    )
    await _seed_protective_row(
        pos_id, user_id, acc_id,
        broker_ref=None,
        created_seconds_ago=5.0,
    )

    await _ENGINE.reconcile_once()

    rows = await _protective_rows(pos_id)
    assert rows[0].status == "PENDING_PLACEMENT"  # untouched in-flight claim
    assert broker.placed == []


@pytest.mark.asyncio
async def test_broker_reported_fill_advances_to_terminal_state(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(
        user_id, acc_id, protection_state=PROTECTION_STATE_PROTECTED
    )
    await _seed_protective_row(
        pos_id, user_id, acc_id,
        leg=LEG_STOP_LOSS,
        status="PLACED",
        broker_ref="FAKE-FILLED",
        created_seconds_ago=CRASH_WINDOW_STALE_SECONDS + 60,
    )
    broker.status_map["FAKE-FILLED"] = "FILLED"

    report = await _ENGINE.reconcile_once()

    rows = await _protective_rows(pos_id)
    assert rows[0].status == "COMPLETE"
    assert rows[0].broker_reported_status == "FILLED"
    assert (await _position_state(pos_id)) == PROTECTION_STATE_STOP_TRIGGERED
    assert any(
        item["position_id"] == pos_id
        and item["state"] == PROTECTION_STATE_STOP_TRIGGERED
        for item in report["triggered"]
    )


# ── tenant isolation / close / orphans ───────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_isolation_denies_cross_user_mutation(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_a, acc_a = await _seed_user_and_broker()
    user_b, _acc_b = await _seed_user_and_broker()
    pos_id = await _seed_position(user_a, acc_a)

    outcome = await _ENGINE.ensure_position_protection(
        pos_id, authorized_user_id=user_b
    )

    assert outcome.ok is False
    assert outcome.state != PROTECTION_STATE_PROTECTED
    assert await _protective_rows(pos_id) == []
    assert broker.placed == []


@pytest.mark.asyncio
async def test_cancel_position_protection_tears_down_live_legs(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(user_id, acc_id)

    await _ENGINE.ensure_position_protection(pos_id)
    refs = {r.broker_protective_order_id for r in await _protective_rows(pos_id)}

    outcome = await _ENGINE.cancel_position_protection(
        pos_id, reason="position closed"
    )

    assert outcome.ok is True
    assert broker.cancelled and set(broker.cancelled) == refs
    rows = await _protective_rows(pos_id)
    assert all(r.status == "CANCELLED" for r in rows)


@pytest.mark.asyncio
async def test_orphan_protective_row_resolved_locally_never_broker(monkeypatch):
    broker = _FakeProtectiveBroker()
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: broker)
    user_id, acc_id = await _seed_user_and_broker()
    pos_id = await _seed_position(
        user_id, acc_id, protection_state=PROTECTION_STATE_PROTECTED
    )
    await _seed_protective_row(
        pos_id, user_id, acc_id,
        status="PLACED",
        broker_ref="FAKE-ORPHAN",
        created_seconds_ago=CRASH_WINDOW_STALE_SECONDS + 60,
    )
    broker.status_map["FAKE-ORPHAN"] = "OPEN"

    # Delete the position row — the protective row becomes an orphan.
    async with SessionLocal() as db:
        pos = await db.get(PositionRecord, pos_id)
        await db.delete(pos)
        await db.commit()

    report = await _ENGINE.reconcile_once()

    rows = await _protective_rows(pos_id)
    assert rows[0].status == "RESOLVED"
    assert report["orphans_resolved"] >= 1
    assert broker.cancelled == []  # orphan resolved locally ONLY