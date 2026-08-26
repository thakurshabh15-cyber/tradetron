"""Quant QA — TradeThrone Auto-Pilot Risk Guard."""

import pytest

from app.engine.risk_manager import RiskManager
from app.schemas.trading import OrderRequest, Side


def _rm(**cfg) -> RiskManager:
    rm = RiskManager()
    rm.configure_autopilot(**cfg)
    return rm


def test_consecutive_losses_trip_kill_switch():
    rm = _rm(max_consecutive_losses=3)
    for _ in range(2):
        rm.record_trade_result(-100.0)
    allowed, _ = rm.check(OrderRequest(symbol="NIFTY", side=Side.BUY, quantity=65))
    assert allowed, "kill-switch must NOT be active before threshold"
    rm.record_trade_result(-150.0)
    allowed, reason = rm.check(OrderRequest(symbol="NIFTY", side=Side.BUY, quantity=65))
    assert not allowed
    assert "AUTO-PILOT" in reason and "consecutive" in reason


def test_winning_trade_resets_loss_streak():
    rm = _rm(max_consecutive_losses=3)
    rm.record_trade_result(-100.0)
    rm.record_trade_result(-100.0)
    rm.record_trade_result(+50.0)     # streak broken
    rm.record_trade_result(-100.0)    # streak back to 1
    status = rm.get_autopilot_status()
    assert status["consecutive_losses"] == 1
    assert not status["kill_switch_active"]


def test_intraday_drawdown_trips_kill_switch():
    rm = _rm(max_daily_drawdown_pct=10.0, max_consecutive_losses=0)
    rm.record_trade_result(+1000.0)   # peak daily pnl
    rm.record_trade_result(-120.0)    # 12% giveback from peak
    allowed, reason = rm.check(OrderRequest(symbol="NIFTY", side=Side.BUY, quantity=65))
    assert not allowed
    assert "drawdown" in reason.lower()
    st = rm.get_autopilot_status()
    assert st["peak_daily_pnl"] == 1000.0
    assert st["drawdown_pct"] == pytest.approx(12.0)


def test_autopilot_disabled_never_halts():
    rm = _rm(enabled=False, max_consecutive_losses=1, max_daily_drawdown_pct=1.0)
    for pnl in (-100.0, -100.0, -100.0):
        rm.record_trade_result(pnl)
    allowed, _ = rm.check(OrderRequest(symbol="NIFTY", side=Side.BUY, quantity=65))
    assert allowed


def test_reset_autopilot_clears_streak_and_releases_switch():
    rm = _rm(max_consecutive_losses=1)
    rm.record_trade_result(-10.0)
    assert rm.get_autopilot_status()["kill_switch_active"]
    st = rm.reset_autopilot()
    assert not st["kill_switch_active"]
    assert st["consecutive_losses"] == 0
    allowed, _ = rm.check(OrderRequest(symbol="NIFTY", side=Side.BUY, quantity=65))
    assert allowed


def test_configure_validates_ranges():
    rm = RiskManager()
    with pytest.raises(ValueError):
        rm.configure_autopilot(max_consecutive_losses=500)
    with pytest.raises(ValueError):
        rm.configure_autopilot(max_daily_drawdown_pct=-2)
    st = rm.configure_autopilot(max_daily_drawdown_pct=25)
    assert st["max_daily_drawdown_pct"] == 25


def test_record_trade_result_never_raises_on_bad_input():
    rm = RiskManager()
    rm.record_trade_result(float("nan"))  # defensive path must not explode
    assert isinstance(rm.get_autopilot_status(), dict)


def test_order_manager_hook_feeds_closed_pnl(monkeypatch):
    """The OrderManager._close_position auto-pilot hook must feed the RM."""
    from app.engine.order_manager import OrderManager, Position

    fed = []

    class FakeRM:
        def record_trade_result(self, pnl):
            fed.append(pnl)

    class StaticBroker:
        def update_price(self, *a, **k):
            pass

        async def place_order(self, req):
            return {"filled_price": 90.0, "broker_order_id": "T1"}

    om = OrderManager(broker=StaticBroker())
    monkeypatch.setattr(om, "risk_manager", FakeRM(), raising=False)

    pos = Position(
        position_id="p1", symbol="AAPL", side=Side.BUY,
        quantity=10, entry_price=100.0,
    )
    om.active_positions[pos.symbol] = pos

    import asyncio
    asyncio.run(om._close_position(pos, exit_price=90.0,
                                   exit_reason="STOP_LOSS",
                                   action_type="STOP_LOSS_EXIT"))
    assert fed and fed[0] == pytest.approx((90.0 - 100.0) * 10)
    assert om.realized_pnl == pytest.approx(-100.0)
    assert pos.status == "CLOSED"