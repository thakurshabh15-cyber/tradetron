# TRADETRON — Autonomous-OS Launch Prep / Integration-Readiness Audit (Phases A–K)

> **Branch:** `feat/autonomous-os`
> **Date:** 2026-09-15
> **Audited baseline:** `35591d0a5b5cffdb2b23e7b9680484c753a0c428` (HEAD == origin, tree clean at audit time)
> **Document commit:** `219671f8` (docs only — audit + DEPLOYMENT.md pre-flight count fix)
> **Operational posture:** `BROKER_MODE=simulated` — LIVE trading disabled at every dispatch gate.
> **Classification:** 🟢 GREEN (verified locally / in suite) · ⛔ BLOCKED (external infra/credentials) · 🟡 AMBER (locally verified, external verification pending)

---

## 0. Baseline Gates (Phase A) — ALL GREEN

| Gate | Command | Result |
|---|---|---|
| Full backend suite | `pytest -q --no-header` (BROKER_MODE=simulated) | **907 passed**, 0 failed, 3 warnings (726 s) |
| Frontend suite | `vitest run` | **71 passed** (9 files), 0 failed |
| Static type-check | `pyright` | **0 errors, 0 warnings, 0 informations** |
| Tree sync | `git rev-parse HEAD` vs `origin/feat/autonomous-os` | identical `35591d0a`; working tree clean |
| Alembic drift guard | `scripts/ci_alembic_check.py` | **ALL PASS** — single head `0012_subscription_cancel_fields`, empty-DB upgrade clean, **32-table ORM parity** |

---

## 1. Phase B — Deep Architecture Audit (verification by source read)

### 1.1 Deployment topology (render.yaml / Procfile / Dockerfile)

- **Two-service Render blueprint** (`render.yaml`):
  - `tradetron-backend` — main API. `rootDir: fastapi-template`, `startCommand: uvicorn app.main:app`, health `/api/health`, `releaseCommand: alembic upgrade head`. Env: `ENVIRONMENT=production`, `BROKER_MODE=simulated`, `FEED_MODE_CRYPTO=live`, `ALLOWED_ORIGINS` locked to `https://tradethrone.vercel.app,https://tradethron.vercel.app`, `JWT_SECRET` `generateValue: true`.
  - `tradetron-webhooks` — separate FastAPI app (`app/webhooks/main.py`), own `/healthz`, `/readyz`, `/metrics`; same `DATABASE_URL` + `REDIS_URL`; `WEBHOOK_LOCAL_MODE=false`; `TRADETHRONE_WEBHOOK_SECRET` generated. Provider webhooks (Zerodha/Upstox/AngelOne postbacks, Razorpay billing, TradeThrone signals) route here — the main API does NOT mount the ingress router.
  - Shared `tradetron-postgres` (starter, region oregon, `databaseName: tradetron`).
  - **Redis is NOT auto-provisionable by Blueprint** — must be created as a Render Key Value and linked to both services (`⛔ E-1`).
- **Procfile** (Railway path): `web` = main API, `webhook` = webhook service on `WEBHOOK_PORT`.
- **Dockerfile**: `python:3.11-slim`, non-root `appuser` (UID 1000), `curl`/`build-essential`/`libpq-dev`, `HEALTHCHECK` hits `/api/health`, uvicorn with `--proxy-headers --forwarded-allow-ips='*'`. no `.pyc` writes, unbuffered output.

### 1.2 Configuration guards (`app/config.py`)

`_validate_production_boot()` fails fast (refuses to boot) when `ENVIRONMENT=production` and any of:
- `JWT_SECRET` missing / < 32 chars / in the known-bad SHA-256 blocklist (14 digests);
- `SKIP_SIGNATURE_VERIFICATION=true` (HMAC enforcement is mandatory in prod);
- `WEBHOOK_LOCAL_MODE=true` (bypasses HMAC + Redis queue);
- `DATABASE_URL` missing or SQLite (`sqlite://`);
- no explicit Redis URL (`effective_redis_url` empty), no silent localhost fallback;
- `redis://` hostname missing; non-`redis`/`rediss` scheme rejected.

Risk defaults: `max_position_size=100`, `max_daily_loss=10000`, `max_orders_per_minute=30`. `BROKER_MODE` defaults to `simulated`; live dispatch requires the explicit triple-guard (`live_dispatch_allowed()` in `app/brokers/__init__.py` + per-adapter boundary gates).

### 1.3 Database (`app/db/session.py`, `app/db/migrations.py`)

- `init_db()` → `ensure_tables_local_dev()` **refuses** to run `create_all`/ALTER when `ENVIRONMENT=production`; schema is exclusively Alembic-owned.
- `run_migrations()`: subprocess `alembic upgrade head`; **credential redaction** before logging Alembic output; fail-closed (`MigrationError` aborts startup — no request is served on a drifted schema).
- Admin seeding: bootstrap admin created with a **generated strong random password** when none configured; hexdump logged once for operator capture, never echoed after seed.

### 1.4 Webhook platform (`app/webhooks/main.py`, `ingress/router.py`, `queue/redis_streams.py`)

- **Fail-closed acceptance:** `QueueUnavailableError` → HTTP 503 (never a silent 202). A webhook that cannot be durably queued in non-local mode must let the sender retry. (Verified: `fix(webhooks) fail closed on degraded Redis … 64b2fd52`.)
- **Ingress pipeline** (per request): HMAC signature verify → freshness/replay guard (stale timestamps older than the idempotency TTL are rejected 401) → schema validation (422) → idempotency check-and-lock (duplicate → 202 cached) → route resolve + enrich → Redis Streams enqueue → 202 with `X-RateLimit-*` headers.
- **Signature posture:** when `webhook_local_mode=false`, a provider with no registered verifier is **rejected 401** (the `("*","*")` fallback route never silently accepts unsigned signals).
- **Redis Streams queue:** priority lanes derived from `ROUTE_TABLE`, per-route consumer groups created at init (`mkstream=True`, `BUSYGROUP` tolerated), DLQ always present, PEL-recovery on reconnect, `attempt >= max_retries` → DLQ with `final_error`/`original_queue`, `ack`/`nack` orderings guarantee the requeue copy is durable before the original leaves the PEL.
- **Worker pools:** `tradethrone_critical/high/normal` + `custom_normal` registered by `handlers/tradethrone_signal.py` (the only handler family for trade signals).
- **Resiliency stack** (verified by source): distributed Lua token-bucket rate limiter (falls back to process-local), idempotency store (7-day TTL; `is_completed()` is read-only and **fail-safe**: indeterminate → NOT completed), circuit breakers (db/redis/zerodha/razorpay/websocket), bulkheads (db/redis/broker APIs/websocket/webhook_processing).
- **TradeThrone signals are intent-only:** `handle_tradethrone_signal` → `AgentIntentTriggerBridge.submit_webhook_signal` → durable `agent_tasks` row (deterministic envelope-derived trigger key = idempotency key) → later scheduler pass → governed `AgentTradingService` intent → broker. **No synchronous broker dispatch**, not even in `webhook_local_mode` (verified in ingress local-mode handler).
### 1.5 Autonomous engine chain (`app/engine/*`)

- `agent_runtime.py`: dual-path claiming (PG `FOR UPDATE SKIP LOCKED` / SQLite CAS), heartbeats + stale recovery, capability intersection (code ∧ registry row, fail-closed), attempt/timeout clamps sourced from settings.
- `agent_intent_triggers.py`: accepts only `PAPER`/`DEMO`/`LIVE` (DEMO→PAPER), only `MARKET`/`LIMIT` entry orders, **server-derived ownership only** (unresolved owner fails closed); convergence on exactly one intent + one dispatch via CAS + unique anchors.
- `protective_orders.py`: `PROTECTED` only with genuine broker leg references (no fabrication); `PENDING_PLACEMENT` committed before broker call; reference-less PENDING rows reconciled for manual review, never blind re-poked.
- `order_reconciliation.py`: broker READ-ONLY reconciliation of stale keyed PENDING rows (Window-C via `get_positions`), never fabricates fills, bounded loop, tenant-scoped.
- `broker_state_sync.py`: broker truth persisted as `BrokerStateRecord` (CAS upsert, freshness tracked), stale snapshots never presented as LIVE truth.
- `broker_cron.py`: 8:45 AM IST TOTP/session renewal; skipped when `live_dispatch_allowed()` is false.
- Startup lifespan (main): migrations → DB init → broker (simulated in current posture) → tick queue → simulator → unified market hub → engine → TOTP scheduler → order recon → broker state sync → protective orders → agent runtime → agent control; teardown reverses in order.

### 1.6 Security / observability (`app/core/cors.py`, `app/core/monitoring.py`)

- **CORS:** production-locked to exact origins; no `*` with `allow_credentials=True`; no generic `*.vercel.app` regex trust; dev-only regex behind non-production.
- **Routers** (`app/api/main_router.py` verified in deep read): `/api/health`, `/api/admin`, `/api/users`, `/api/auth`, `/api/strategies`, `/api/orders`, `/api/trades`, `/api/market-data`, `/api/marketplace`, `/api/agents`, `/api/copy-trading`, `/api/v2/reports`, etc.
- **Docs closed in production:** `docs_url`, `redoc_url`, `openapi_url` all `None` when `ENVIRONMENT=production`.
- **Sentry + Telegram sentinel:** order-failure alerts at `fatal`; risk-breach/broker-disconnect alerts; secret redaction in alert/log text; no silent failures.
- **Request metrics middleware:** Prometheus text at `/metrics` (`tradetron_http_requests_total`, `tradetron_engine_state`, `tradetron_broker_mode_live`, `tradetron_ws_channels`); webhook service exposes its own `webhook_*` series.

### 1.7 Frontend (`client/src/config.js` + deploy contract)

- `API_BASE` resolves: `VITE_API_URL`/`VITE_API_BASE_URL` env override → localhost:8080 on dev hostnames → prod fallback **`https://tradetron-8jkz.onrender.com`** (verified live; legacy `tradethrone.onrender.com` is dead). WS URL derived by http→ws scheme swap; tokens appended only for private endpoints.
- Phase 5D `/verify-live` e2e smoke asserts backend LTP banding, no fabricated NIFTY `24850.0`, honest `Awaiting feed…` placeholder, no hardcoded broker name in deployment modal.

### 1.8 DEPLOYMENT.md accuracy

- Env table, Redis provisioning runbook (Key Value → link → `/readyz` `cache:true`), Supabase/Railway/Cloudflare options, migration policy (Alembic-owned, startup gate authoritative on Free), observability endpoints, dependency note on `kiteconnect`/`autobahn`, and repo-root hygiene all **match the current codebase**. (One cosmetic drift: Pre-Flight states `156/156 pytest` — stale count vs current 907; corrected in §Phase L.)
---

## 2. Phase C–J Domain Verification — ALL GREEN (targeted suites at HEAD)

| Phase | Domain | Suite (pytest unless noted) | Result |
|---|---|---|---|
| C | Webhook readiness | `test_webhooks_integration.py`, `test_broker_postback_hardening.py` | ✅ (with D) |
| D | Autonomous chain stress | `test_signal_webhook_durable_orders_green.py`, `…_red.py`, `test_agent_autonomous_e2e.py` | **83 passed** (C+D, 30.4 s) |
| E | Order/protective chaos | `test_protective_orders.py`, `test_protective_orders_e2e.py`, `test_order_reconciliation.py`, `test_close_cas_durability_before_dispatch.py`, `test_concurrent_close_cas.py`, `test_close_race_red_repro.py`, `test_phase15_reconciliation_partial_fill_red.py`, `test_phase15_kill_switch_user_scoped_red.py`, `test_phase15_simulator_survives_error_red.py`, `test_phase15_tick_loop_survives_error_red.py` | **58 passed** (46.2 s) |
| F | Frontend audit | `vitest run` (9 files) | **71 passed** |
| G | Security / tenant | `test_watchlist_tenant_isolation.py`, `test_ws_auth_isolation.py`, `test_p3a_position_ownership.py`, `test_production_auth.py`, `test_copy_trading_p0_live_safety.py`, `test_public_trade_exposure.py`, `test_payment_forgery_failclosed.py`, `test_p2_docs_disabled_production.py` | **61 passed** (40.8 s) |
| H | Resilience | `test_live_mode_guard.py`, `test_live_routing_uncovered_paths.py`, `test_p2_broker_adapter_gates.py`, `test_engine_signal_cooldown.py`, `test_market_freshness.py`, `test_feed_gate.py`, `test_ws_connection_limits.py` | **54 passed** (H+I, 37.5 s) |
| I | Performance/rate limits | `test_p2_api_order_rate_limit.py` (in H+I run) | ✅ |
| J | Observability | `test_p14_observability.py`, `test_p2_logging_metrics.py`, `test_revoked_token_retention.py`, `test_dashboard_no_fake_strategies.py` | **19 passed** (14.4 s) |
| K | Regression (idempotency / accounting / live safety) | `test_order_idempotency.py`, `test_postback_trade_idempotency.py`, `test_paper_accounting_hardening.py`, `test_paper_book_bounded_history.py`, `test_subscription_symbol_cap.py`, `test_risk_guard.py`, `test_dma_execution.py`, `test_strategy_live_entry_orphan_repro.py`, `test_broker_unlink_live_position_safety.py`, `test_copy_trading_live_entry_crash_window_red.py` | **57 passed** (31.1 s) |

> Cumulative targeted re-verification at HEAD `35591d0a`: **332 backend tests + 71 frontend tests**, all GREEN, alongside the full-suite baseline (907) and drift/type gates.

---

## 3. Findings & Defect Review

### 3.1 Locally-fixable defects found

1. **DEPLOYMENT.md stale pre-flight count** (`156/156`) vs current 907 — documentation-only, corrected in Phase L.
2. **No other code defects found.** The deep-read (config, session, migrations, cors, webhooks ingress/queue/validation/resiliency, engine triggers/runtime, protective/reconciliation/state-sync, monitoring) surfaced no correctness, security, or tenant-isolation gaps.

### 3.2 Honest-risk register (unchanged by design)

| # | Risk | Posture | Status |
|---|---|---|---|
| R1 | Unauthorised live order | 3-layer `BROKER_MODE` guard + adapter boundary gates + `deny_live_dispatch` | MITIGATED (proven in H/I suites) |
| R2 | Webhook signature bypass | HMAC mandatory in prod; `webhook_local_mode` hard-refused in prod | MITIGATED |
| R3 | Replay after key expiry | Freshness guard: events older than idempotency TTL rejected 401 | MITIGATED |
| R4 | Cross-tenant mutation | Every engine op re-binds order/position to its own broker account & owner | MITIGATED (G suites) |
| R5 | Redis outage → silent data loss | `QueueUnavailableError` → HTTP 503 | MITIGATED (fail-closed) |
| R6 | Credential leak in logs | Alembic output redacted; sentinel text redacts configured secrets | MITIGATED |

---

## 4. External Blockers (unchanged — require operator action)

| # | Blocker | Impact | Action |
|---|---|---|---|
| **E-1** | Render Key Value (Redis) not provisioned | Backend `/readyz` `cache:false` → 503 at boot in production | Dashboard → New → Key Value → `tradetron-redis` (oregon) → link to BOTH `tradetron-backend` and `tradetron-webhooks` |
| **E-2** | Broker sandbox credentials absent (Angel One/Zerodha/Upstox/Binance) | No live-integration verification; bounds current work to simulated | Obtain sandbox creds; configure in Render secrets |
| **E-3** | Alembic migrations not yet run against real Render Postgres | Schema may lag on first deploy | After E-1/E-2: deploy → `releaseCommand` runs `alembic upgrade head` |
| **E-4** | Final Render/Vercel deploy verification | `/healthz`, `/readyz`, `/metrics`, frontend integration unconfirmed on live host | After E-1/E-3: run `verify-live/e2e-smoke.mjs` against prod |

---

## 5. Verdict

**LOCAL CODE READINESS — CONFIRMED.** Backend 907 + frontend 71 + pyright clean + alembic drift clean at a synced HEAD. Webhook, autonomous-chain, protective-order, tenant-isolation, resilience, performance, and observability domains each re-verified GREEN at this HEAD. **LIVE-trading readiness NOT declared** — `BROKER_MODE=simulated` remains enforced at every dispatch gate. Next required step is the external infrastructure milestones (E-1 → E-4).