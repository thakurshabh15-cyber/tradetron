"""Phase 16 P1 RED regression: the manual/DMA position-close path must feed
the engine's auto-pilot RiskManager.

Defect (P1): ``app/api/trades.py::close_position`` computes ``realized_pnl``
and books it (TradeRecord + ``credit_paper_pnl``) but NEVER feeds the running
engine's ``RiskManager.record_trade_result``.  The auto-pilot guard is only
fed by:

  * ``app/engine/order_manager.py::OrderManager._close_position`` (the
    in-memory SMA cross/stop-loss executor), and
  * copy-trading closes (via the same OrderManager).

The dominant customer workflow -- closing a position from the UI via
``POST /api/trades/positions/{id}/close`` -- therefore bypasses the
consecutive-loss and intraday-drawdown auto-pilot kill-switch entirely.  A
trader who is manually grinding out losing hedges can rack up N consecutive
losses (or breach the daily drawdown), and the platform-wide auto-pilot WILL
NOT trip.  The same P&L that ``/api/risk-guard/status`` reports as
``daily_pnl`` is never credited into the guard, so ``risk-status.check()``
keeps returning ``allowed`` despite the configured threshold.

Fix contract: after a successful atomic close (CAS OPEN->CLOSED won and
committed), feed ``realized_pnl`` to the ENGINE's ``RiskManager`` via
``app.main.get_engine().risk_manager.record_trade_result(realized_pnl)``.
The feed must be:
  * exactly-once (only after the CAS close won and the commit succeeded),
  * owner/mode agnostic (the P&L belongs to the position regardless of who
    closed it),
  * defensive (never raises, never breaks the close response), and
  * skipped when no engine / risk manager is present (unit-test and
    engine-less contexts).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.trading import PositionRecord
from app.models.user import UserRecord

ENTRY_PRICE = 2500.0
EXIT_PRICE = 2540.0  # deterministic quote: (2540 - 2500) * QTY = +400.0
QTY = 10
SYMBOL = "RELIANCE"


class _FixedQuoteMarket:
    """Deterministic quote source: exit price EXIT_PRICE for every symbol."""

    def get_quote(self, symbol: str):  # noqa: ANN001
        return {"symbol": symbol, "price": EXIT_PRICE}


class _SpyRiskManager:
    """Records every P&L the engine risk manager is fed."""

    def __init__(self) -> None:
        self.fed: list[float] = []

    def record_trade_result(self, pnl: float) -> None:
        self.fed.append(float(pnl))


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


async def _register_and_get_token(client: AsyncClient, tag: str) -> tuple[str, str]:
    uid = uuid.uuid4().hex[:8]
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": f"autopilot_{tag}_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": f"Autopilot {tag.title()}",
        },
    )
    assert reg.status_code == 201, reg.text
    body = reg.json()
    return body["user"]["id"], body["access_token"]


async def _seed_open_paper_position(owner_id: str) -> str:
    pos_id = str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(PositionRecord(
            id=pos_id,
            user_id=owner_id,
            broker_account_id=None,
            symbol=SYMBOL,
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


def _spy_engine(monkeypatch, spy) -> None:
    class _FakeEngine:
        risk_manager = spy

    monkeypatch.setattr("app.main.get_engine", lambda: _FakeEngine())


@pytest.mark.asyncio
async def test_manual_close_feeds_autopilot_risk_manager(monkeypatch):
    """The manual position-close P&L must reach the engine's RiskManager.

    RED on base code: ``close_position`` books TradeRecord/paper balance but
    never calls ``record_trade_result``, so the spy records nothing and the
    auto-pilot cannot trip on manual losses.
    """
    spy = _SpyRiskManager()
    _spy_engine(monkeypatch, spy)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        owner_id, token = await _register_and_get_token(client, "feed")
        pos_id = await _seed_open_paper_position(owner_id)
        headers = {"Authorization": f"Bearer {token}"}

        close_res = await client.post(
            f"/api/trades/positions/{pos_id}/close", headers=headers
        )
        assert close_res.status_code == 200, close_res.text
        assert close_res.json()["realized_pnl"] == 400.0

        # The auto-pilot guard MUST have received the realized P&L exactly once.
        assert spy.fed == [400.0], (
            "manual close must feed RiskManager.record_trade_result with the "
            f"realized P&L; recorded={spy.fed}"
        )


@pytest.mark.asyncio
async def test_manual_close_never_double_feeds_on_replay(monkeypatch):
    """A second (no-op 404) close of the same position never re-feeds the RM."""
    spy = _SpyRiskManager()
    _spy_engine(monkeypatch, spy)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        owner_id, token = await _register_and_get_token(client, "replay")
        pos_id = await _seed_open_paper_position(owner_id)
        headers = {"Authorization": f"Bearer {token}"}

        close_res = await client.post(
            f"/api/trades/positions/{pos_id}/close", headers=headers
        )
        assert close_res.status_code == 200, close_res.text
        assert spy.fed == [400.0]

        replay = await client.post(
            f"/api/trades/positions/{pos_id}/close", headers=headers
        )
        assert replay.status_code == 404, replay.text
        # Exactly-once: the replay (404) must not feed the guard again.
        assert spy.fed == [400.0], f"duplicate feed on replay: {spy.fed}"


@pytest.mark.asyncio
async def test_close_still_succeeds_with_no_engine(monkeypatch):
    """Defensive contract: with no running engine, the close still works."""
    monkeypatch.setattr("app.main.get_engine", lambda: None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        owner_id, token = await _register_and_get_token(client, "noeng")
        pos_id = await _seed_open_paper_position(owner_id)
        headers = {"Authorization": f"Bearer {token}"}

        close_res = await client.post(
            f"/api/trades/positions/{pos_id}/close", headers=headers
        )
        assert close_res.status_code == 200, close_res.text
        assert close_res.json()["status"] == "CLOSED"

