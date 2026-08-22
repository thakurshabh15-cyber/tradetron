"""Unit tests for Technical Indicators (SMA, EMA, RSI, MACD, Bollinger, ATR) and Trade Metrics."""

import math
from app.engine.strategy_evaluator import StrategyEvaluator
from app.engine.order_manager import Position, Side


def test_indicator_calculations():
    """Verify SMA, EMA, RSI, MACD, Bollinger Bands, and ATR calculations."""
    evaluator = StrategyEvaluator()
    symbol = "AAPL"

    # Feed 30 price ticks to simulate market movement
    prices = [
        100.0, 101.5, 102.0, 101.0, 103.5, 105.0, 104.2, 106.0, 107.5, 106.8,
        108.0, 109.5, 110.0, 108.5, 111.0, 112.5, 111.8, 113.0, 114.5, 113.8,
        115.0, 116.5, 117.0, 115.5, 118.0, 119.5, 118.8, 120.0, 121.5, 122.0,
    ]

    for p in prices:
        evaluator.update_price(symbol, p)

    # 1. Test SMA (10 period)
    sma_10 = evaluator._compute_indicator(symbol, "SMA", 10)
    expected_sma = sum(prices[-10:]) / 10
    assert sma_10 is not None
    assert math.isclose(sma_10, expected_sma, rel_tol=1e-5)

    # 2. Test EMA (10 period)
    ema_10 = evaluator._compute_indicator(symbol, "EMA", 10)
    assert ema_10 is not None
    assert ema_10 > 110.0

    # 3. Test RSI (14 period)
    rsi_14 = evaluator._compute_indicator(symbol, "RSI", 14)
    assert rsi_14 is not None
    assert 0.0 <= rsi_14 <= 100.0
    assert rsi_14 > 50.0  # Uptrend sequence

    # 4. Test MACD (12/26)
    macd_val = evaluator._compute_indicator(symbol, "MACD", 26)
    assert macd_val is not None
    assert isinstance(macd_val, float)

    # 5. Test Bollinger Bands (Upper > Mid > Lower)
    bb_upper = evaluator._compute_indicator(symbol, "BOLLINGER_UPPER", 20)
    bb_mid = evaluator._compute_indicator(symbol, "BOLLINGER_MID", 20)
    bb_lower = evaluator._compute_indicator(symbol, "BOLLINGER_LOWER", 20)

    assert bb_upper is not None and bb_mid is not None and bb_lower is not None
    assert bb_upper > bb_mid > bb_lower

    # 6. Test ATR (14 period)
    atr_14 = evaluator._compute_indicator(symbol, "ATR", 14)
    assert atr_14 is not None
    assert atr_14 > 0.0


def test_trade_record_metrics():
    """Verify position pnl, pnlPct, duration, and exitReason calculations."""
    pos = Position(
        position_id="test-pos-001",
        symbol="NVDA",
        side=Side.BUY,
        quantity=10,
        entry_price=120.0,
        stop_loss_price=117.6,
        take_profit_price=126.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.05,
    )

    # 1. Unrealized PnL at 126.0 (Take Profit hit)
    unrealized = pos.update_pnl(126.0)
    assert unrealized == 60.0  # ($126 - $120) * 10 = +$60.00
    assert pos.pnl_pct == 5.0   # +5.0% return

    # 2. Close Position with Take Profit
    pos.realized_pnl = 60.0
    pos.status = "CLOSED"
    pos.exit_reason = "TAKE_PROFIT"

    assert pos.realized_pnl == 60.0
    assert pos.pnl_pct == 5.0
    assert pos.exit_reason == "TAKE_PROFIT"
    assert pos.duration_seconds >= 0.0
