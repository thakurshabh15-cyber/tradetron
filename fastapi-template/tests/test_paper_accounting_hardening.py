"""P1-1 RED→GREEN regression suite: PAPER accounting hardening.

Proven defects this suite pins (established from the code before the fix):

  * ``app/api/trades.py`` close_position credits the AUTHENTICATED CALLER's
    ``paper_balance`` with the realized P&L of a PAPER close instead of the
    POSITION OWNER's balance.  When the caller differs from the owner
    (ADMIN/SUPERADMIN path authorized at close_position) the P&L lands in the
    wrong user's account — cross-user accounting corruption.  copy_trading's
    close path resolves ``p.user_id`` already, so the two close paths
    implement the same economic event differently.

  * ``app/engine/trading_engine._execute_signal`` (custom strategy, PAPER
    mode, owner-scoped) persists an OrderRecord + TradeRecord but NO
    PositionRecord, so a confirmed user-scoped paper fill vanishes from the
    position ledger: no /positions row, nothing to square off, no P&L ever
    booked into the owner's paper account.  (The LIVE side of the same flow
    goes through the P0-2 durable claim with ``create_position=True``.)

Invariants:
  I1. A PAPER close credits realized P&L exactly once to the POSITION OWNER's
      ``paper_balance`` (never the caller), atomically with the OPEN→CLOSED
      CAS.  No owner ⇒ no credit.
  I2. Every owner-scoped PAPER fill creates exactly one OPEN PositionRecord
      linked to its OrderRecord (ledger completeness).
  I3. P&L-tracker semantics preserved: balance = 1_000_000 + Σ realized; entry
      never debits; credit rounds to 2 decimal places.
  I4. A replay close of an already-CLOSED PAPER position returns no-op and
      NEVER re-credits the balance.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.brokers.simulated import SimulatedBroker
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.engine.trading_engine import TradingEngine
from app.main import app
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord

_STARTING = 1_000_000.0


class _FixedQuoteMarket:
    """Deterministic quote source: exit price 255.0 for every symbol."""

    def get_quote(self, symbol: str):  # noqa: ANN001
        return {"symbol": symbol, "price": 255.0}


async def _register_and_get_headers(client: AsyncClient, tag: str) -> tuple[str, dict]:
    import time

    uid = int(time.time() * 1000) % 10_000_000
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": f"paper_acc_{tag}_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": f"Paper Accounting {tag.title()}",
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


async def _seed_open_paper_position(owner_id: str) -> str:
    async with SessionLocal() as db:
        pos = PositionRecord(
            id=str(uuid.uuid4()),
            user_id=owner_id,
            broker_account_id=None,
            symbol="NIFTY50",
            side="LONG",
            quantity=20,
            entry_price=250.0,
            current_price=255.0,
            unrealized_pnl=100.0,
            realized_pnl=0.0,
            mode="PAPER",
            status="OPEN",
            opened_at=datetime.now(timezone.utc),
        )
        db.add(pos)
        await db.commit()
        return pos.id


@pytest.fixture(autouse=True)
async def _init_schema(monkeypatch):
    await init_db()
    monkeypatch.setattr("app.api.trades.unified_market_manager", _FixedQuoteMarket())
    yield


@pytest.mark.asyncio
async def test_manual_paper_close_credits_position_owner_not_caller():
    """I1: closing another user's PAPER position credits the OWNER, not the caller.

    RED today: close_position uses ``user.paper_balance += realized`` where
    ``user`` is the authenticated caller — the admin is credited and the owner
    is not.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        owner_id, _owner_headers = await _register_and_get_headers(client, "owner")
        caller_id, caller_headers = await _register_and_get_headers(client, "caller")
        # The caller closes the owner's position via the authorized ADMIN path.
        await _promote_to_admin(caller_id)

        pos_id = await _seed_open_paper_position(owner_id)

        close_res = await client.post(
            f"/api/trades/positions/{pos_id}/close", headers=caller_headers
        )
        assert close_res.status_code == 200, close_res.text
        assert close_res.json()["realized_pnl"] == 100.0

    async with SessionLocal() as db:
        owner = await db.get(UserRecord, owner_id)
        caller = await db.get(UserRecord, caller_id)
        assert owner is not None and caller is not None
        assert owner.paper_balance == round(_STARTING + 100.0, 2), (
            f"owner must be credited with the realized P&L; got {owner.paper_balance}"
        )
        assert caller.paper_balance == round(_STARTING, 2), (
            f"caller balance must be untouched; got {caller.paper_balance}"
        )


@pytest.mark.asyncio
async def test_user_scoped_paper_strategy_fill_creates_position_on_ledger():
    """I2: a user-scoped PAPER strategy fill creates exactly one OPEN
    PositionRecord linked to its OrderRecord and never debits the balance.

    RED today: _execute_signal -> _persist_trade writes OrderRecord +
    TradeRecord with NO PositionRecord, so the fill vanishes from the position
    ledger and the paper account can never square it off.
    """
    tick_q: asyncio.Queue = asyncio.Queue()
    broker = SimulatedBroker()
    broker.update_price("RELIANCE", 2000.0)
    engine = TradingEngine(broker=broker, tick_queue=tick_q)

    async with SessionLocal() as db:
        owner = UserRecord(
            id=str(uuid.uuid4()),
            email=f"strat_fill_{uuid.uuid4().hex[:8]}@tradetron.io",
            hashed_password=hash_password("Pass12345!"),
            full_name="Strat Fill Tester",
            role="trader",
            is_active=True,
            is_verified=True,
            paper_balance=_STARTING,
        )
        db.add(owner)
        await db.commit()
        owner_id = owner.id

    strategy = {
        "id": "strat-paper-acc-01",
        "name": "Paper Accounting Strategy",
        "symbols": ["RELIANCE"],
        "conditions": [],
        "action": {"side": "BUY", "quantity": 10, "order_type": "MARKET"},
        "enabled": True,
        "execution_mode": "PAPER",
        "broker_account_id": None,
        "user_id": owner_id,
        "capital_allocated": 100000.0,
    }

    await engine._execute_signal(strategy, "RELIANCE", 2000.0)

    async with SessionLocal() as db:
        positions = (
            (await db.execute(select(PositionRecord).where(PositionRecord.user_id == owner_id)))
            .scalars()
            .all()
        )
        assert len(positions) == 1, (
            f"expected exactly ONE PositionRecord on the paper ledger; got {len(positions)}"
        )
        pos = positions[0]
        assert pos.mode == "PAPER"
        assert pos.status == "OPEN"
        assert pos.side == "LONG"
        assert pos.quantity == 10
        assert pos.entry_price == 2000.0
        assert pos.current_price == 2000.0

        orders = (
            (await db.execute(select(OrderRecord).where(OrderRecord.user_id == owner_id)))
            .scalars()
            .all()
        )
        assert len(orders) == 1
        assert orders[0].position_id == pos.id, "OrderRecord must link to the PositionRecord"

        trades = (
            (await db.execute(select(TradeRecord).where(TradeRecord.user_id == owner_id)))
            .scalars()
            .all()
        )
        assert len(trades) == 1
        assert trades[0].mode == "PAPER"
        assert trades[0].price == 2000.0
        assert trades[0].pnl is None, "an entry fill books no realized P&L"

        owner = await db.get(UserRecord, owner_id)
        assert owner is not None
        assert owner.paper_balance == round(_STARTING, 2), (
            "I3: opening a PAPER position never debits paper_balance"
        )


@pytest.mark.asyncio
async def test_paper_close_replay_never_double_credits():
    """I4: a second close of an already-CLOSED paper position is a no-op (404)
    and never re-credits the owner's balance."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        owner_id, owner_headers = await _register_and_get_headers(client, "replay")
        pos_id = await _seed_open_paper_position(owner_id)

        first = await client.post(
            f"/api/trades/positions/{pos_id}/close", headers=owner_headers
        )
        assert first.status_code == 200, first.text
        assert first.json()["realized_pnl"] == 100.0

        second = await client.post(
            f"/api/trades/positions/{pos_id}/close", headers=owner_headers
        )
        assert second.status_code == 404, "replay close must be rejected by the CAS claim"

    async with SessionLocal() as db:
        owner = await db.get(UserRecord, owner_id)
        assert owner is not None
        assert owner.paper_balance == round(_STARTING + 100.0, 2), (
            f"balance must be credited exactly once; got {owner.paper_balance}"
        )