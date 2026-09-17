"""Regression tests for the production KeyError('value') incident.

Render logs (2026-09) showed a repeating::

    KeyError: 'value'
      trading_engine._process_tick
      → strategy_evaluator.evaluate
      → threshold = float(cond["value"])

for MSFT/NVDA — the enabled seed strategies' symbols.  Root cause:
strategies loaded from the DB were evaluated as raw dicts with NO contract
enforcement; any legacy row saved with ``threshold`` instead of ``value``
(or missing ``value`` entirely) crashed on every tick.

These tests pin the fail-closed contract: malformed conditions NEVER
evaluate, NEVER invent a threshold, and produce structured diagnostics —
while every previously-valid shape keeps evaluating exactly as before.
"""

from __future__ import annotations

import json

import pytest

from app.engine.conditions import (
    ConditionValidationError,
    normalize_action,
    normalize_condition,
    normalize_conditions,
)
from app.engine.strategy_evaluator import StrategyEvaluator


# ── Exact production failing shapes ────────────────────────────────────


class TestProductionIncidentShapes:
    """The exact malformed shapes that crashed the Render deployment."""

    def test_condition_missing_value_fails_closed(self):
        """Legacy row: {'indicator', 'operator'} with NO value at all."""
        evaluator = StrategyEvaluator()
        symbol = "MSFT"
        for price in (100.0, 101.0, 102.0):
            evaluator.update_price(symbol, price)
        # Previously raised KeyError('value') here.
        assert evaluator.evaluate(
            "prod-strat", symbol,
            [{"indicator": "PRICE", "operator": "gt"}],
        ) is False

    def test_condition_threshold_alias_is_accepted(self):
        """Proven legacy alias: 'threshold' was accepted by the API schema
        validator and the legacy _row_to_read normalizer."""
        rules = normalize_conditions(
            [{"indicator": "PRICE", "operator": "gt", "threshold": 90.0}]
        )
        assert rules[0].value == 90.0

    def test_condition_value_null_fails_closed(self):
        evaluator = StrategyEvaluator()
        evaluator.update_price("NVDA", 500.0)
        assert evaluator.evaluate(
            "prod-strat", "NVDA",
            [{"indicator": "PRICE", "operator": "gt", "value": None}],
        ) is False

    def test_condition_value_non_numeric_fails_closed(self):
        evaluator = StrategyEvaluator()
        evaluator.update_price("NVDA", 500.0)
        assert evaluator.evaluate(
            "prod-strat", "NVDA",
            [{"indicator": "PRICE", "operator": "gt", "value": "expensive"}],
        ) is False

    def test_condition_value_nan_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="finite"):
            normalize_condition(
                {"indicator": "PRICE", "operator": "gt", "value": float("nan")}
            )

    def test_condition_value_inf_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="finite"):
            normalize_condition(
                {"indicator": "PRICE", "operator": "gt", "value": float("inf")}
            )

    def test_condition_missing_indicator_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="indicator"):
            normalize_condition({"operator": "gt", "value": 10.0})

    def test_condition_missing_operator_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="operator"):
            normalize_condition({"indicator": "PRICE", "value": 10.0})

    def test_condition_unknown_indicator_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="unsupported indicator"):
            normalize_condition(
                {"indicator": "ASTROLOGY", "operator": "gt", "value": 1.0}
            )

    def test_condition_unknown_operator_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="unsupported operator"):
            normalize_condition(
                {"indicator": "PRICE", "operator": "approx", "value": 1.0}
            )

    def test_condition_non_dict_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="JSON object"):
            normalize_conditions(["price > 100"])

    def test_empty_conditions_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="empty"):
            normalize_conditions([])

    def test_conditions_not_a_list_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="JSON array"):
            normalize_conditions({"indicator": "PRICE"})


# ── No fabricated values ───────────────────────────────────────────────


class TestNoFabricatedValues:
    """The evaluator must never guess a replacement threshold."""

    def test_missing_value_is_never_defaulted_to_zero(self):
        """value=0 with operator gt would evaluate PRICE > 0 — always true —
        which would silently turn a broken strategy into a firehose of
        orders.  It must instead fail closed."""
        evaluator = StrategyEvaluator()
        evaluator.update_price("MSFT", 420.0)
        fired = evaluator.evaluate(
            "s", "MSFT", [{"indicator": "PRICE", "operator": "gt"}]
        )
        assert fired is False

    def test_no_hidden_operator_default(self):
        """An unsupported operator must fail closed, not coerce to 'gt'."""
        with pytest.raises(ConditionValidationError):
            normalize_condition(
                {"indicator": "PRICE", "operator": "", "value": 10.0}
            )



# ── Structured diagnostics ─────────────────────────────────────────────


class TestStructuredDiagnostics:
    def test_error_carries_index_and_reason(self):
        try:
            normalize_conditions(
                [
                    {"indicator": "PRICE", "operator": "gt", "value": 100.0},
                    {"indicator": "RSI", "operator": "lt"},  # broken
                ]
            )
        except ConditionValidationError as exc:
            payload = exc.to_dict()
            assert payload["condition_index"] == 1
            assert "value" in payload["reason"]
            assert "indicator" in payload["condition"]
        else:
            pytest.fail("ConditionValidationError not raised")

    def test_context_is_prefixed(self):
        with pytest.raises(ConditionValidationError, match="strategy abc-123"):
            normalize_conditions(
                [{"indicator": "PRICE", "operator": "gt"}],
                context="strategy abc-123",
            )


# ── Backward compatibility (proven-valid shapes keep working) ─────────


class TestBackwardCompatibility:
    def test_symbolic_operators_still_evaluate(self):
        evaluator = StrategyEvaluator()
        symbol = "SYM"
        for price in (105.0, 106.8):
            evaluator.update_price(symbol, price)
        assert evaluator.evaluate(
            "s1", symbol, [{"indicator": "PRICE", "operator": ">", "value": 105.0}]
        )
        assert evaluator.evaluate(
            "s2", symbol, [{"indicator": "PRICE", "operator": ">=", "value": 106.8}]
        )

    def test_canonical_shapes_still_evaluate(self):
        evaluator = StrategyEvaluator()
        symbol = "SYM"
        for price in (100.0, 101.0, 99.5):
            evaluator.update_price(symbol, price)
        assert evaluator.evaluate(
            "s", symbol,
            [{"indicator": "PRICE", "operator": "gt", "value": 99.0, "period": 14}],
        )

    def test_typed_rules_take_fast_path(self):
        from app.engine.conditions import ConditionRule

        evaluator = StrategyEvaluator()
        evaluator.update_price("X", 100.0)
        rule = ConditionRule(
            indicator="PRICE", operator="gt", value=50.0, period=14
        )
        assert evaluator.evaluate("s", "X", [rule]) is True


# ── Action contract ────────────────────────────────────────────────────


class TestActionContract:
    def test_valid_action(self):
        rule = normalize_action({"side": "BUY", "quantity": 10})
        assert rule.side == "BUY"
        assert rule.quantity == 10
        assert rule.order_type == "MARKET"

    def test_action_missing_side_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="side"):
            normalize_action({"quantity": 10})

    def test_action_missing_quantity_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="quantity"):
            normalize_action({"side": "BUY"})

    def test_action_zero_quantity_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="positive"):
            normalize_action({"side": "BUY", "quantity": 0})

    def test_action_bad_order_type_fails_closed(self):
        with pytest.raises(ConditionValidationError, match="order_type"):
            normalize_action({"side": "BUY", "quantity": 1, "order_type": "YOLO"})


# ── Loader quarantine contract (per-row containment) ──────────────────


class TestLoaderQuarantine:
    def test_broken_row_raises_with_context_healthy_row_loads(self):
        """Exactly what TradingEngine._load_strategies does per row: the
        broken row raises with a diagnosable context and is quarantined;
        the healthy row normalizes cleanly."""
        broken_raw = [{"indicator": "PRICE", "operator": "gt"}]  # no value
        with pytest.raises(ConditionValidationError, match="strategy broken-1"):
            normalize_conditions(broken_raw, context="strategy broken-1")

        healthy = [
            {"indicator": "PRICE", "operator": "gt", "value": 100.0}
        ]
        rules = normalize_conditions(healthy, context="strategy ok-1")
        assert len(rules) == 1
        assert rules[0].value == 100.0

    def test_engine_module_imports_loader_helpers(self):
        from app.engine.trading_engine import TradingEngine  # noqa: F401

        from app.engine import conditions as cond_mod

        assert hasattr(cond_mod, "normalize_conditions")
        assert hasattr(cond_mod, "normalize_action")


# ── Backtester contract ────────────────────────────────────────────────


class TestBacktesterContract:
    def test_backtest_rejects_malformed_conditions(self):
        from app.engine.backtester import run_backtest

        with pytest.raises(ValueError, match="Invalid strategy conditions"):
            run_backtest(
                symbol="RELIANCE",
                conditions=[{"indicator": "PRICE", "operator": "gt"}],
            )

    def test_backtest_accepts_valid_conditions(self):
        from app.engine.backtester import run_backtest

        report = run_backtest(
            symbol="RELIANCE",
            conditions=[
                {"indicator": "PRICE", "operator": "gt", "value": 1.0, "period": 5}
            ],
            days=1,
        )
        assert report["symbol"] == "RELIANCE"
