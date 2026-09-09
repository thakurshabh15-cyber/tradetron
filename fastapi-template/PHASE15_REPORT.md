# Phase 15 — Adversarial Test Coverage Gap Analysis & Verified-Defect Remediation

**Date:** 2026-09-09
**Baseline:** 642 tests passing (verified, full suite run before this phase touched anything)
**Result:** 4 REGRESSION TEST FILES ADDED (RED/GREEN), 4 PRODUCTION FILES FIXED,
full suite regression clean.

---

## 1. Correction of the working notes (critical)

The phase-15 working notes carried forward from the previous session contained
**phantom findings** that this session verified as **not present** in the
codebase. These were noted as "CRITICAL GAP" items; each was re-checked against
the actual source and the record is corrected below. **No code was changed on
the basis of phantom findings.**

| Prior-session claim | Verification result |
| :--- | :--- |
| `trading_engine.execute_with_retry` (tenacity, 3 attempts, no dead-letter/`on_giveup`) | **Does not exist.** No `execute_with_retry` symbol in the repo; `tenacity` is not a dependency anywhere. The only retry-adjacent code is CoinGecko's 429 `Retry-After` handling in `app/market_data/providers/crypto.py`. |
| `trading_engine.sync_account_state` timezone bug (`datetime.utcnow()` lines 47/83/93) | **Does not exist.** No `sync_account_state` method; a repo-wide `datetime.utcnow()` search returns **zero** matches. Every `_utcnow()` default (models, order_manager, handlers) uses `datetime.now(timezone.utc)`. |
| `trades.py` copy-trade "30s cooldown" edge case | **Does not exist.** No cooldown logic in `app/engine/copy_trading.py`. The only "cooldown" in the repo is the 60 s OTP resend window in `app/api/auth.py`. |
| `admin.py` line-217 `datetime.utcnow()` tz-naive cleanup bug | **Does not exist.** `admin.py` consistently uses `datetime.now(timezone.utc)`; line 217 is password-strength validation. |
| "Hardcoded 100 MB DB-estimate" in admin DB stats | **Does not exist.** `app/api/admin.py` computes metrics strictly from live DB aggregates (`count`/`sum`); no hardcoded size estimate found. |
| `place_signal` / `get_all_positions` / `get_all_pending_orders` public methods | **Do not exist.** `TradingEngine` exposes `start`, `stop`, `reload_strategies`, plus internal tick/signal/persistence methods; positions/orders are reached via `OrderManager` and `app/api/trades.py`. |

**Lesson:** the previous session's "coverage matrix" claimed two confirmed gaps
for retry-exhaustion and reconciliation-tz. Both were false positives; the
matrix was rebuilt from direct source reads this session.

---

## 2. Coverage matrix (rebuilt from source)

| Production-critical capability | Production file(s) | Test protection | Coverage verdict |
| :--- | :--- | :--- | :--- |
| LIVE/PAPER dispatch guard (`BROKER_MODE`) | `app/brokers/__init__.py` | `test_live_mode_guard`, `test_live_routing_uncovered_paths`, `test_p2_broker_adapter_gates`, `test_staging_brokers` | **Covered** |
| Durable pre-dispatch claim → broker ref → CAS finalize (manual/DMA/strategy/copy/webhook) | `app/engine/durable_claims.py` | `test_order_idempotency`, `test_strategy_live_entry_orphan_repro`, `test_copy_trading_durable_claim`, `test_signal_webhook_durable_orders_{red,green}` | **Covered** |
| Broker-acceptance crash-window reconciliation (Window-B/C) | `app/engine/order_reconciliation.py` | `test_order_reconciliation`, `test_position_normalization`, `test_postback_reconciliation_race` | **Covered** — partial-fill accounting **was missing → now added** |
| Concurrent close CAS (no double-close / no duplicate PnL) | `app/api/trades.py`, `app/engine/copy_trading.py` | `test_concurrent_close_cas`, `test_close_cas_durability_before_dispatch`, `test_close_race_red_repro` | **Covered** |
| Owner-scoped PnL credit + admin-close fan-out | `app/api/trades.py`, `app/engine/paper_account.py` | `test_paper_accounting_hardening`, `test_admin_close_copy_fanout_owner_scoped`, `test_p3a_position_ownership` | **Covered** |
| Webhook HMAC + fail-closed + worker PEL recovery | `app/webhooks/**` | `test_staging_webhook_config`, `test_webhooks_integration`, `test_signal_webhook_durable_orders_*` | **Covered** |
| Engine tick loop resilience | `app/engine/trading_engine.py` | — no loop-survival test existed | **GAP → fixed + RED test added** |
| Market-data simulator loop resilience | `app/market_data/simulator.py` | — no loop-survival test existed | **GAP → fixed + RED test added** |
| Admin user kill-switch user scoping | `app/api/admin.py` | `test_admin_governance` (status-200 only) | **GAP → cross-tenant defect found, fixed + RED test added** |
| Reconciliation partial-fill quantity bookkeeping | `app/engine/order_reconciliation.py` | — no partial-fill case existed | **GAP → defect found, fixed + RED test added** |
| Position valuation / PnL math on close | `app/api/trades.py` | `test_concurrent_close_cas`, `test_paper_accounting_hardening` | **Covered** |
| AuthN/Z (jail/password rules/2FA/token rotate/rate-limit) | `app/api/auth.py`, `app/core/security.py` | `test_auth`, `test_production_auth`, `test_2fa_login_completion`, `test_secret_remediation` | **Covered** |
| Payments (Razorpay fail-closed, idempotent verify) | `app/api/billing.py`, `app/core/payment_gateway.py` | `test_billing_and_payments`, `test_payment_forgery_failclosed`, `test_p3b_payment_idempotency` | **Covered** |
| WebSocket tenant isolation / limits | `app/api/websocket.py` | `test_ws_auth_isolation`, `test_ws_connection_limits`, `test_websocket_events` | **Covered** |
| Anonymous trade-tape exposure (public view) | `app/api/trades.py` | `test_public_trade_exposure`, `test_v2_reports_dashboard_strategies_isolation` | **Covered** |
| DB schema / migrations / startup gate | `app/db/**` | `test_staging_db`, `test_startup_migrations`, `test_production_schema`, `test_p3c_schema_ownership` | **Covered** |
| Observability (500 JSON contract, metrics, sentinel) | `app/core/monitoring.py`, middlewares | `test_p14_observability` | **Covered** |
---

## 3. Verified defects → RED → GREEN

### DEFECT 1 (P1 — financial correctness): reconciliation over-books partial fills
`order_reconciliation._finalize_filled` set the CAS-updated order's
`filled_quantity` from the broker read, but created the **TradeRecord and
PositionRecord with `order.quantity` (the full requested qty)**. A broker-
confirmed partial fill (`filled_quantity=4` of an order of 10) booked a
**10-unit position** — violating the codebase's own invariant `M. Partial fill —
never over-book from partial info` (pinned in `test_copy_trading_durable_claim`).

- RED test: `tests/test_phase15_reconciliation_partial_fill_red.py`
  (`Trade booked 10 while broker confirmed 4` — failed on base code).
- Fix: book `fill_qty` on both Trade and Position in `_finalize_filled`.
- GREEN: passes after fix.

### DEFECT 2 (P1 — resilience): engine tick loop died on any per-tick error
`TradingEngine._tick_loop` caught only `asyncio.CancelledError`. A single
transient failure in `_persist_trade` (or broadcast/strategy evaluation)
propagated out and killed the engine's **only** processing task — `_running`
stayed `True` but autonomous strategy execution silently stopped until process
restart. This contradicted the codebase's own loop contract
(`order_reconciliation._run_pass`: *"a per-cycle failure never crashes the
loop"*; `WorkerPool._worker_loop`).

- RED test: `tests/test_phase15_tick_loop_survives_error_red.py`
  (`task died after a single transient per-tick error` — failed on base code).
- Fix: extracted the per-tick pipeline into `TradingEngine._process_tick` and
  contained `_tick_loop` around it (log + continue), CancelledError exempted.
- GREEN: passes after fix.

### DEFECT 3 (P1 — same resilience class): market-data simulator
`MarketSimulator._run` had the identical flaw — a transient `ws_manager.broadcast`
failure killed the whole simulated market-data stream permanently.

- RED test: `tests/test_phase15_simulator_survives_error_red.py`
- Fix: per-symbol try/except (log + continue), CancelledError exempted.
- GREEN: passes after fix. (`broker_cron._run_loop` already contained its errors
  at line 303 — no change needed there.)

### DEFECT 4 (P1 — cross-tenant operational): `POST /api/admin/kill-switch/user/{user_id}`
Documented and broadcast as a **single-user** halt, but executed
`update(StrategyDeploymentRecord).where(status == "RUNNING")` — **no owner
filter** — pausing **every user's** deployments platform-wide. Worse, deployment
rows carry **no owner column**, and the engine only honors
`StrategyRecord.enabled`; so the endpoint neither computed a true per-user
count nor actually halted the target's engine dispatch.

- RED test: `tests/test_phase15_kill_switch_user_scoped_red.py`
- Fix: scope to `StrategyRecord.user_id == user_id` (the engine-honored lever,
  same as `pause_strategy` for all of the user's strategies), drop the
  platform-wide deployment sweep, and `reload_strategies()` so the halt takes
  effect immediately.
- GREEN: passes after fix.

---

## 4. Final regression

- Baseline before phase: **642 passed**.
- Full suite after fixes with the first three new regression files:
  **645 passed**.
- A second full run over all **646 collected** tests validated the final state:
  **646 passed** (run log confirms all 4 new files included).
- Focused suites re-run green: `test_order_reconciliation`,
  `test_position_normalization`, `test_postback_reconciliation_race`,
  `test_strategy_live_entry_orphan_repro`, `test_copy_trading_durable_claim`,
  `test_strategy_executor`, `test_order_manager`, `test_live_vs_paper_execution`,
  `test_paper_accounting_hardening`, `test_p1_safety`, `test_p14_observability`,
  `test_webhooks_integration`, `test_signal_webhook_durable_orders_green`,
  `test_market_data`, `test_historical_candles`, `test_market_freshness`,
  `test_multi_asset_feeds`, `test_websocket_events`, `test_admin_governance`,
  `test_admin_close_copy_fanout_owner_scoped`, `test_marketplace`,
  `test_dashboard_*`, `test_v2_reports_dashboard_strategies_isolation`.

### New regression files (this phase)
1. `tests/test_phase15_reconciliation_partial_fill_red.py`
2. `tests/test_phase15_tick_loop_survives_error_red.py`
3. `tests/test_phase15_simulator_survives_error_red.py`
4. `tests/test_phase15_kill_switch_user_scoped_red.py`

### Production files changed (this phase)
1. `app/engine/order_reconciliation.py` — partial-fill quantity bookkeeping.
2. `app/engine/trading_engine.py` — tick-loop containment (`_process_tick`).
3. `app/market_data/simulator.py` — per-symbol error containment.
4. `app/api/admin.py` — user kill-switch scoped to the target user + engine reload.
---

## 5. Prioritized remediation list (remaining recommendations)

| Priority | Item | Risk if unaddressed | Suggested owner |
| :--- | :--- | :--- | :--- |
| P1 | Add an **owner column** to `strategy_deployments` (Alembic migration + backfill from `POST /api/strategies/{id}/deploy`, which currently stores no owner) so deployments are tenant-scoped end-to-end. | Admin/copy `StrategyDeploymentRecord` surfaces cannot be user-scoped; platform-wide sweeps / per-user halts remain ambiguous. | Backend / Migrations |
| P1 | Wire `RiskManager.record_trade_result` (auto-pilot feed) into manual/DMA **close** paths — the position-close P&L in `app/api/trades.py` currently does not feed the consecutive-loss/drawdown auto-pilot rules. | Auto-pilot kill-switch may not trip on manual losses. | Backend / Engine |
| P2 | Extend `test_phase15_tick_loop_survives_error_red.py` to also exercise a `_process_tick` **broadcast** failure (already covered structurally by the simulator test) and a mid-loop strategy-removal re-entry. | Future refactors of `_process_tick` regress silently. | Backend / QA |
| P2 | Establish the documented Alembic migration baseline for the new per-user deployment column with a PG parity check (EXTERNAL DEPENDENCY — PG-specific verification is out of CI scope). | Postgres-only FK/index bugs remain invisible in SQLite CI. | Backend / DB |
| P2 | Observe a real broker partial fill and formalize Window-B semantics in the reconciliation docstring (book confirmed qty vs stay PENDING) once real partial-fill payloads are seen. | Ambiguity in crash-window behavior on real partial fills. | Backend / Broker |

---

## 6. External-dependency note

Production is Supabase PostgreSQL; CI runs SQLite. All fixes and tests in this
phase are dialect-neutral (SQLAlchemy `update`/CAS patterns already proven under
both), but PG-specific verification remains an **external dependency**.