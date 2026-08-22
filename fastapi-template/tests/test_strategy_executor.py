"""Test StrategyExecutor with SMA 50/200 crossover using collections.deque."""

from app.engine.trading_engine import StrategyExecutor
import pytest


@pytest.mark.asyncio
async def test_strategy_executor_deque_buffer():
    executor = StrategyExecutor(fast_period=5, slow_period=10, buffer_size=20)

    # Push 4 prices (less than fast_period 5)
    for price in [100.0, 101.0, 102.0, 103.0]:
        res = await executor.on_tick("AAPL", price)
        assert res["signal"] is None

    # Push up to 9 prices (fast SMA ready, slow SMA not ready)
    for price in [104.0, 105.0, 106.0, 107.0, 108.0]:
        res = await executor.on_tick("AAPL", price)
        assert res["signal"] is None

    # 10th price: Slow SMA is now available.
    # Prices: 100 to 109. Uptrend: Fast SMA (last 5) > Slow SMA (last 10). Initial state set to 'above'.
    res = await executor.on_tick("AAPL", 109.0)
    assert res["signal"] is None  # Initial state established, no crossover yet
    assert executor.crossover_states["AAPL"] == "above"

    # Now simulate a downtrend to trigger a Death Cross (Fast SMA falls below Slow SMA)
    death_cross_triggered = False
    for price in [80.0, 75.0, 70.0, 65.0, 60.0, 55.0]:
        res = await executor.on_tick("AAPL", price)
        if res.get("signal") == "SELL":
            death_cross_triggered = True
            break
    assert death_cross_triggered, "Death Cross (SELL) should be triggered"
    assert executor.crossover_states["AAPL"] == "below"

    # Now simulate a sharp rebound to trigger a Golden Cross (Fast SMA crosses above Slow SMA)
    golden_cross_triggered = False
    for price in [150.0, 160.0, 170.0, 180.0, 190.0, 200.0]:
        res = await executor.on_tick("AAPL", price)
        if res.get("signal") == "BUY":
            golden_cross_triggered = True
            break
    assert golden_cross_triggered, "Golden Cross (BUY) should be triggered"
    assert executor.crossover_states["AAPL"] == "above"


def test_strategy_executor_50_200_periods():
    executor = StrategyExecutor(fast_period=50, slow_period=200)
    assert executor.fast_period == 50
    assert executor.slow_period == 200
    assert executor.buffer_size >= 250
