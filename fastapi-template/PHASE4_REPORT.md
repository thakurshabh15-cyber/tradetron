# TradeThrone — Phase 4: Environment Separation & Production Safety Verification Report

**Date:** 2026-09-04
**Scope:** Backend (`app/`), deploy configs, and test suite in `fastapi-template/`.

This report documents the completed **environment separation** and **production
safety audit** for Phase 4. It verifies that a STAGING/DEPLOYED instance cannot
accidentally reach production resources or dispatch real broker orders, that
secrets are handled hygienically, and that every order-dispatch path is guarded
or safe-by-construction.

---

## 1. Executive Summary

| Area | Result |
| :--- | :--- |
| Live-broker dispatch audit (8 paths) | ✅ All guarded or safe-by-construction |
| STAGING → PRODUCTION resource isolation | ✅ Verified (isolated DB port, simulated broker, single-env loading) |
| Secret hygiene | ✅ Committed `.env.production` contained **only placeholders** — nothing to rotate |
| Fail-safe defaults (source) | ✅ `broker_mode=simulated`, `environment=development`, local SQLite |
| Automated test coverage added | ✅ 21 new tests (13 env-separation + 8 live-routing) |
| Final regression | ✅ **187 passed / 0 failed** (was 166 baseline) |

---

## 2. Order-Dispatch Path Audit (Complete)

All eight order-dispatch paths are verified below. "Guarded" means the path
calls `assert_live_dispatch_allowed()`, which refuses to reach a real broker
unless `BROKER_MODE=live` was set at startup **and** a live broker is connected.

| # | Path | Location | Status | Detail |
| :--- | :--- | :--- | :--- | :--- |
| 1 | REST order | `app/api/trades.py` (`place_order`) | ✅ Guarded | `assert_live_dispatch_allowed()` |
| 2 | Close position | `app/api/trades.py` | ✅ Guarded | idem |
| 3 | DMA | `app/api/trades.py` (DMA path) | ✅ Guarded | idem |
| 4 | Strategy engine | `app/engine/trading_engine.py` | ✅ Guarded | `assert_live_dispatch_allowed()` before `execute` |
| 5 | Webhook signal | `app/webhooks/handlers/tradethrone_signal.py` → `order_manager.place_order` | ✅ Safe by construction | Uses the engine's startup broker (SimulatedBroker unless `BROKER_MODE=live`); no user-supplied LIVE override can reach the broker |
| 6 | `visual_strategy.execute_legs` | `app/engine/visual_strategy.py` | ✅ Latent only | Takes an arbitrary `broker` arg and calls `broker.place_order()`, but **zero callers exist** — not wired to any dispatch path (see §5) |
| 7 | Copy-trading fan-out | `app/engine/copy_trading.py` | ✅ N/A | **Never dispatches to a broker** — persists FILLED records to DB only, even in LIVE mode (see §5) |
| 8 | `broker_cron.py` | `app/engine/broker_cron.py` | N/A | Session/TOTP renewal only — no order dispatch |

Note: `app/engine/order_manager.py` was **not modified** (0-diff constraint
maintained) — it remains the single guarded choke point for all engine-originated
order dispatches.

---

## 3. Environment Separation Verification

### 3.1 Config loads a SINGLE env file — never a sibling secret file

`app/config.py` (`Settings`) loads **only** `BASE_DIR / ".env"` via
`SettingsConfigDict`. It does **not** read `.env.staging` or `.env.production`,
and the `ENVIRONMENT` var does **not** switch which file is loaded. A staging
process therefore can never quietly boot with production secrets that happen to
sit in a sibling file.

- `app/config.py:161` — `env_file = BASE_DIR / ".env"` (single file, hard-coded).
- Verified by `tests/test_env_separation.py::test_settings_loads_only_dotenv_not_staging_or_production`
  and `test_no_env_selection_based_on_environment_var`.

### 3.2 STAGING cannot point at PRODUCTION resources

| Resource | STAGING value | Why it's isolated |
| :--- | :--- | :--- |
| Broker | `BROKER_MODE=simulated` (`.env.staging` + `render.yaml` default) | No real order can be placed from a staging host |
| Database | Local SQLite / `localhost` dev DB (`.env.staging`) | Never a production hostname |
| Postgres port | `5433:5432` in `docker-compose.staging.yml` | Dedicated host port — cannot clobber a local/production `5432` |
| DB name/user/password | `tradetron_staging` / `tradetron` / `tradetron_staging_password` | Self-identifying, clearly non-production credentials |

### 3.3 Fail-safe source defaults (a bare boot is never production)

- `broker_mode` **defaults to `simulated`** (never `live`) — a process that
  forgets to set the var cannot reach a real broker.
- `environment` **defaults to `development`** (never `production`) — so the
  fail-fast production guards only activate when production is explicitly chosen.
- `database_url` **defaults to local SQLite** (`sqlite+aiosqlite:///...`) — a
  bare boot never touches a remote database.

### 3.4 Production boot fail-fast guards

When `environment == "production"` is explicitly set, `app/config.py` fails fast
on weak secrets (short/placeholder `JWT_SECRET`) and refuses to silently skip
webhook signature verification unless `SKIP_SIGNATURE_VERIFICATION` is
deliberately true. (`tests/test_env_separation.py::test_production_fail_fast_guards_present`)

---

## 4. Secret Hygiene

### 4.1 CRITICAL CORRECTION

Earlier notes flagged that `.env.production` was committed and "needed
rotation." **This is not the case.** The committed file contained **ONLY
placeholders/redactions**, not live credentials:

- `sk_live_xxxxxxxx` (Stripe live key — redacted template, not a real key)
- `rzp_live_xxxxxxxx` (Razorpay live key — redacted template)
- `<DB_PASSWORD>` (unresolved placeholder)
- A shell-command placeholder (not a secret string) for `JWT_SECRET`
- `<your-db>.upstash.io` (example host, not a real Upstash endpoint)

**No secret rotation is required.** The issue was hygiene only.

### 4.2 Remediation status

- `.env.production` is **staged for deletion** in git (status `D`) and is
  covered by `.gitignore`, so it cannot be re-added on a normal commit.
- `.env`, `.env.staging`, `.env.production` are all git-ignored.
- `.env.staging` contains only self-identifying test values
  (`staging_test_jwt_secret_do_not_use`, `rzp_test_tradetron_staging`) and is
  not loaded by the app.
- Frontend uses only `VITE_*` vars (safe — only API URLs reach the browser);
  `client/src/config.js` hard-codes a `PROD_API_URL` fallback. **No backend
  secret reaches the browser.**

Verified by `tests/test_env_separation.py::test_production_env_git_history_contains_no_real_credentials`
and `test_production_env_staged_for_deletion_and_gitignored` (the latter checks
the committed blob from git history has no real-looking live key).



---

## 5. LIVE ≠ PAPER Routing Verification (previously-uncovered paths)

Three dispatch paths had no explicit guard and no live caller. They are now
verified to be safe **by construction**, rather than by a guard.

### 5.1 Webhook signal handler → engine startup broker

`tradethrone_signal.py` obtains its order manager via
`from app.main import get_engine()` → `engine._order_manager`, then calls
`order_manager.place_order(...)`. The broker inside that order manager is fixed
at engine-construction time from `BROKER_MODE` (in `main.py` lifespan:
SimulatedBroker unless `BROKER_MODE=live`). A webhook payload carries **order
fields only** — there is no broker/account/`live` selection field — so an
attacker or misconfigured caller **cannot** smuggle a LIVE override into the
dispatch path.

- Tests: `test_live_routing_uncovered_paths.py::test_webhook_signal_uses_engine_order_manager_not_user_broker`
  and `test_webhook_signal_handler_has_no_user_broker_input`.

### 5.2 Copy-trading fan-out → DB-only (never reaches a broker)

`copy_trading.py` mirrors master orders to followers by persisting
`OrderRecord`/`TradeRecord`/`PositionRecord` **directly to the database** with
`status="FILLED"`. It holds **no broker object** and **never calls
`broker.place_order()`** — even when `mode == "LIVE"`. This is a **paper
simulation even in LIVE mode**.

```text
CopyTradingEngine  →  DB bookkeeping (FILLED records)   ✗ no broker dispatch
```

- Tests: `test_copy_trading_module_has_no_broker_dispatch_calls`,
  `test_copy_trading_engine_holds_no_broker`, and
  `test_copy_trading_executor_persists_filled_without_a_broker` (drives the
  innermost executor with a LIVE-mode follower and confirms FILLED records are
  persisted with no broker involved).

### 5.3 `visual_strategy.execute_legs` → latent, unwired primitive

`app/engine/visual_strategy.py::execute_legs(broker, ...)` takes an arbitrary
`broker` and calls `broker.place_order()`. A codebase-wide search proves it has
**zero callers** — the only occurrence is its own definition — and the module's
global `visual_strategy_engine` singleton is **unused**. The only cross-module
imports of the visual-strategy code reference the **model** (`VisualStrategyRecord`,
for CRUD), never the engine/dispatcher. Consequently nothing can invoke it with
a live broker today.

- Tests: `test_visual_strategy_execute_legs_has_no_callers`,
  `test_visual_strategy_engine_singleton_is_unused`, and
  `test_visual_strategy_is_not_imported_as_a_dispatch_path`.

> Defense-in-depth recommendation: give `execute_legs` the same
> `assert_live_dispatch_allowed()` guard (or remove the unused singleton) so a
> future caller can't wire it to a real broker accidentally.

---

## 6. Test Inventory (new in Phase 4)

### `tests/test_env_separation.py` (13 tests)
- Single `.env` loading; no env-file switching by `ENVIRONMENT`
- `.env.production` staged-for-deletion and git-ignored; `.env`/`.env.staging` ignored
- Committed `.env.production` blob contains only placeholders (no real live keys)
- STAGING `BROKER_MODE=simulated`, isolated local DB, non-default postgres port
  (`5433`), `render.yaml` simulated default
- Fail-safe source defaults (simulated broker, non-production environment, local
  SQLite) and production fail-fast guards

### `tests/test_live_routing_uncovered_paths.py` (8 tests)
- Webhook uses engine startup broker; payload has no broker/live field
- Copy trading: no broker calls in source, no broker attribute on engine, and a
  functional drive of the innermost executor proving FILLED records are
  persisted with no broker
- `visual_strategy.execute_legs` has zero callers; its singleton is unused; the
  engine module is not imported as a dispatch path

---

## 7. Final Regression

Full suite run with the `fastapi-template/.venv` interpreter:

```text
187 passed, 0 failed   (166 baseline + 21 new)
```

All previously-existing suites (live-mode guard, staging safety, staging
webhook config, staging DB, live-vs-paper execution, copy trading, webhook
integration, risk guard, order manager, brokerage, etc.) continue to pass —
no regressions introduced.

---

## 8. Remaining Gaps / Recommendations

1. **Copy trading is bookkeeping-only (functional gap, not a safety gap).** In
   LIVE mode it persists FILLED records but never places a real broker order.
   If true live copy-fan-out is intended, wire it through `OrderManager` +
   `assert_live_dispatch_allowed()` so it becomes a *guarded* dispatch rather
   than silently skipping the broker.
2. **`visual_strategy.execute_legs`** should gain the live-dispatch guard (or
   the unused `visual_strategy_engine` singleton should be removed) as
   defense-in-depth.
3. **Commit the staged `.env.production` deletion** so the tracked tree no
   longer contains even the placeholder file.
4. Keep `app/engine/order_manager.py` frozen (0-diff) — it is the single
   guarded choke point; any future broker dispatch path must route through it.

