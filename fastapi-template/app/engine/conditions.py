"""Typed strategy-condition contract (engine-side validation layer).

This module is the SINGLE source of truth for what a persisted strategy
condition may look like when it reaches the evaluation engines
(``StrategyEvaluator``, the agent evaluator and the backtester).

Why it exists
-------------
Production logs (Render, 2026-09) showed a repeating ``KeyError: 'value'``
from ``StrategyEvaluator.evaluate`` for MSFT/NVDA.  Root cause: strategies
were loaded from the database as raw JSON dicts and evaluated without any
contract enforcement — any legacy row whose conditions were saved with a
``threshold`` key (or without ``value`` at all) crashed on every tick.  The
API layer (``app.schemas.trading.Condition``) silently coerces missing values
to ``0.0`` for new rows, which *masked* the contract gap instead of closing
it.

Contract
--------
A persisted condition is a JSON object with:

- ``indicator``  — one of the canonical indicators below (aliases accepted,
  proven-valid legacy forms only),
- ``operator``   — one of the canonical operators below (symbolic aliases
  such as ``">"`` accepted — they were persisted by legacy versions),
- ``value``      — a finite, real number (``threshold`` accepted as the
  proven legacy alias used by both the API schema and the legacy
  ``_row_to_read`` normalizer),
- ``period``     — integer in ``[1, 1000]`` (optional, default 14).

Validation is FAIL-CLOSED: a condition that does not satisfy the contract
raises :class:`ConditionValidationError` with structured diagnostics
(condition index, reason and a safe summary of the offending payload).
Callers must quarantine the offending strategy — never invent a threshold,
never default a missing value, never silently skip a condition.

Every accepted legacy mapping here is *proven*: it was already handled
somewhere in the codebase before this module existed
(``_OPERATOR_ALIASES``, ``Condition.normalize_condition``,
``_row_to_read`` op/indicator maps, ``_compute_indicator`` aliases).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from app.core.logging import get_logger

logger = get_logger("engine.conditions")

# ── Canonical vocabularies ──────────────────────────────────────────────

# Indicators the evaluator can actually compute (app.engine.strategy_evaluator.
# _compute_indicator).  Aliases → canonical name.  Anything outside this map
# was ALREADY failing closed in the evaluator ("Unknown indicator" → None →
# no trade); normalizing it to an explicit error adds the missing diagnostics.
INDICATOR_ALIASES: dict[str, str] = {
    "PRICE": "PRICE",
    "RSI": "RSI",
    "SMA": "SMA",
    "EMA": "EMA",
    "MACD": "MACD",
    "ATR": "ATR",
    "BOLLINGER_UPPER": "BOLLINGER_UPPER",
    "BB_UPPER": "BOLLINGER_UPPER",
    "BOLLINGER_LOWER": "BOLLINGER_LOWER",
    "BB_LOWER": "BOLLINGER_LOWER",
    "BOLLINGER_MID": "BOLLINGER_MID",
    "BB_MID": "BOLLINGER_MID",
}

# Comparison / crossover operators the evaluator implements.  Symbolic
# aliases mirror the legacy _OPERATOR_ALIASES table (rows persisted before
# normalization stored raw ">", "<", ">=", "<=", "=", "==").
OPERATOR_ALIASES: dict[str, str] = {
    "lt": "lt",
    "lte": "lte",
    "gt": "gt",
    "gte": "gte",
    "eq": "eq",
    "cross_above": "cross_above",
    "cross_below": "cross_below",
    ">": "gt",
    "<": "lt",
    ">=": "gte",
    "<=": "lte",
    "=": "eq",
    "==": "eq",
}

# Action contract (mirrors app.schemas.trading.Action).
ACTION_SIDES: frozenset[str] = frozenset({"BUY", "SELL"})
ACTION_ORDER_TYPES: frozenset[str] = frozenset({"MARKET", "LIMIT"})


class ConditionValidationError(Exception):
    """A persisted strategy condition/action violates the typed contract.

    Carries structured diagnostics so callers can quarantine the offending
    strategy and an operator can fix the data — without leaking anything
    sensitive and without the engine guessing a replacement value.
    """

    def __init__(
        self,
        reason: str,
        *,
        index: int | None = None,
        raw_summary: str | None = None,
    ) -> None:
        self.reason = reason
        self.index = index
        self.raw_summary = raw_summary
        super().__init__(reason)

    def to_dict(self) -> dict[str, Any]:
        """Structured diagnostic payload for logs / API surfaces."""
        payload: dict[str, Any] = {"reason": self.reason}
        if self.index is not None:
            payload["condition_index"] = self.index
        if self.raw_summary is not None:
            payload["condition"] = self.raw_summary
        return payload


@dataclass(frozen=True, slots=True)
class ConditionRule:
    """A validated, immutable condition ready for evaluation."""

    indicator: str
    operator: str
    value: float
    period: int
    source_index: int = -1

    def as_dict(self) -> dict[str, Any]:
        return {
            "indicator": self.indicator,
            "operator": self.operator,
            "value": self.value,
            "period": self.period,
        }


@dataclass(frozen=True, slots=True)
class ActionRule:
    """A validated, immutable trigger action ready for the order pipeline."""

    side: str
    quantity: int
    order_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
        }


# ── Diagnostics helpers ────────────────────────────────────────────────


def _safe_summary(raw: Any, limit: int = 200) -> str:
    """Best-effort, truncated repr of an offending payload for diagnostics.

    Condition payloads come from the strategy builder (indicator/operator/
    value/period) — no secrets — but the summary is still length-capped and
    exception-proof so logging can never blow up on exotic types.
    """
    try:
        text = repr(raw)
    except Exception:  # pragma: no cover — repr of exotic objects
        return "<unprintable>"
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


PERIOD_MIN = 1
PERIOD_MAX = 1000
PERIOD_DEFAULT = 14


# ── Normalization ──────────────────────────────────────────────────────


def normalize_condition(raw: Any, index: int = 0) -> ConditionRule:
    """Validate ONE condition dict and return a typed :class:`ConditionRule`.

    Raises :class:`ConditionValidationError` (fail-closed) when the payload
    cannot satisfy the contract.  No default threshold is ever invented.
    """
    if not isinstance(raw, Mapping):
        raise ConditionValidationError(
            "condition must be a JSON object",
            index=index,
            raw_summary=_safe_summary(raw),
        )

    has_indicator = "indicator" in raw
    has_operator = "operator" in raw
    if not has_indicator and not has_operator:
        raise ConditionValidationError(
            "condition is missing both 'indicator' and 'operator'",
            index=index,
            raw_summary=_safe_summary(raw),
        )
    if not has_indicator:
        raise ConditionValidationError(
            "condition is missing 'indicator'",
            index=index,
            raw_summary=_safe_summary(raw),
        )
    if not has_operator:
        raise ConditionValidationError(
            "condition is missing 'operator'",
            index=index,
            raw_summary=_safe_summary(raw),
        )

    raw_indicator = raw["indicator"]
    if not isinstance(raw_indicator, str):
        raise ConditionValidationError(
            f"'indicator' must be a string, got {type(raw_indicator).__name__}",
            index=index,
            raw_summary=_safe_summary(raw),
        )
    indicator = INDICATOR_ALIASES.get(raw_indicator.strip().upper())
    if indicator is None:
        raise ConditionValidationError(
            f"unsupported indicator {raw_indicator!r}",
            index=index,
            raw_summary=_safe_summary(raw),
        )

    raw_operator = raw["operator"]
    if not isinstance(raw_operator, str):
        raise ConditionValidationError(
            f"'operator' must be a string, got {type(raw_operator).__name__}",
            index=index,
            raw_summary=_safe_summary(raw),
        )
    operator = OPERATOR_ALIASES.get(raw_operator.strip().lower())
    if operator is None:
        raise ConditionValidationError(
            f"unsupported operator {raw_operator!r}",
            index=index,
            raw_summary=_safe_summary(raw),
        )

    # Value: proven legacy alias 'threshold' → 'value' (both the API schema
    # validator and the legacy _row_to_read normalizer accepted it).  A
    # missing value is a CONTRACT VIOLATION — never defaulted, never guessed.
    if raw.get("value") is not None:
        raw_value = raw["value"]
    elif raw.get("threshold") is not None:
        raw_value = raw["threshold"]
    else:
        raise ConditionValidationError(
            "condition is missing a numeric 'value' (legacy 'threshold' "
            "alias accepted)",
            index=index,
            raw_summary=_safe_summary(raw),
        )
    if isinstance(raw_value, bool):
        raise ConditionValidationError(
            "'value' must be a real number, got a boolean",
            index=index,
            raw_summary=_safe_summary(raw),
        )
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise ConditionValidationError(
            f"'value' must be a real number, got {raw_value!r}",
            index=index,
            raw_summary=_safe_summary(raw),
        ) from None
    if not math.isfinite(value):
        raise ConditionValidationError(
            f"'value' must be finite, got {raw_value!r}",
            index=index,
            raw_summary=_safe_summary(raw),
        )

    # Period: optional with the historical default of 14, bounded to the
    # same [1, 1000] range the API schema enforces for new strategies.
    if raw.get("period") is not None:
        try:
            period = int(raw["period"])
        except (TypeError, ValueError):
            raise ConditionValidationError(
                f"'period' must be an integer, got {raw['period']!r}",
                index=index,
                raw_summary=_safe_summary(raw),
            ) from None
        if not (PERIOD_MIN <= period <= PERIOD_MAX):
            raise ConditionValidationError(
                f"'period' must be within [{PERIOD_MIN}, {PERIOD_MAX}], "
                f"got {period}",
                index=index,
                raw_summary=_safe_summary(raw),
            )
    else:
        period = PERIOD_DEFAULT

    return ConditionRule(
        indicator=indicator,
        operator=operator,
        value=value,
        period=period,
        source_index=index,
    )



def normalize_conditions(
    conditions: Any,
    *,
    context: str = "",
) -> list[ConditionRule]:
    """Validate a persisted condition list into typed rules (fail-closed).

    ``context`` is a short human-readable scope string (e.g. the strategy
    id) used only to make the raised error diagnosable.
    """
    if not isinstance(conditions, (list, tuple)):
        raise ConditionValidationError(
            f"conditions must be a JSON array, got {type(conditions).__name__}",
            raw_summary=_safe_summary(conditions)[:80],
        )
    if not conditions:
        raise ConditionValidationError(
            "conditions array is empty — a strategy must carry at least "
            "one condition",
            raw_summary="[]",
        )
    rules: list[ConditionRule] = []
    for index, raw in enumerate(conditions):
        try:
            rules.append(normalize_condition(raw, index=index))
        except ConditionValidationError as exc:
            if context:
                raise ConditionValidationError(
                    f"{context}: {exc.reason}",
                    index=exc.index,
                    raw_summary=exc.raw_summary,
                ) from exc
            raise
    return rules


def normalize_action(action: Any) -> ActionRule:
    """Validate a persisted strategy action (side/quantity/order_type).

    Mirrors ``app.schemas.trading.Action`` so a strategy whose *action* half
    is malformed is quarantined with the same rigor as a malformed condition
    — ``_execute_signal`` previously crashed per-tick on
    ``KeyError: 'side'``/``KeyError: 'quantity'`` for such rows.
    """
    if not isinstance(action, Mapping):
        raise ConditionValidationError(
            "action must be a JSON object",
            raw_summary=_safe_summary(action),
        )
    side = action.get("side")
    if not isinstance(side, str) or side.strip().upper() not in ACTION_SIDES:
        raise ConditionValidationError(
            f"action 'side' must be one of {sorted(ACTION_SIDES)}, got {side!r}",
            raw_summary=_safe_summary(action),
        )
    raw_quantity = action.get("quantity")
    if raw_quantity is None:
        raise ConditionValidationError(
            "action is missing 'quantity'",
            raw_summary=_safe_summary(action),
        )
    if isinstance(raw_quantity, bool):
        raise ConditionValidationError(
            "action 'quantity' must be a positive integer, got a boolean",
            raw_summary=_safe_summary(action),
        )
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        raise ConditionValidationError(
            f"action 'quantity' must be a positive integer, got {raw_quantity!r}",
            raw_summary=_safe_summary(action),
        ) from None
    if quantity <= 0:
        raise ConditionValidationError(
            f"action 'quantity' must be positive, got {quantity}",
            raw_summary=_safe_summary(action),
        )
    order_type = str(action.get("order_type", "MARKET")).strip().upper()
    if order_type not in ACTION_ORDER_TYPES:
        raise ConditionValidationError(
            f"action 'order_type' must be one of {sorted(ACTION_ORDER_TYPES)}, "
            f"got {order_type!r}",
            raw_summary=_safe_summary(action),
        )
    return ActionRule(
        side=side.strip().upper(),
        quantity=quantity,
        order_type=order_type,
    )


def is_condition_rule(condition: Any) -> bool:
    """True when ``condition`` is already a validated :class:`ConditionRule`."""
    return isinstance(condition, ConditionRule)

