# STRATEGY CONDITION CONTRACT — Production Incident Audit

**Date:** 2026-09-17 · **Branch:** `feat/autonomous-os`

## Incident

Render production logs showed a repeating::

    KeyError: 'value'
      trading_engine._process_tick
      → strategy_evaluator.evaluate
      → threshold = float(cond["value"])

for MSFT/NVDA — the symbols of enabled seed strategies
("Golden Cross SMA Trend 50/200" → AAPL/NVDA/MSFT; "Bollinger Band
Volatility Breakout" → NVDA/MSFT).

## Root cause

`TradingEngine._load_strategies()` loaded `conditions_json` from the
database as **raw dicts with zero contract enforcement**, and
`StrategyEvaluator.evaluate()` blindly indexed `cond["value"]`.  Any legacy
row persisted before the API's `Condition.normalize_condition` validator
existed (e.g. conditions saved with a `threshold` key, or without a numeric
`value`) raised `KeyError: 'value'` on **every tick**.  The per-tick
containment (Phase 15) kept the engine alive, but the same traceback
repeated forever — the strategy never evaluated and never diagnosed why.

Secondary defects found while tracing:

1. `_load_strategies` cleared the strategy cache and then parsed rows
   inside one `try` — a single corrupt row aborted the whole loop and left
   the engine with **zero** strategies (silent total strategy loss).
2. `operator = ... or "gt"` fabricated a `gt` operator for empty/unknown
   operators (invented evaluation semantics).
3. `_execute_signal` read `action["side"]`/`action["quantity"]`
   unvalidated — the same crash class existed for malformed action JSON.
4. The agent evaluator (`agent_control._decide_symbol`) called the same
   evaluator with raw JSON.

## Fix (fail-closed typed contract)

New module **`app/engine/conditions.py`** — the single source of truth for
the persisted condition/action contract:

- `ConditionRule` / `ActionRule` — typed, immutable validated payloads.
- `normalize_condition(s)` / `normalize_action` — validation with
  structured `ConditionValidationError` diagnostics (condition index,
  reason, safe payload summary).
- Only **proven-valid** legacy aliases are accepted (`threshold`→`value`,
  symbolic operators `>`/`>=`/…, indicator aliases `BB_UPPER`/`BB_LOWER`/
  `BB_MID`/`BOLLINGER_*` — each was already handled somewhere in the
  codebase before this module existed).  A missing/NaN/inf/non-numeric
  `value` is a **contract violation — never defaulted, never guessed**.

Wiring:

- `trading_engine._load_strategies` — **per-row containment**; malformed
  conditions/actions AND corrupt JSON are quarantined at load with a
  structured ERROR log (strategy id, owner, diagnostic) and never reach
  the tick pipeline; healthy rows always load.  Loaded conditions are
  typed rules (fast path — no per-tick re-validation).
- `strategy_evaluator.evaluate` — normalizes raw dicts defensively,
  consumes typed rules directly, logs a de-duplicated structured ERROR
  (bounded key set) and returns False (fail closed).
- `agent_control._decide_symbol` — contract violations produce a durable
  NO_TRADE decision with `risk_result=INVALID_CONDITIONS` and the
  structured diagnostic in `risk_reason`.
- `backtester.run_backtest` — validates conditions up front; malformed
  payloads return HTTP 422 instead of crashing mid-run.
- `app/core/metrics.py` — new Prometheus counter
  `tradetron_strategy_condition_errors_total{stage}` (engine_load /
  evaluator / agent_decision / backtest).
- `GET /api/health` — now reports `engine_strategies_loaded` so operators
  can see quarantine effects without grepping rotating logs.

## Tests

- `tests/test_condition_contract.py` — the exact production failing shapes
  (missing/null/non-numeric/NaN/inf value, unknown indicator/operator,
  non-dict conditions, empty list), no-fabrication guarantees, structured
  diagnostics, proven-alias backward compatibility, action contract,
  backtester 422 contract.
- `tests/test_engine_condition_quarantine.py` — end-to-end engine load
  with healthy + broken + corrupt-JSON rows: quarantine + structured
  logs + typed rules + tick pipeline never raises for MSFT/NVDA + healthy
  strategy still fires.
- Existing suites re-verified: evaluator symbolic-operator tests, engine
  tick-survival tests, agent control/autonomous e2e, P0 remediation.

## Related incident: Binance HTTP 451 (verified, no change needed)

Root cause is environmental: Binance geo-blocks cloud-provider IP ranges
(HTTP 451 "Unavailable For Legal Reasons"), so `stream.binance.com` is
unreachable from Render.  The codebase already handles this honestly:
`CryptoStreamMarketDataProvider` reports **UNAVAILABLE** (never labels
synthetic/delayed data as live) and the CoinGecko fallback is explicitly
gated and always labelled DELAYED
(`app/market_data/providers/crypto_stream.py`, documented decision).
