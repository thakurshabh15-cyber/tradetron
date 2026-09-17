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
from app.engine.conditions import (
    ConditionRule,
    ConditionValidationError,
    is_condition_rule,
    normalize_conditions,
)
from app.core.metrics import record_condition_validation_error

logger = get_logger("engine.evaluator")

# Maximum price history kept per symbol for indicator calculation
_MAX_HISTORY = 1000

# Bound on the per-evaluator condition-violation dedup keys (one key per
# broken (strategy, reason) pair) so long-running processes stay bounded.
_MAX_CONDITION_ERROR_KEYS = 1024

# Symbolic operator aliases accepted in stored strategy conditions (legacy data
# created before normalization persisted raw operators such as ">", ">=", ...).
# The authoritative alias tables now live in app.engine.conditions; these
# shims keep the module-level names importable for existing tests.
from app.engine.conditions import OPERATOR_ALIASES as _OPERATOR_ALIASES  # noqa: E402,F401


class StrategyEvaluator:
    """Evaluates strategy conditions against incoming market ticks."""

    def __init__(self) -> None:
        # price history per symbol:  { "AAPL": deque([225.3, 225.4, ...]) }
        self._history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=_MAX_HISTORY)
        )
        # previous indicator values for crossover detection
        self._prev_values: dict[str, float] = {}
        # de-dup of condition-contract violation diagnostics, keyed
        # f"{strategy_id}:{reason}" — see _log_condition_failure.
        self._condition_error_seen: set[str] = set()

    def update_price(self, symbol: str, price: float) -> None:
        """Record a new price in the rolling history."""
        self._history[symbol].append(price)

    def evaluate(
        self,
        strategy_id: str,
        symbol: str,
        conditions: list[Any],
    ) -> bool:
        """Return True if ALL conditions are satisfied for the given symbol.

        Parameters
        ----------
        strategy_id:
            Unique strategy identifier (used to namespace crossover state).
        symbol:
            The ticker symbol being evaluated.
        conditions:
            Typed :class:`ConditionRule` list (preferred — the engine and the
            agent layer validate at load time) or raw condition dicts, which
            are validated here against the typed contract.

        Fail-closed semantics
        ---------------------
        A condition that violates the typed contract (e.g. the legacy
        production rows that raised ``KeyError: 'value'``) is NEVER evaluated
        with an invented threshold.  The whole strategy evaluates to False
        and a structured diagnostic is logged once per (strategy, reason)
        so a permanently-broken strategy cannot spam the logs on every tick.
        """
        if not conditions:
            return False

        try:
            rules = self._coerce_rules(conditions)
        except ConditionValidationError as exc:
            self._log_condition_failure(strategy_id, symbol, exc)
            return False

        for rule in rules:
            current = self._compute_indicator(symbol, rule.indicator, rule.period)
            if current is None:
                return False  # Not enough data yet

            prev_key = f"{strategy_id}:{symbol}:{rule.indicator}:{rule.period}"
            previous = self._prev_values.get(prev_key)

            matched = self._compare(rule.operator, current, rule.value, previous)

            # Store current as previous for next evaluation
            self._prev_values[prev_key] = current

            if not matched:
                return False

        return True

    # ── Typed-condition intake ───────────────────────────────────────

    @staticmethod
    def _coerce_rules(conditions: list[Any]) -> list[ConditionRule]:
        """Accept pre-validated rules (fast path) or validate raw dicts."""
        if all(is_condition_rule(cond) for cond in conditions):
            return list(conditions)
        return normalize_conditions(conditions)

    def _log_condition_failure(
        self,
        strategy_id: str,
        symbol: str,
        exc: ConditionValidationError,
    ) -> None:
        """Structured, de-duplicated diagnostic for a contract violation.

        One bad strategy must not kill the engine (per-tick containment
        already guarantees that) and must not flood production logs with an
        identical traceback on every tick — the first occurrence per
        (strategy, reason) is logged at ERROR with the full diagnostic;
        repeats are counted at DEBUG.
        """
        key = f"{strategy_id}:{exc.reason}"
        if key not in self._condition_error_seen:
            self._condition_error_seen.add(key)
            self._prune_condition_error_seen()
            record_condition_validation_error("evaluator")
            logger.error(
                "Strategy condition contract violation — strategy FAILS "
                "CLOSED (never evaluated, no invented threshold): "
                "strategy_id=%s symbol=%s diagnostic=%s",
                strategy_id,
                symbol,
                exc.to_dict(),
            )
        else:
            logger.debug(
                "Strategy condition contract violation (repeat): "
                "strategy_id=%s symbol=%s diagnostic=%s",
                strategy_id,
                symbol,
                exc.to_dict(),
            )

    def _prune_condition_error_seen(self) -> None:
        """Bound the dedup key set so long runs cannot grow it unboundedly."""
        if len(self._condition_error_seen) > _MAX_CONDITION_ERROR_KEYS:
            # Drop the oldest half (sets are unordered; keep it simple and
            # just shrink — a repeat will log once more after pruning).
            for key in list(self._condition_error_seen)[
                : -(_MAX_CONDITION_ERROR_KEYS // 2)
            ]:
                self._condition_error_seen.discard(key)


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
