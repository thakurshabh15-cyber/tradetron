"""Regression: OrderManager's in-process PAPER ledger must stay memory-bounded.

Trading/audit truth lives in the DB tables; the in-process book is a shadow, so
capping its retained rows must not lose authoritative history.  Lifetime
counters keep summary totals exact after the bounded window rolls.
"""

import asyncio

from app.brokers.simulated import SimulatedBroker
from app.engine.order_manager import OrderManager


def _close_all(om: OrderManager) -> None:
    """Trigger a hard stop-loss for every symbol in the active book."""
    for symbol in list(om.active_positions):
        pos = om.active_positions[symbol]
        asyncio.run(om.check_triggers(symbol, pos.entry_price * 0.9))


def test_book_history_is_bounded_but_lifetime_counts_are_exact(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "paper_book_max_history", 2)

    broker = SimulatedBroker()
    om = OrderManager(broker=broker, default_stop_loss_pct=0.05)
    assert om._book_cap == 2

    # Open + close three distinct symbols.
    for symbol, price in (("AAPL", 100.0), ("MSFT", 200.0), ("NVDA", 150.0)):
        asyncio.run(om.process_signal(symbol, "BUY", price, quantity=10))
        assert symbol in om.active_positions
        _close_all(om)
        assert symbol not in om.active_positions

    # The retained in-process window never exceeds the cap...
    assert len(om.closed_positions) == 2
    assert len(om.execution_history) <= 2
    # ...but lifetime totals are exact (nothing silently dropped).
    assert om.closed_positions_lifetime == 3
    assert om.executions_lifetime >= 3  # ENTRY + close executions
    # The NEWEST rows are retained (the oldest roll out of the window).
    assert om.closed_positions[-1].symbol == "NVDA"
    assert om.closed_positions[0].symbol == "MSFT"
    # No-open-book state stays intact.
    assert om.active_positions == {}


def test_clamp_never_disables_the_bound(monkeypatch):
    from app.config import settings

    # An out-of-range "0" / "disabled" / tiny value must clamp to the safe
    # minimum (100) — the memory bound can never be switched off by config.
    for bad in (0, -5, 1):
        monkeypatch.setattr(settings, "paper_book_max_history", bad)
        om = OrderManager(broker=SimulatedBroker())
        assert om._book_cap == 100, f"paper_book_max_history={bad} must clamp to 100"