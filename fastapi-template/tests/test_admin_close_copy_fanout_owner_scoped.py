"""P1-2 RED regression: the copy-trading close fan-out must be keyed to the
POSITION OWNER's identity, never the authenticated caller.

Defect (P1): ``app/api/trades.py::close_position`` computes the copy-trading
fan-out master as::

    master_uid = user.id if user else pos.user_id

``user`` is a mandatory dependency (never ``None``), so when an
ADMIN/SUPERADMIN closes another trader's position (authorized by role at the
ownership check) the fan-out runs under the ADMIN's id instead of the
position owner's.  Consequences:

  1. The OWNER's copy-trading followers never receive the mirrored close —
     their mirrored OPEN position keeps stale exposure and can never be
     squared off by the normal close flow (PAPER accounting skew / LIVE
     book divergence for followers).
  2. If the ADMIN happens to have their own copy groups, ``mirror_close_position``
     selects the ADMIN's followers and closes THEIR unrelated OPEN positions on
     the same symbol — phantom closes of state the master never held.

The P1-1 balance-credit for the same close is already owner-scoped
(``credit_paper_pnl(db, pos.user_id, ...)``); the fan-out must use the same
owner identity so the two sides of the same economic event match.

Scope note: for a position owner closing their OWN position (the common path)
``user.id == pos.user_id``, so this change is behavior-preserving.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.copy_trading import CopyFollowerRecord, CopyGroupRecord
from app.models.trading import PositionRecord
from app.models.user import UserRecord

SIDE_STARTING_BALANCE = 1_000_000.0
EXIT_PRICE = 2540.0  # deterministic quote: (2540 - 2500) * 10 = 400.0
ENTRY_PRICE = 2500.0
QTY = 10
SYMBOL = "RELIANCE"
class _FixedQuoteMarket:
    """Deterministic quote source: exit price EXIT_PRICE for every symbol."""

    def get_quote(self, symbol: str):  # noqa: ANN001
        return {"symbol": symbol, "price": EXIT_PRICE}


@pytest.fixture(autouse=True)
async def _init_schema(monkeypatch):
    await init_db()
    monkeypatch.setattr("app.api.trades.unified_market_manager", _FixedQuoteMarket())
    yield


@pytest.fixture(autouse=True)
def _mock_notify_trade_fill(monkeypatch):
    """No Telegram/HTTP I/O from fill notifications during tests."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr("app.engine.alerts.notify_trade_fill", AsyncMock())


async def _register_and_get_headers(client: AsyncClient, tag: str) -> tuple[str, dict]:
    uid = uuid.uuid4().hex[:8]
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": f"fanout_{tag}_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": f"Fanout {tag.title()}",
        },
    )
    assert reg.status_code == 201, reg.text
    body = reg.json()
    token = body["access_token"]
    user_id = body["user"]["id"]
    return user_id, {"Authorization": f"Bearer {token}"}


async def _promote_to_admin(user_id: str) -> None:
    async with SessionLocal() as db:
        user = await db.get(UserRecord, user_id)
        assert user is not None
        user.role = "admin"
        await db.commit()


async def _seed_open_paper_position(owner_id: str, *, symbol: str = SYMBOL) -> str:
    pos_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(PositionRecord(
            id=pos_id,
            user_id=owner_id,
            broker_account_id=None,
            symbol=symbol,
            side="LONG",
            quantity=QTY,
            entry_price=ENTRY_PRICE,
            current_price=ENTRY_PRICE,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            mode="PAPER",
            status="OPEN",
            opened_at=datetime.now(timezone.utc),
        ))
        await db.commit()
    return pos_id


async def _seed_active_group(master_user_id: str, name: str) -> str:
    group_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(CopyGroupRecord(
            id=group_id,
            master_user_id=master_user_id,
            name=name,
            is_active=True,
            is_public=False,
        ))
        await db.commit()
    return group_id


async def _seed_active_follower(follower_user_id: str, group_id: str) -> str:
    fol_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(CopyFollowerRecord(
            id=fol_id,
            follower_user_id=follower_user_id,
            group_id=group_id,
            multiplier=1.0,
            max_allocation=100_000.0,
            status="ACTIVE",
            mode="PAPER",
        ))
        await db.commit()
    return fol_id


async def _position_state(pos_id: str) -> dict:
    async with SessionLocal() as db:
        p = await db.get(PositionRecord, pos_id)
        if p is None:
            return {"status": None, "realized_pnl": None}
        return {"status": p.status, "realized_pnl": p.realized_pnl}


async def _balance(user_id: str) -> float:
    async with SessionLocal() as db:
        u = await db.get(UserRecord, user_id)
        return float(u.paper_balance) if u is not None else -1.0


async def _follower_realized_pnl(fol_id: str) -> float:
    async with SessionLocal() as db:
        row = await db.get(CopyFollowerRecord, fol_id)
        return float(row.realized_pnl or 0.0) if row is not None else -1.0


async def _wait_for_close(pos_id: str, *, timeout: float = 3.0) -> dict:
    """Poll until the position flips to CLOSED (background fan-out settle)."""
    deadline = time.monotonic() + timeout
    state: dict = {}
    while time.monotonic() < deadline:
        state = await _position_state(pos_id)
        if state["status"] == "CLOSED":
            return state
        await asyncio.sleep(0.05)
    return state


async def _seed_full_scenario(client: AsyncClient) -> dict:
    """master M (owner) + follower F, and admin A + follower AF."""
    master_id, master_headers = await _register_and_get_headers(client, "master")
    follower_id, _f_headers = await _register_and_get_headers(client, "follower")
    admin_id, admin_headers = await _register_and_get_headers(client, "admin")
    await _promote_to_admin(admin_id)

    # Master's copy group + follower
    group_m = await _seed_active_group(master_id, "Master Group")
    fol_m = await _seed_active_follower(follower_id, group_m)
    # Admin's unrelated copy group + follower (must never be touched by this close)
    group_a = await _seed_active_group(admin_id, "Admin Group")
    fol_a = await _seed_active_follower(admin_id, group_a)

    # Positions
    master_pos = await _seed_open_paper_position(master_id)
    follower_pos = await _seed_open_paper_position(follower_id)
    admin_follower_pos = await _seed_open_paper_position(admin_id)  # admin follower's INDEPENDENT trade

    return {
        "master_id": master_id,
        "master_headers": master_headers,
        "follower_id": follower_id,
        "admin_id": admin_id,
        "admin_headers": admin_headers,
        "fol_m": fol_m,
        "fol_a": fol_a,
        "master_pos": master_pos,
        "follower_pos": follower_pos,
        "admin_follower_pos": admin_follower_pos,
    }
@pytest.mark.asyncio
async def test_admin_close_fans_out_under_position_owner_identity():
    """RED: an ADMIN closing the master's position must close the MASTER's
    followers' mirrored positions (and credit them) while leaving the ADMIN's
    own group/followers completely untouched.

    RED today: the fan-out is keyed to the caller (`user.id` = admin), so the
    master's follower position stays OPEN and — because the admin has their own
    copy group — the admin follower's independent position is phantom-closed.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ctx = await _seed_full_scenario(client)

        close_res = await client.post(
            f"/api/trades/positions/{ctx['master_pos']}/close",
            headers=ctx["admin_headers"],
        )
        assert close_res.status_code == 200, close_res.text
        assert close_res.json()["realized_pnl"] == 400.0
        assert close_res.json()["status"] == "CLOSED"

        # Settle both possible fan-out destinations, then read final state.
        await asyncio.sleep(0.15)  # let the background fan-out task run
        master_state = await _wait_for_close(ctx["master_pos"])
        follower_state = await _wait_for_close(ctx["follower_pos"])
        admin_follower_state = await _wait_for_close(ctx["admin_follower_pos"])

        assert master_state["status"] == "CLOSED"
        assert master_state["realized_pnl"] == 400.0

        # 1. The OWNER's follower must be closed and credited (the defect).
        assert follower_state["status"] == "CLOSED", (
            "master's follower position must be closed by the mirrored fan-out; "
            f"actual={follower_state}"
        )
        assert follower_state["realized_pnl"] == 400.0

        follower_balance = await _balance(ctx["follower_id"])
        assert follower_balance == round(SIDE_STARTING_BALANCE + 400.0, 2), (
            f"follower paper balance must be credited once; got {follower_balance}"
        )
        assert await _follower_realized_pnl(ctx["fol_m"]) == 400.0

        # 2. The ADMIN's own group/follower must NOT be phantom-closed.
        assert admin_follower_state["status"] == "OPEN", (
            "admin follower's independent position must stay OPEN; "
            f"actual={admin_follower_state} — phantom close via caller identity"
        )
        assert await _balance(ctx["admin_id"]) == SIDE_STARTING_BALANCE
        assert await _follower_realized_pnl(ctx["fol_a"]) == 0.0

        # 3. Idempotency: a second close of the same position is a no-op (404)
        #    and never re-fires the fan-out / re-credits.
        replay = await client.post(
            f"/api/trades/positions/{ctx['master_pos']}/close",
            headers=ctx["admin_headers"],
        )
        assert replay.status_code == 404, replay.text
        await asyncio.sleep(0.15)
        assert await _balance(ctx["follower_id"]) == round(SIDE_STARTING_BALANCE + 400.0, 2)
        assert await _follower_realized_pnl(ctx["fol_m"]) == 400.0


@pytest.mark.asyncio
async def test_owner_close_preserves_fanout_and_owner_credit():
    """GREEN guard: the owner closing their OWN position keeps fanning out to
    their own followers and credits the owner's balance exactly once."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ctx = await _seed_full_scenario(client)

        close_res = await client.post(
            f"/api/trades/positions/{ctx['master_pos']}/close",
            headers=ctx["master_headers"],
        )
        assert close_res.status_code == 200, close_res.text

        await asyncio.sleep(0.15)
        follower_state = await _wait_for_close(ctx["follower_pos"])
        assert follower_state["status"] == "CLOSED", follower_state
        assert follower_state["realized_pnl"] == 400.0

        master_balance = await _balance(ctx["master_id"])
        assert master_balance == round(SIDE_STARTING_BALANCE + 400.0, 2), (
            f"owner balance credited once; got {master_balance}"
        )
        assert await _balance(ctx["follower_id"]) == round(SIDE_STARTING_BALANCE + 400.0, 2)

        # Admin follower's independent position untouched.
        admin_follower_state = await _wait_for_close(ctx["admin_follower_pos"])
        assert admin_follower_state["status"] == "OPEN"