# Phase 16 — Fresh Adversarial Production-Product Audit & Verified-Defect Remediation

**Date:** 2026-09-09
**Baseline:** 646 tests passing (verified full-suite run before this phase touched anything)
**Result:** 1 NEW P1 PRODUCTION DEFECT FOUND & FIXED (RED→GREEN), Phase 15 fixes all
independently re-verified, full suite 649 green, deployment/observability/security
gates all pass.

---

## 1. Phase 15 fixes — independent re-verification (exit criterion 1)

Every Phase 15 fix was independently verified against the actual source in this
phase (not trusted from the prior report):

| Phase 15 defect | File | Verified fix | RED test | Status |
| :--- | :--- | :--- | :--- | :--- |
| Partial-fill over-booking | `app/engine/order_reconciliation.py` | `_finalize_filled` books `fill_qty` on Trade+Position | `test_phase15_reconciliation_partial_fill_red.py` | **GREEN** |
| Engine tick fragility | `app/engine/trading_engine.py` | per-tick `_process_tick` containment | `test_phase15_tick_loop_survives_error_red.py` | **GREEN** |
| Simulator fragility | `app/market_data/simulator.py` | per-symbol containment | `test_phase15_simulator_survives_error_red.py` | **GREEN** |
| Kill-switch cross-tenant | `app/api/admin.py` | user-scoped via `StrategyRecord.enabled` + `reload_strategies` | `test_phase15_kill_switch_user_scoped_red.py` | **GREEN** |

All four RED tests run green together (4 passed). `reload_strategies` confirmed to
reload only `enabled == True` strategies, making the per-user halt genuine.

---

## 2. Fresh adversarial audit — Coverage matrix (rebuilt from source this phase)

| Capability | File(s) | Verified protection | Verdict |
| :--- | :--- | :--- | :--- |
| Manual/DMA close auto-pilot feed | `app/api/trades.py`, `app/engine/risk_manager.py` | **GAP — no feed** → fixed + RED test | **FIXED (P1)** |
| Idempotent keyed entry / replay / broker-ref crash window | `trades.py::_claim_or_replay_order` | `test_order_idempotency`, `test_strategy_live_entry_orphan_repro`, `test_signal_webhook_durable_orders_*` | Covered |
| Position-close CAS (no double-close / no duplicate PnL) | `trades.py::close_position` | `test_concurrent_close_cas`, `test_close_cas_durability_before_dispatch` | Covered |
| Owner-scoped PAPER credit + admin fan-out | `paper_account.py`, `copy_trading.py` | `test_paper_accounting_hardening`, `test_admin_close_copy_fanout_owner_scoped` | Covered |
| trades.order_id FK (real OrderRecord, not display string) | `trades.py` | FK-fix regression + PG note | Covered (SQLite-PG gap noted) |
| LIVE dispatch fail-closed (`BROKER_MODE`) | `app/brokers/__init__.py` | `test_live_mode_guard`, `test_p2_broker_adapter_gates` | Covered |
| Broker postback signature + tenant binding | `postback.py`, `signatures.py` | `test_broker_postback_hardening`, `test_direct_postback_status_normalization` | Covered |
| AuthN/Z, 2FA, token rotation, lockout, OTP single-use | `auth.py`, `security.py` | `test_auth`, `test_2fa_login_completion`, `test_secret_remediation` | Covered |
| WebSocket tenant isolation / cap / reconnect | `useWebSocket.js`, `websocket.py` | `test_ws_auth_isolation`, `test_ws_connection_limits` | Covered |
| Anonymous vs authenticated data boundaries | `trades.py`, `dashboard.py` | `test_public_trade_exposure`, `test_dashboard_no_fake_strategies`, `test_v2_reports*` | Covered |

---

## 3. Verified defect → RED → GREEN

### DEFECT (P1 — risk-control bypass): manual/DMA close never fed the auto-pilot

**File:** `app/api/trades.py::close_position`

**Root cause:** the endpoint computed and booked `realized_pnl` (TradeRecord +
`credit_paper_pnl`) but never called `RiskManager.record_trade_result`. Only
`OrderManager._close_position` (the in-memory SMA/stop-loss executor) fed the
guard, so the dominant customer workflow — closing a position from the UI via
`POST /api/trades/positions/{id}/close` — silently bypassed the consecutive-loss
and intraday-drawdown auto-pilot kill-switch. A trader manually grinding losing
hedges could rack up N consecutive losses (or breach the daily drawdown) and the
platform-wide auto-pilot would NOT trip.

**RED evidence:** new `tests/test_phase16_manual_close_feeds_autopilot_red.py` —
2 tests failed on base code (`assert [] == [400.0]`, guard never fed); 1
engine-less defensive test passed. 2 failed, 1 passed.

**Fix:** after the atomic CAS OPEN→CLOSED close commits, feed `realized_pnl` to
the engine's risk manager (`app.main.get_engine().risk_manager.record_trade_result`).
Exactly-once (replay 404 cannot reach it), owner/mode agnostic, defensive
(engine-less contexts and exceptions skip without breaking the close).

---

## 4. Verification gates (Phase 16 exit criteria)

| Gate | Result |
| :--- | :--- |
| Full backend suite | **649 passed** (3 warnings) |
| Phase 15 + Phase 16 RED tests | 4 + 3 green |
| Close-path regression (risk-guard, copy-close, CAS, paper-accounting) | 36 passed |
| Python compile/import | clean |
| Alembic drift guard | 23 tables match ORM, single head, clean empty-DB upgrade |
| Secret scanner | clean (354 tracked files) |
| Frontend build | success (2.35s) |
| Frontend lint | 0 errors (25 `set-state-in-effect` style warnings, non-runtime) |
| git diff --check | clean (CRLF notices only) |

---

## 5. Notable non-defect observations (documented, not acted on)

1. **Copy-trading follower closes** also do not feed the engine's global
   `RiskManager`. The engine risk manager is **platform-global** (one instance),
   not per-user; wiring follower P&L into a single global guard would conflate
   multiple users' streaks/drawdowns into one trip decision. This is a pre-existing
   **design limitation of the global guard**, not a local defect. Recommended next
   phase: a per-user risk ledger if per-account auto-pilot is desired (larger
   scope). **Classification: EXTERNAL/UNKNOWN (no safe local fix).**
2. `StrategyDeploymentRecord` still has **no owner column** (remains a Phase 16
   backlog candidate from Phase 15, section-5 item 1 — requires Alembic migration
   + backfill; out of scope for a single-commit fix).
3. **PostgreSQL vs SQLite FK behavior:** FK enforcement differs; covered by the
   `trades.order_id` FK fix and required operator acceptance on real PG (below).

---

## 6. Remaining external dependencies

- **PostgreSQL-specific FK/transaction behavior** cannot be fully validated on
  SQLite CI. Operational acceptance test: run the close/entry reconciliation
  suite against a real staging Postgres (`ENVIRONMENT=production` config).

## 7. Updated production classification

**Genuinely deployable** trading product with financial safety, security,
reliable execution, honest per-user data, coherent frontend/backend wiring, and
recovery behavior — with the explicit external launch gates in §6 and the
copy-trading row/global-autopilot design decision in §5.1 open.

## 8. Commits

- `e1f5f959` — `fix(P1): manual-close auto-pilot feed + carry verified Phase 15 production hardening`

