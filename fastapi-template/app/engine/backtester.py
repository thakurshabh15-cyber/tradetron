"""TradeThrone Truthful Backtesting Engine.

Design goals
------------
1. **Truthful costs** — every simulated round-trip is priced through
   ``app.compliance.lot_sizes.TransactionCharges`` so results reflect
   exact Indian statutory charges: brokerage (Rs 20/order), STT,
   exchange transaction fees, GST (18%), stamp duty, SEBI turnover fee
   and configurable slippage.  Reported P&L is always NET of costs.
2. **SEBI-compliant sizing** — quantities are auto-normalised to valid
   exchange lot multiples before simulation.
3. **Deterministic data** — candles come from a seeded random walk so
   any report reproduces bit-for-bit from its ``seed``.  With no local
   historical feed this keeps the engine offline-capable while staying
   statistically honest (per-timeframe volatility calibration).
4. **Signal parity** — entries are evaluated with the very same
   ``StrategyEvaluator`` that drives live execution, so backtested
   behaviour matches deployed behaviour.

Pure-Python (no DB / network) — trivially unit-testable.
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from dataclasses import dataclass, field

from app.compliance.lot_sizes import (
    LOT_SIZES,
    TransactionCharges,
    resolve_symbol,
    validate_quantity,
)
from app.core.logging import get_logger
from app.engine.strategy_evaluator import StrategyEvaluator

logger = get_logger("engine.backtester")

# ─── Timeframe calibration ────────────────────────────────────────────

TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "1d": 375,  # full NSE session in minutes
}

MINUTES_PER_DAY = 375
TRADING_DAYS_PER_YEAR = 252

_BASE_PRICES: dict[str, float] = {
    "NIFTY": 24_300.0,
    "BANKNIFTY": 51_800.0,
    "FINNIFTY": 23_100.0,
    "MIDCPNIFTY": 12_600.0,
    "SENSEX": 79_400.0,
    "BANKEX": 57_200.0,
}

_INDEX_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "SENSEX", "BANKEX",
}


def _stable_seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:16], 16)


def canonical_is_index(symbol: str) -> bool:
    canonical, _exchange = resolve_symbol(symbol)
    return canonical in _INDEX_SYMBOLS


def is_derivative_symbol(symbol: str) -> bool:
    """True when the symbol trades as F&O / commodity / currency derivative."""
    canonical, exchange = resolve_symbol(symbol)
    return exchange in {"NFO", "BFO", "MCX", "CDS"} or canonical in _INDEX_SYMBOLS


def _base_price(symbol: str) -> float:
    canonical, _exchange = resolve_symbol(symbol)
    if canonical in _BASE_PRICES:
        return _BASE_PRICES[canonical]
    # Deterministic pseudo-spot for cash symbols (Rs 180 - Rs 4,800 band)
    h = _stable_seed("spot", canonical)
    return 180.0 + (h % 46_200) / 10.0


def generate_candles(
    symbol: str,
    days: int,
    timeframe: str = "5m",
    seed: int | None = None,
    max_bars: int = 4000,
) -> list[dict]:
    """Generate a deterministic OHLC series for ``symbol``.

    Returns dicts of ``{ts, open, high, low, close}``.  Identical inputs
    always yield an identical series — every backtest is reproducible.
    """
    tf_key = timeframe.lower()
    if tf_key not in TIMEFRAME_MINUTES:
        raise ValueError(
            f"Unsupported timeframe '{timeframe}'. Use one of {list(TIMEFRAME_MINUTES)}"
        )

    tf_min = TIMEFRAME_MINUTES[tf_key]
    bars_per_day = max(1, MINUTES_PER_DAY // tf_min) if tf_key != "1d" else 1
    n_bars = min(max(10, days * bars_per_day), max_bars)

    rng = random.Random(_stable_seed(symbol, tf_key, days, seed))
    price = _base_price(symbol)

    annual_vol = 0.18 if canonical_is_index(symbol) else 0.28
    bar_vol = annual_vol / math.sqrt(TRADING_DAYS_PER_YEAR * bars_per_day)

    candles: list[dict] = []
    ts = _stable_seed("epoch", symbol) % 1_700_000_000

    for i in range(n_bars):
        drift = rng.gauss(0.0002, bar_vol)
        open_p = price
        close_p = max(1.0, open_p * (1.0 + drift))
        wick = abs(drift) * open_p * rng.uniform(0.4, 1.6) + open_p * bar_vol * 0.35
        high_p = max(open_p, close_p) + wick * rng.uniform(0.1, 0.9)
        low_p = max(0.5, min(open_p, close_p) - wick * rng.uniform(0.1, 0.9))

        candles.append({
            "ts": ts + i * tf_min * 60,
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
        })
        price = close_p

    return candles


# ─── Core backtest ────────────────────────────────────────────────────

@dataclass
class OpenTrade:
    entry_idx: int
    entry_price: float
    quantity: int
    side: str                       # "BUY" (long) | "SELL" (short)
    stop_loss: float | None
    take_profit: float | None


def _classify_product(symbol: str, product_type: str) -> tuple[bool, bool]:
    """Return (is_option, is_future) flags for the statutory charge engine."""
    upper = symbol.upper()
    if upper.endswith(("CE", "PE")) and any(ch.isdigit() for ch in upper):
        return True, False
    if is_derivative_symbol(symbol) and product_type.upper() != "DELIVERY":
        return False, True
    return False, False


def run_backtest(
    symbol: str,
    conditions: list[dict],
    side: str = "BUY",
    quantity: int = 1,
    timeframe: str = "5m",
    days: int = 30,
    capital: float = 100_000.0,
    product_type: str = "INTRADAY",
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    slippage_pct: float | None = None,
    seed: int | None = None,
    candles: list[dict] | None = None,
) -> dict:
    """Execute a truthful, charge-aware backtest and return a full report."""
    canonical, exchange = resolve_symbol(symbol)
    is_option, is_future = _classify_product(canonical, product_type)

    # Lot-size compliance: normalise quantity to a tradable multiple.
    # Requests below one lot are floored to a single lot (the exchange
    # minimum), never zero.
    validation = validate_quantity(canonical, max(1, int(quantity)), auto_correct=True)
    qty = validation.corrected_quantity
    lot_warning = validation.warning
    if qty <= 0:
        min_lot = LOT_SIZES.get(canonical, 1)
        qty = min_lot
        lot_warning = (
            f"Requested quantity {quantity} is below one lot of {canonical}; "
            f"using minimum tradable size 1 lot ({min_lot} qty)"
        )

    if candles is None:
        candles = generate_candles(canonical, days=days, timeframe=timeframe, seed=seed)
    n_bars = len(candles)
    if n_bars < 30:
        return {
            "engine": "TradeThrone Truthful Backtester v1",
            "symbol": canonical,
            "error": f"Not enough bars ({n_bars}) to backtest - need >= 30",
            "trades": [],
            "equity_curve": [],
            "metrics": {},
        }

    evaluator = StrategyEvaluator()
    direction = side.upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")

    sl_frac = stop_loss_pct / 100.0 if stop_loss_pct else None
    tp_frac = take_profit_pct / 100.0 if take_profit_pct else None

    open_trade: OpenTrade | None = None
    trades: list[dict] = []
    equity_curve: list[dict] = []
    equity = capital
    peak_equity = capital
    max_drawdown_pct = 0.0
    holding_bars_total = 0
    breakdown_totals = {
        "brokerage": 0.0,
        "stt": 0.0,
        "exchange_transaction_charge": 0.0,
        "gst": 0.0,
        "stamp_duty": 0.0,
        "sebi_fee": 0.0,
        "slippage_cost": 0.0,
    }

    def _leg(side_label: str, price: float) -> dict:
        return TransactionCharges.calculate_charges(
            symbol=canonical,
            side=side_label,
            quantity=qty,
            price=price,
            product_type=product_type,
            exchange=exchange,
            is_option=is_option,
            is_future=is_future,
            slippage_pct=slippage_pct,
        )

    def _close_trade(exit_idx: int, exit_price: float, reason: str) -> None:
        nonlocal equity, peak_equity, max_drawdown_pct, holding_bars_total, open_trade
        t = open_trade

        # Statutory charges per executed leg (exact, not proportional)
        if t.side == "BUY":
            buy_leg = _leg("BUY", t.entry_price)
            sell_leg = _leg("SELL", exit_price)
            gross = (exit_price - t.entry_price) * t.quantity
        else:
            sell_leg = _leg("SELL", t.entry_price)
            buy_leg = _leg("BUY", exit_price)
            gross = (t.entry_price - exit_price) * t.quantity

        total_charges = round(buy_leg["total_charges"] + sell_leg["total_charges"], 2)
        for k in breakdown_totals:
            breakdown_totals[k] += buy_leg[k] + sell_leg[k]

        net = round(gross - total_charges, 2)
        equity = round(equity + net, 2)
        holding_bars_total += exit_idx - t.entry_idx

        trades.append({
            "entry_ts": candles[t.entry_idx]["ts"],
            "entry_index": t.entry_idx,
            "exit_ts": candles[exit_idx]["ts"],
            "exit_index": exit_idx,
            "side": t.side,
            "quantity": t.quantity,
            "lots": validation.lots,
            "lot_size": LOT_SIZES.get(canonical, 1),
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(exit_price, 2),
            "gross_pnl": round(gross, 2),
            "charges": total_charges,
            "net_pnl": net,
            "return_on_capital_pct": round(net / capital * 100.0, 4),
            "holding_bars": exit_idx - t.entry_idx,
            "exit_reason": reason,
        })

        equity_curve.append({"ts": candles[exit_idx]["ts"], "equity": equity})
        if equity > peak_equity:
            peak_equity = equity
        dd = (
            (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
        )
        if dd > max_drawdown_pct:
            max_drawdown_pct = dd

        open_trade = None

    # ── Main event loop ──────────────────────────────────────────────
    for i, bar in enumerate(candles):
        evaluator.update_price(canonical, bar["close"])

        if open_trade is not None:
            # Intrabar stop-loss / take-profit checks against OHLC extremes
            if open_trade.stop_loss is not None:
                hit_sl = (
                    bar["low"] <= open_trade.stop_loss
                    if open_trade.side == "BUY"
                    else bar["high"] >= open_trade.stop_loss
                )
                if hit_sl:
                    _close_trade(i, open_trade.stop_loss, "STOP_LOSS")
                    continue
            if open_trade.take_profit is not None:
                hit_tp = (
                    bar["high"] >= open_trade.take_profit
                    if open_trade.side == "BUY"
                    else bar["low"] <= open_trade.take_profit
                )
                if hit_tp:
                    _close_trade(i, open_trade.take_profit, "TAKE_PROFIT")
                    continue
        else:
            # Entry uses the same evaluator state machine as live trading
            if evaluator.evaluate("backtest", canonical, conditions):
                entry_price = bar["close"]
                sl: float | None = None
                tp: float | None = None
                if sl_frac is not None:
                    sl = (
                        entry_price * (1 - sl_frac)
                        if direction == "BUY"
                        else entry_price * (1 + sl_frac)
                    )
                if tp_frac is not None:
                    tp = (
                        entry_price * (1 + tp_frac)
                        if direction == "BUY"
                        else entry_price * (1 - tp_frac)
                    )
                open_trade = OpenTrade(
                    entry_idx=i,
                    entry_price=entry_price,
                    quantity=qty,
                    side=direction,
                    stop_loss=sl,
                    take_profit=tp,
                )

    # Force-close any surviving position at the last close (mark-to-market)
    if open_trade is not None:
        _close_trade(n_bars - 1, candles[-1]["close"], "END_OF_DATA")

    # ── Metrics ──────────────────────────────────────────────────────
    net_pnls = [t["net_pnl"] for t in trades]
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p <= 0]
    gross_pnl = round(sum(t["gross_pnl"] for t in trades), 2)
    total_charges = round(sum(t["charges"] for t in trades), 2)
    net_pnl = round(sum(net_pnls), 2)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (
        round(gross_win / gross_loss, 3) if gross_loss > 0
        else (round(gross_win, 3) if gross_win > 0 else 0.0)
    )

    returns = [t["return_on_capital_pct"] for t in trades]
    mean_ret = statistics.fmean(returns) if returns else 0.0
    std_ret = statistics.stdev(returns) if len(returns) > 1 else 0.0

    # Annualisation: average holding horizon -> estimated trades/year
    avg_hold = holding_bars_total / len(trades) if trades else 0.0
    tf_min = TIMEFRAME_MINUTES[timeframe.lower()]
    bars_per_year = TRADING_DAYS_PER_YEAR * MINUTES_PER_DAY / tf_min
    trades_per_year = (bars_per_year / avg_hold) if avg_hold > 0 else 0.0
    sharpe = (
        round(mean_ret / std_ret * math.sqrt(trades_per_year), 3)
        if std_ret > 0 and trades_per_year > 0
        else 0.0
    )

    logger.info(
        "Backtest complete: %s tf=%s bars=%d trades=%d net=%.2f charges=%.2f",
        canonical, timeframe, n_bars, len(trades), net_pnl, total_charges,
    )

    return {
        "engine": "TradeThrone Truthful Backtester v1",
        "symbol": canonical,
        "exchange": exchange,
        "timeframe": timeframe,
        "bars": n_bars,
        "days_requested": days,
        "seed": seed,
        "data_source": "deterministic-seeded-synthetic",
        "product_type": product_type,
        "capital": capital,
        "requested_quantity": quantity,
        "quantity": qty,
        "lot_size": LOT_SIZES.get(canonical, 1),
        "lot_warning": lot_warning,
        "metrics": {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2) if trades else 0.0,
            "gross_pnl": gross_pnl,
            "total_charges": total_charges,
            "net_pnl": net_pnl,
            "net_return_pct": round(net_pnl / capital * 100.0, 3),
            "charges_as_pct_of_gross": (
                round(total_charges / abs(gross_pnl) * 100.0, 2) if gross_pnl else 0.0
            ),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "profit_factor": profit_factor,
            "avg_win": round(statistics.fmean(wins), 2) if wins else 0.0,
            "avg_loss": round(statistics.fmean(losses), 2) if losses else 0.0,
            "expectancy_per_trade_pct": round(mean_ret, 4),
            "sharpe_annualised": sharpe,
            "avg_holding_bars": round(avg_hold, 1),
            "final_equity": round(capital + net_pnl, 2),
        },
        "charges_breakdown": {k: round(v, 2) for k, v in breakdown_totals.items()},
        "equity_curve": equity_curve,
        "trades": trades,
    }