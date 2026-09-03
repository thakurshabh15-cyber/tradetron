"""Strategy condition evaluator.

Refactored from the original ``strategy_engine.py``.  This module evaluates
whether a set of conditions (indicator + operator + threshold) are satisfied
given a price tick.

Supports:
  - Comparison operators: lt, lte, gt, gte, eq
  - Crossover operators: cross_above, cross_below
  - Indicators: PRICE (direct), RSI, SMA, EMA (calculated from history)

All evaluation functions are pure (no side-effects) except the rolling
history, which is managed per-strategy-per-symbol.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Any

from app.core.logging import get_logger

logger = get_logger("engine.evaluator")

# Maximum price history kept per symbol for indicator calculation
_MAX_HISTORY = 1000

# Symbolic operator aliases accepted in stored strategy conditions (legacy data
# created before normalization persisted raw operators such as ">", ">=", ...).
# Mirrors the aliases handled by ``visual_strategy.VisualContext._compare`` so
# DB-loaded conditions behave identically regardless of the form they were
# saved in.
_OPERATOR_ALIASES: dict[str, str] = {
    ">": "gt",
    "<": "lt",
    ">=": "gte",
    "<=": "lte",
    "=": "eq",
    "==": "eq",
}


class StrategyEvaluator:
    """Evaluates strategy conditions against incoming market ticks."""

    def __init__(self) -> None:
        # price history per symbol:  { "AAPL": deque([225.3, 225.4, ...]) }
        self._history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=_MAX_HISTORY)
        )
        # previous indicator values for crossover detection
        self._prev_values: dict[str, float] = {}

    def update_price(self, symbol: str, price: float) -> None:
        """Record a new price in the rolling history."""
        self._history[symbol].append(price)

    def evaluate(
        self,
        strategy_id: str,
        symbol: str,
        conditions: list[dict[str, Any]],
    ) -> bool:
        """Return True if ALL conditions are satisfied for the given symbol.

        Parameters
        ----------
        strategy_id:
            Unique strategy identifier (used to namespace crossover state).
        symbol:
            The ticker symbol being evaluated.
        conditions:
            List of condition dicts with keys: indicator, operator, value, period.
        """
        for cond in conditions:
            indicator = cond["indicator"].upper()
            raw_operator = cond["operator"].lower()
            operator = _OPERATOR_ALIASES.get(raw_operator, raw_operator)
            threshold = float(cond["value"])
            period = int(cond.get("period", 14))

            current = self._compute_indicator(symbol, indicator, period)
            if current is None:
                return False  # Not enough data yet

            prev_key = f"{strategy_id}:{symbol}:{indicator}:{period}"
            previous = self._prev_values.get(prev_key)

            matched = self._compare(operator, current, threshold, previous)

            # Store current as previous for next evaluation
            self._prev_values[prev_key] = current

            if not matched:
                return False

        return True

    # ── Indicator calculations ───────────────────────────────────────

    def _compute_indicator(
        self, symbol: str, indicator: str, period: int
    ) -> float | None:
        """Compute the requested indicator value from price history."""
        history = self._history.get(symbol)
        if not history:
            return None

        if indicator == "PRICE":
            return history[-1]

        if len(history) < period:
            return None  # Not enough data for the lookback

        prices = list(history)[-period:]

        if indicator == "SMA":
            return sum(prices) / len(prices)

        if indicator == "EMA":
            return self._ema(prices)

        if indicator == "RSI":
            return self._rsi(list(history)[-(period + 1):])

        if indicator == "MACD":
            return self._macd(list(history))

        if indicator in ("BOLLINGER", "BOLLINGER_UPPER", "BB_UPPER"):
            return self._bollinger(prices)[0]

        if indicator in ("BOLLINGER_LOWER", "BB_LOWER"):
            return self._bollinger(prices)[1]

        if indicator in ("BOLLINGER_MID", "BB_MID"):
            return self._bollinger(prices)[2]

        if indicator == "ATR":
            return self._atr(list(history)[-(period + 1):])

        logger.warning("Unknown indicator: %s", indicator)
        return None

    @staticmethod
    def _ema(prices: list[float]) -> float:
        """Exponential Moving Average."""
        k = 2 / (len(prices) + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema

    @staticmethod
    def _rsi(prices: list[float]) -> float | None:
        """Relative Strength Index (Wilder's smoothing)."""
        if len(prices) < 2:
            return None

        gains, losses = [], []
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))

        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    @classmethod
    def _macd(cls, prices: list[float], fast: int = 12, slow: int = 26) -> float | None:
        """Moving Average Convergence Divergence (MACD line)."""
        if len(prices) < slow:
            return None
        fast_ema = cls._ema(prices[-fast:])
        slow_ema = cls._ema(prices[-slow:])
        return round(fast_ema - slow_ema, 4)

    @staticmethod
    def _bollinger(prices: list[float], num_std: float = 2.0) -> tuple[float, float, float]:
        """Bollinger Bands (Upper, Lower, Middle SMA)."""
        mid = sum(prices) / len(prices)
        variance = sum((p - mid) ** 2 for p in prices) / len(prices)
        std = math.sqrt(variance)
        upper = round(mid + (num_std * std), 2)
        lower = round(mid - (num_std * std), 2)
        return upper, lower, round(mid, 2)

    @staticmethod
    def _atr(prices: list[float]) -> float | None:
        """Average True Range (volatility measure)."""
        if len(prices) < 2:
            return None
        true_ranges = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
        return round(sum(true_ranges) / len(true_ranges), 4)

    # ── Comparison logic ─────────────────────────────────────────────

    @staticmethod
    def _compare(
        operator: str,
        current: float,
        threshold: float,
        previous: float | None,
    ) -> bool:
        """Evaluate a single operator comparison."""
        if operator == "lt":
            return current < threshold
        if operator == "lte":
            return current <= threshold
        if operator == "gt":
            return current > threshold
        if operator == "gte":
            return current >= threshold
        if operator == "eq":
            return math.isclose(current, threshold, rel_tol=1e-6)
        if operator == "cross_above":
            return previous is not None and previous <= threshold < current
        if operator == "cross_below":
            return previous is not None and previous >= threshold > current

        logger.warning("Unknown operator: %s", operator)
        return False
