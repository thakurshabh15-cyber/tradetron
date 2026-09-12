"""End-to-end certification tests for Protective Orders (Phase 15C).

Covers:
  A) PAPER DMA with SL/TP — no protective orders created, no crash
  B) LIVE PROTECTED full lifecycle: entry -> replace -> close
  C) LIVE fail-closed via SIMULATED broker (no exchange-side protection)
  D) LIVE broker dispatch unavailable -> 502, no ghost position
  E) LIVE protective placement rejected mid-arm -> 503 fail-closed
"""

import uuid as _uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import settings
from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.broker_account import BrokerAccountRecord
from app.models.protective_order import ProtectiveOrderRecord
from app.models.trading import OrderRecord, PositionRecord
from app.engine.protective_orders import (
    LEG_STOP_LOSS,
    LEG_TAKE_PROFIT,
    PROTECTION_STATE_FAILED,
    PROTECTION_STATE_PROTECTED,
    ROW_CANCELLED,
    ROW_FAILED,
)
from app.brokers.base import BrokerProtectionCapability
from app.brokers.simulated import SimulatedBroker
from app.schemas.trading import OrderRequest


# -- helpers ---------------------------------------------------------------


async def _register(client: AsyncClient, tag: str) -> tuple[str, str, dict[str, str]]:
    em = f"e2e_prot_{tag}_{_uuid.uuid4().hex[:8]}@tradetron.io"
    reg = await client.post(
        "/api/auth/register",
        json={"email": em, "password": "SecurePassword123!", "full_name": f"Test {tag}"},
    )
    assert reg.status_code == 201, reg.text
    return reg.json()["access_token"], reg.json()["user"]["id"], {"Authorization": f"Bearer {reg.json()['access_token']}"}


async def _add_broker(user_id: str, broker_name: str = "ZERODHA") -> str:
    async with SessionLocal() as db:
        acc = BrokerAccountRecord(user_id=user_id, broker_name=broker_name, status="CONNECTED", is_active=True)
        acc.set_credentials("k", "s", "t")
        db.add(acc)
        await db.commit()
        return acc.id


async def _protective_rows(position_id: str) -> list[ProtectiveOrderRecord]:
    async with SessionLocal() as db:
        return list((await db.execute(select(ProtectiveOrderRecord).where(ProtectiveOrderRecord.position_id == position_id))).scalars().all())


async def _get_position(pid: str) -> PositionRecord | None:
    async with SessionLocal() as db:
        return await db.get(PositionRecord, pid)


async def _user_positions(uid: str) -> list[PositionRecord]:
    async with SessionLocal() as db:
        return list((await db.execute(select(PositionRecord).where(PositionRecord.user_id == uid))).scalars().all())


async def _user_orders(uid: str) -> list[OrderRecord]:
    async with SessionLocal() as db:
        return list((await db.execute(select(OrderRecord).where(OrderRecord.user_id == uid))).scalars().all())


# -- fake brokers ----------------------------------------------------------


class _FakeProtectiveBroker:
    """Deterministic fake adapter with DECLARED native SL/TP support."""

    def __init__(self) -> None:
        self.placed: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.status_map: dict[str, str] = {}
        self.next_ref = 1

    def supports_native_protection(self) -> BrokerProtectionCapability:
        return BrokerProtectionCapability(
            adapter="FakeProtectiveBroker", native_sl=True, native_tp=True,
            bracket=True, replace=True, order_types=(), tested=True,
            reason="deterministic test adapter",
        )

    async def place_order(self, order: OrderRequest) -> dict:
        self.placed.append(order)
        ref = f"FAKE-{self.next_ref:04d}"; self.next_ref += 1
        self.status_map[ref] = "OPEN"
        return {"broker_protective_order_id": ref, "status": "OPEN"}

    async def cancel_order(self, broker_order_id: str) -> dict:
        self.cancelled.append(broker_order_id)
        self.status_map[broker_order_id] = "CANCELLED"
        return {"status": "CANCELLED", "broker_order_id": broker_order_id}

    async def get_order_status(self, broker_order_id: str) -> dict:
        return {"status": self.status_map.get(broker_order_id, "UNKNOWN"), "broker_order_id": broker_order_id}

    async def modify_order(self, broker_order_id, quantity=None, price=None) -> dict:
        return {"status": "MODIFIED"}


class _RejectingProtectiveBroker:
    """Fake that declares protection support but REJECTS every placement."""

    def __init__(self) -> None:
        self.placed: list = []
        self.cancelled: list[str] = []

    def supports_native_protection(self) -> BrokerProtectionCapability:
        return BrokerProtectionCapability(
            adapter="RejectingProtectiveBroker", native_sl=True, native_tp=True,
            bracket=False, replace=False, order_types=(), tested=True,
            reason="declares support but always rejects",
        )

    async def place_order(self, order: OrderRequest) -> dict:
        self.placed.append(order)
        raise RuntimeError("simulated broker rejection: protective leg denied")

    async def cancel_order(self, broker_order_id: str) -> dict:
        self.cancelled.append(broker_order_id)
        return {"status": "CANCELLED", "broker_order_id": broker_order_id}

    async def get_order_status(self, broker_order_id: str) -> dict:
        return {"status": "UNKNOWN", "broker_order_id": broker_order_id}


class _UnreachableBroker:
    async def place_order(self, order: OrderRequest) -> dict:
        raise RuntimeError("simulated broker outage")

    async def cancel_order(self, broker_order_id: str) -> dict:
        return {"status": "CANCELLED", "broker_order_id": broker_order_id}

# -- Test A: PAPER DMA with SL/TP -- no protective orders -------------------


@pytest.mark.asyncio
async def test_e2e_paper_dma_sltp_never_creates_protective_orders():
    """PAPER DMA with SL/TP must never create protective orders or rows."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, uid, hdr = await _register(client, "paper_sltp")

        res = await client.post(
            "/api/v1/orders/execute-dma",
            json={
                "symbol": "NIFTY", "side": "BUY", "lots": 1,
                "product": "MIS", "order_type": "MARKET",
                "stop_loss_pct": 0.5, "take_profit_pct": 1.0,
                "mode": "PAPER",
            },
            headers=hdr,
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["success"] is True
        assert body["mode"] == "PAPER"
        assert body["stop_loss_price"] is not None
        assert body["take_profit_price"] is not None
        pos_id = body["position_id"]

        pos = await _get_position(pos_id)
        assert pos is not None
        assert pos.status == "OPEN" and pos.mode == "PAPER"
        assert pos.protection_state in ("UNPROTECTED", "PAPER")
        assert await _protective_rows(pos_id) == []

        patch_res = await client.patch(
            f"/api/v1/orders/positions/{pos_id}/risk-targets",
            json={"stop_loss_price": round(body["executed_price"] * 0.97, 2)},
            headers=hdr,
        )
        assert patch_res.status_code == 200
        assert await _protective_rows(pos_id) == []

        close_res = await client.post(
            f"/api/trades/positions/{pos_id}/close", headers=hdr,
        )
        assert close_res.status_code == 200, close_res.text
        assert close_res.json()["status"] == "CLOSED"
        assert await _protective_rows(pos_id) == []
# -- Test B: LIVE PROTECTED lifecycle: entry -> replace -> close ---------------


@pytest.mark.asyncio
async def test_e2e_live_protection_lifecycle_protected_replace_cleanup(monkeypatch):
    """Full LIVE lifecycle: entry -> PROTECTED -> risk-target replace -> close."""
    await init_db()
    dispatch_calls: list[str] = []
    dispatch_broker = SimulatedBroker()
    protect_broker = _FakeProtectiveBroker()
    monkeypatch.setattr(
        "app.api.trades.get_broker_adapter",
        lambda acc: (dispatch_calls.append(acc.id), dispatch_broker)[1],
    )
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: protect_broker)
    settings.broker_mode = "live"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, uid, hdr = await _register(client, "live_prot")
        acc_id = await _add_broker(uid, broker_name="ZERODHA")

        # -- Entry --
        res = await client.post(
            "/api/v1/orders/execute-dma",
            json={
                "symbol": "NIFTY", "side": "BUY", "lots": 1,
                "product": "MIS", "order_type": "MARKET",
                "stop_loss_pct": 0.5, "take_profit_pct": 1.0,
                "mode": "LIVE", "broker_account_id": acc_id,
            },
            headers=hdr,
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["success"] is True
        assert body["broker_order_id"].startswith("SIM-")
        assert body["stop_loss_price"] is not None
        assert body["take_profit_price"] is not None
        pos_id = body["position_id"]

        pos = await _get_position(pos_id)
        assert pos is not None and pos.status == "OPEN" and pos.mode == "LIVE"
        assert pos.protection_state == PROTECTION_STATE_PROTECTED
        assert pos.protected_at is not None
        rows = await _protective_rows(pos_id)
        assert len(rows) == 2
        assert {r.leg for r in rows} == {LEG_STOP_LOSS, LEG_TAKE_PROFIT}
        for r in rows:
            assert r.status == "PLACED"
            assert r.broker_protective_order_id.startswith("FAKE-")
        assert len(protect_broker.placed) == 2
        assert protect_broker.cancelled == []

        sl_row = next(r for r in rows if r.leg == LEG_STOP_LOSS)
        tp_row = next(r for r in rows if r.leg == LEG_TAKE_PROFIT)
        old_tp_ref = tp_row.broker_protective_order_id

        # -- Risk-target PATCH: move TP --
        new_tp = round(body["executed_price"] * 1.02, 2)
        patch_res = await client.patch(
            f"/api/v1/orders/positions/{pos_id}/risk-targets",
            json={"take_profit_price": new_tp},
            headers=hdr,
        )
        assert patch_res.status_code == 200, patch_res.text
        assert protect_broker.cancelled == [old_tp_ref]
        assert len(protect_broker.placed) == 3
        rows_after = await _protective_rows(pos_id)
        tp_row_after = next(r for r in rows_after if r.leg == LEG_TAKE_PROFIT)
        sl_row_after = next(r for r in rows_after if r.leg == LEG_STOP_LOSS)
        assert tp_row_after.broker_protective_order_id != old_tp_ref
        assert sl_row_after.broker_protective_order_id == sl_row.broker_protective_order_id
        for r in rows_after:
            assert r.status == "PLACED"

        bad_patch = await client.patch(
            f"/api/v1/orders/positions/{pos_id}/risk-targets",
            json={"take_profit_price": round(body["executed_price"] * 0.99, 2)},
            headers=hdr,
        )
        assert bad_patch.status_code == 422
        assert len(protect_broker.placed) == 3
        assert len(protect_broker.cancelled) == 1

        # -- Close --
        close_res = await client.post(
            f"/api/trades/positions/{pos_id}/close", headers=hdr,
        )
        assert close_res.status_code == 200, close_res.text
        assert close_res.json()["status"] == "CLOSED"

        pos_closed = await _get_position(pos_id)
        assert pos_closed is not None
        assert pos_closed.status == "CLOSED"
        assert pos_closed.protection_state == "CLOSED"
        assert pos_closed.closed_at is not None
        for r in await _protective_rows(pos_id):
            assert r.status in (ROW_CANCELLED, ROW_FAILED)

        assert len(dispatch_calls) == 2  # entry + close
        assert len(protect_broker.placed) == 3
        assert len(protect_broker.cancelled) == 3  # old TP + SL + new TP


# -- Test C: LIVE fail-closed via SIMULATED broker ----------------------------


@pytest.mark.asyncio
async def test_e2e_live_fail_closed_simulated_broker():
    """LIVE entry via SIMULATED broker -> 503, position CLOSED + PROTECTION_FAILED."""
    await init_db()
    settings.broker_mode = "live"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, uid, hdr = await _register(client, "fail_closed_sim")
        acc_id = await _add_broker(uid, broker_name="SIMULATED")

        res = await client.post(
            "/api/v1/orders/execute-dma",
            json={
                "symbol": "NIFTY", "side": "BUY", "lots": 1,
                "product": "MIS", "order_type": "MARKET",
                "stop_loss_pct": 0.5, "take_profit_pct": 1.0,
                "mode": "LIVE", "broker_account_id": acc_id,
            },
            headers=hdr,
        )
        assert res.status_code == 503
        detail = res.json()["detail"]
        assert "FAIL-CLOSED" in detail
        assert "prevent UNPROTECTED inventory" in detail

        positions = await _user_positions(uid)
        assert len(positions) == 1
        pos = positions[0]
        assert pos.status == "CLOSED"
        assert pos.mode == "LIVE"
        assert pos.protection_state == PROTECTION_STATE_FAILED
        assert pos.protection_error is not None
        assert "FAIL-CLOSED entry" in pos.protection_error

        # SIMULATED never arms exchange-side protective orders
        assert await _protective_rows(pos.id) == []


# -- Test D: LIVE broker dispatch unavailable -> 502 --------------------------


@pytest.mark.asyncio
async def test_e2e_live_broker_dispatch_unavailable_no_ghost_position(monkeypatch):
    """Broker place_order raises -> 502, no position/order rows persisted."""
    await init_db()
    monkeypatch.setattr(
        "app.api.trades.get_broker_adapter", lambda acc: _UnreachableBroker()
    )
    monkeypatch.setattr(
        "app.brokers.get_broker_adapter", lambda acc: _UnreachableBroker()
    )
    settings.broker_mode = "live"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, uid, hdr = await _register(client, "broker_unavail")
        acc_id = await _add_broker(uid, broker_name="ZERODHA")

        res = await client.post(
            "/api/v1/orders/execute-dma",
            json={
                "symbol": "NIFTY", "side": "BUY", "lots": 1,
                "product": "MIS", "order_type": "MARKET",
                "stop_loss_pct": 0.5, "take_profit_pct": 1.0,
                "mode": "LIVE", "broker_account_id": acc_id,
            },
            headers=hdr,
        )
        assert res.status_code == 502
        assert "Broker rejected" in res.json()["detail"]

        # No ghost position / order persisted
        assert await _user_positions(uid) == []
        assert await _user_orders(uid) == []


# -- Test E: LIVE protective placement rejected -> 503 fail-closed ------------


@pytest.mark.asyncio
async def test_e2e_live_protective_placement_rejected_fails_closed(monkeypatch):
    """Entry dispatch succeeds but protective legs rejected -> 503, rows FAILED."""
    await init_db()
    dispatch_calls: list[str] = []
    dispatch_broker = SimulatedBroker()
    protect_broker = _RejectingProtectiveBroker()
    monkeypatch.setattr(
        "app.api.trades.get_broker_adapter",
        lambda acc: (dispatch_calls.append(acc.id), dispatch_broker)[1],
    )
    monkeypatch.setattr("app.brokers.get_broker_adapter", lambda acc: protect_broker)
    settings.broker_mode = "live"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, uid, hdr = await _register(client, "prot_reject")
        acc_id = await _add_broker(uid, broker_name="ZERODHA")

        res = await client.post(
            "/api/v1/orders/execute-dma",
            json={
                "symbol": "NIFTY", "side": "BUY", "lots": 1,
                "product": "MIS", "order_type": "MARKET",
                "stop_loss_pct": 0.5, "take_profit_pct": 1.0,
                "mode": "LIVE", "broker_account_id": acc_id,
            },
            headers=hdr,
        )
        assert res.status_code == 503
        assert "FAIL-CLOSED" in res.json()["detail"]

        positions = await _user_positions(uid)
        assert len(positions) == 1
        pos = positions[0]
        assert pos.status == "CLOSED"
        assert pos.protection_state == PROTECTION_STATE_FAILED
        assert "FAIL-CLOSED entry" in (pos.protection_error or "")

        # Protective rows exist but ALL FAILED -- no fabricated refs
        rows = await _protective_rows(pos.id)
        assert len(rows) == 2
        for r in rows:
            assert r.status == ROW_FAILED
            assert r.broker_protective_order_id is None
            assert "broker rejected" in (r.last_error or "").lower()

        # Rejecting broker got BOTH protective-leg attempts (then rejected them);
        # entry dispatch broker filled exactly one order.
        assert len(protect_broker.placed) == 2
        assert len(dispatch_calls) == 1
        