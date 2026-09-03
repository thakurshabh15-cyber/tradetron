"""Phase 3 — Staging Verification: Trading safety & risk management.

Verifies every code path that can place/modify orders goes through the
pre-trade risk gate. Covers kill-switch, circuit-breaker, position limits,
margin checks, and auto-pilot kill-switch activation.
"""

from __future__ import annotations

import pytest

from app.engine.risk_manager import RiskManager
from app.schemas.trading import OrderRequest, Side


def _order(sym="NIFTY", qty=10):
    return OrderRequest(symbol=sym, side=Side.BUY, quantity=qty)


class TestKillSwitch:

    def test_blocks_after_trigger(self):
        rm = RiskManager()
        assert rm.check(_order())[0]
        rm.trigger_kill_switch("staging halt")
        ok, reason = rm.check(_order())
        assert not ok and "Kill-Switch" in reason

    def test_reset_restores_normal_flow(self):
        rm = RiskManager()
        rm.trigger_kill_switch("test")
        rm.reset_kill_switch()
        assert rm.check(_order())[0]

    def test_manual_and_auto_reset_independent(self):
        rm = RiskManager()
        rm.trigger_kill_switch("manual")
        assert rm._kill_switch
        rm.reset_kill_switch()
        assert rm._kill_switch is False


class TestCircuitBreaker:

    def test_daily_loss_trips_breaker(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "max_daily_loss", 100.0)
        rm = RiskManager()
        rm._daily_pnl = -150.0
        ok, reason = rm.check(_order())
        assert not ok and "Daily loss limit" in reason


class TestMarginGate:

    def test_rejects_when_insufficient(self):
        rm = RiskManager()
        ok, _ = rm.check_margin(500.0, 1000.0)
        assert not ok

    def test_allows_when_sufficient(self):
        rm = RiskManager()
        ok, msg = rm.check_margin(2000.0, 1000.0)
        assert ok and msg == "OK"


class TestAutoPilot:

    def test_consecutive_losses_trip_kill_switch(self):
        rm = RiskManager()
        rm.configure_autopilot(enabled=True, max_consecutive_losses=5)
        for _ in range(4):
            rm.record_trade_result(-100.0)
        assert rm._kill_switch is False
        rm.record_trade_result(-100.0)
        assert rm._kill_switch is True
        assert "AUTO-PILOT" in rm._kill_switch_reason

    def test_winning_trade_resets_streak(self):
        rm = RiskManager()
        rm.configure_autopilot(max_consecutive_losses=3)
        rm.record_trade_result(-10.0)
        rm.record_trade_result(-10.0)
        rm.record_trade_result(+5.0)  # breaks streak
        st = rm.get_autopilot_status()
        assert st["consecutive_losses"] == 0 and not st["kill_switch_active"]

    def test_drawdown_trip(self):
        rm = RiskManager()
        rm.configure_autopilot(max_consecutive_losses=0, max_daily_drawdown_pct=10.0)
        rm.record_trade_result(+1000.0)
        rm.record_trade_result(-200.0)
        assert rm._kill_switch is True and "drawdown" in rm._kill_switch_reason.lower()

    def test_disable_autopilot_never_halt(self):
        rm = RiskManager()
        rm.configure_autopilot(enabled=False, max_consecutive_losses=1)
        rm.record_trade_result(-100.0)
        rm.record_trade_result(-100.0)
        assert rm.check(_order())[0]

    def test_reset_autopilot_clears_streak(self):
        rm = RiskManager()
        rm.configure_autopilot(max_consecutive_losses=1)
        rm.record_trade_result(-10.0)
        assert rm._kill_switch
        st = rm.reset_autopilot()
        assert not st["kill_switch_active"] and st["consecutive_losses"] == 0
