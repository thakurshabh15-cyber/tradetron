"""Quant QA — TradeThrone Truthful Backtesting Engine."""

import pytest

from app.engine.backtester import (
    TIMEFRAME_MINUTES,
    generate_candles,
    run_backtest,
)

RSI_COND = [{"indicator": "RSI", "operator": "lt", "value": 45, "period": 14}]
ALWAYS_COND = [{"indicator": "PRICE", "operator": "gt", "value": 0, "period": 1}]


def test_candle_generation_is_deterministic():
    a = generate_candles("NIFTY", days=10, timeframe="5m", seed=123)
    b = generate_candles("NIFTY", days=10, timeframe="5m", seed=123)
    c = generate_candles("NIFTY", days=10, timeframe="5m", seed=124)
    assert a == b
    assert a != c
    assert all(set(bar) == {"ts", "open", "high", "low", "close"} for bar in a)
    assert all(bar["low"] <= bar["close"] <= bar["high"] for bar in a)


def test_unsupported_timeframe_rejected():
    with pytest.raises(ValueError):
        generate_candles("NIFTY", days=5, timeframe="7m")


def test_lot_size_compliance_nifty():
    report = run_backtest("NIFTY", ALWAYS_COND, quantity=130, days=20, seed=1)
    assert report["quantity"] == 130          # 2 x 65
    assert report["lot_size"] == 65


def test_below_minimum_lot_floors_to_one_lot_with_warning():
    report = run_backtest("BANKNIFTY", ALWAYS_COND, quantity=5, days=20, seed=1)
    assert report["quantity"] == 30           # BANKNIFTY lot = 30
    assert report["lot_warning"]
    assert "minimum" in report["lot_warning"].lower()


def test_charges_are_truthful_and_net_of_costs():
    report = run_backtest("NIFTY", ALWAYS_COND, quantity=65, days=30, seed=9)
    m = report["metrics"]
    n = m["total_trades"]
    assert n >= 1
    # Flat Rs20 brokerage per leg => Rs40 per round trip
    assert report["charges_breakdown"]["brokerage"] == pytest.approx(40.0 * n)
    assert m["total_charges"] > 0
    assert m["gross_pnl"] == pytest.approx(
        sum(t["gross_pnl"] for t in report["trades"])
    )
    assert m["net_pnl"] == pytest.approx(m["gross_pnl"] - m["total_charges"])
    for t in report["trades"]:
        assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["charges"])


def test_determinism_same_seed_same_report():
    kw = dict(quantity=65, timeframe="15m", days=25, seed=42,
              stop_loss_pct=0.5, take_profit_pct=1.0)
    r1 = run_backtest("FINNIFTY", RSI_COND, **kw)
    r2 = run_backtest("FINNIFTY", RSI_COND, **kw)
    assert r1["metrics"] == r2["metrics"]
    assert [t["net_pnl"] for t in r1["trades"]] == [t["net_pnl"] for t in r2["trades"]]


def test_long_and_short_paths_execute():
    long_r = run_backtest("TCS", ALWAYS_COND, side="BUY", quantity=175, days=15, seed=3)
    short_r = run_backtest("TCS", ALWAYS_COND, side="SELL", quantity=175, days=15, seed=3)
    assert long_r["trades"][0]["side"] == "BUY"
    assert short_r["trades"][0]["side"] == "SELL"


def test_stop_loss_and_take_profit_exits_occur():
    exits = set()
    for seed in range(4):
        rep = run_backtest("BANKNIFTY", RSI_COND, quantity=30, timeframe="5m",
                           days=60, stop_loss_pct=0.3, take_profit_pct=0.6,
                           seed=seed)
        exits |= {t["exit_reason"] for t in rep["trades"]}
    assert "STOP_LOSS" in exits
    assert "TAKE_PROFIT" in exits
    assert "END_OF_DATA" in exits  # open positions force-closed at last bar


def test_insufficient_bars_returns_error_shape():
    rep = run_backtest("NIFTY", ALWAYS_COND, days=1, timeframe="1d")
    assert "error" in rep and rep.get("metrics") == {}


def test_equity_curve_matches_trade_sequence():
    rep = run_backtest("RELIANCE", ALWAYS_COND, quantity=250, days=30, seed=11,
                       capital=200_000.0)
    curve = rep["equity_curve"]
    assert len(curve) == len(rep["trades"])
    expected = 200_000.0
    for point, t in zip(curve, rep["trades"]):
        expected += t["net_pnl"]
        assert point["equity"] == pytest.approx(expected)
    assert rep["metrics"]["final_equity"] == pytest.approx(expected)


def test_all_supported_timeframes_declared():
    assert set(TIMEFRAME_MINUTES) == {"1m", "5m", "15m", "30m", "1h", "1d"}