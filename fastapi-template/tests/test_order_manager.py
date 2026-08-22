"""Unit tests for OrderManager and its integration with StrategyExecutor."""

import asyncio
from app.brokers.simulated import SimulatedBroker
from app.engine.order_manager import OrderManager, Position
from app.engine.trading_engine import StrategyExecutor
from app.schemas.trading import Side


def test_order_manager_risk_max_position():
    """Test that OrderManager rejects trades exceeding max position size."""
    broker = SimulatedBroker()
    om = OrderManager(broker=broker, max_position_size=50)

    # Valid order (30 <= 50)
    allowed, msg = om.validate_risk("AAPL", Side.BUY, 30)
    assert allowed is True

    # Run signal to open position of 30
    asyncio.run(om.process_signal("AAPL", "BUY", 100.0, quantity=30))
    assert "AAPL" in om.active_positions
    assert om.active_positions["AAPL"].quantity == 30

    # Attempt to add another 30 (total 60 > 50) -> Rejected
    allowed, msg = om.validate_risk("AAPL", Side.BUY, 30)
    assert allowed is False
    assert "Max position size exceeded" in msg


def test_order_manager_stop_loss_lifecycle():
    """Test full trade lifecycle: Entry -> Stop-Loss trigger -> Closed Position."""
    broker = SimulatedBroker()
    om = OrderManager(broker=broker, default_stop_loss_pct=0.05, default_take_profit_pct=0.10)

    # Entry BUY 10 AAPL @ $100 -> SL = $95 (5%), TP = $110 (10%)
    exec_entry = asyncio.run(om.process_signal("AAPL", "BUY", 100.0, quantity=10))
    assert exec_entry is not None
    assert exec_entry.action_type == "ENTRY"

    pos = om.active_positions.get("AAPL")
    assert pos is not None
    assert pos.entry_price == 100.0
    assert pos.stop_loss_price == 95.0
    assert pos.take_profit_price == 110.0

    # Price drops to $98 -> No trigger (SL is 95)
    trigger_exits = asyncio.run(om.check_triggers("AAPL", 98.0))
    assert len(trigger_exits) == 0
    assert "AAPL" in om.active_positions
    assert pos.unrealized_pnl == -20.0  # (98 - 100) * 10

    # Price drops to $94 -> Triggers Stop-Loss!
    trigger_exits = asyncio.run(om.check_triggers("AAPL", 94.0))
    assert len(trigger_exits) == 1
    assert trigger_exits[0].action_type == "STOP_LOSS_EXIT"
    assert trigger_exits[0].side == Side.SELL
    assert trigger_exits[0].price == 94.0
    assert trigger_exits[0].pnl == -60.0  # (94 - 100) * 10

    # Position is now closed
    assert "AAPL" not in om.active_positions
    assert len(om.closed_positions) == 1
    assert om.closed_positions[0].exit_reason == "STOP_LOSS"
    assert om.realized_pnl == -60.0


def test_order_manager_take_profit_lifecycle():
    """Test Take-Profit trigger on price rise."""
    broker = SimulatedBroker()
    om = OrderManager(broker=broker, default_stop_loss_pct=0.05, default_take_profit_pct=0.10)

    # Entry BUY 10 NVDA @ $100 -> TP = $110
    asyncio.run(om.process_signal("NVDA", "BUY", 100.0, quantity=10))

    # Price rises to $112 -> Triggers Take-Profit!
    trigger_exits = asyncio.run(om.check_triggers("NVDA", 112.0))
    assert len(trigger_exits) == 1
    assert trigger_exits[0].action_type == "TAKE_PROFIT_EXIT"
    assert trigger_exits[0].pnl == 120.0  # (112 - 100) * 10
    assert "NVDA" not in om.active_positions
    assert om.realized_pnl == 120.0


def test_strategy_executor_with_integrated_order_manager():
    """Test StrategyExecutor end-to-end: SMA signals triggering OrderManager trade lifecycle."""
    broker = SimulatedBroker()
    om = OrderManager(broker=broker, max_position_size=100, default_stop_loss_pct=0.05)
    executor = StrategyExecutor(fast_period=5, slow_period=10, order_manager=om, trade_quantity=10)

    # Warmup 9 ticks (no crossover yet)
    for p in [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0]:
        res = asyncio.run(executor.on_tick("AAPL", p))
        assert res["signal"] is None
        assert res["new_execution"] is None

    # 10th tick -> initial state 'above' (Fast > Slow)
    res = asyncio.run(executor.on_tick("AAPL", 109.0))
    assert res["signal"] is None

    # Downtrend -> triggers Death Cross (SELL signal)
    sell_executed = False
    for p in [80.0, 75.0, 70.0, 65.0, 60.0]:
        res = asyncio.run(executor.on_tick("AAPL", p))
        if res["signal"] == "SELL":
            sell_executed = True
            assert res["new_execution"] is not None
            assert res["new_execution"].side == Side.SELL
            assert "AAPL" in om.active_positions
            assert om.active_positions["AAPL"].side == Side.SELL
            break

    assert sell_executed, "StrategyExecutor should have executed SELL order via OrderManager"

    # Rebound -> triggers Golden Cross (BUY signal) which closes short position and opens long position
    buy_executed = False
    for p in [150.0, 160.0, 170.0, 180.0, 190.0, 200.0]:
        res = asyncio.run(executor.on_tick("AAPL", p))
        if res["signal"] == "BUY":
            buy_executed = True
            assert res["new_execution"] is not None
            assert res["new_execution"].side == Side.BUY
            assert "AAPL" in om.active_positions
            assert om.active_positions["AAPL"].side == Side.BUY
            break

    assert buy_executed, "StrategyExecutor should have executed BUY reversal via OrderManager"
