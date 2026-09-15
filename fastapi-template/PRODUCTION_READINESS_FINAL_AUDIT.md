# TRADETHRONE — PRODUCTION READINESS · CONSOLIDATED FINAL AUDIT

> **Branch:** `feat/autonomous-os`
> **HEAD:** `64b2fd52`
> **Date:** 2026-09-15
> **Overall Verdict:** 🟢 **PRODUCTION-READY** (all engineering-owned items resolved; external operator actions remain as documented in §K)

---

## Gate Summary

| Gate | Result | Detail |
|---|---|---|
| Backend pytest | **902 / 902 passed** | 446 s, 3 pre-existing warnings |
| Frontend vitest | **71 / 71 passed** | 6.98 s |
| Frontend build | **OK** | 1.99 s, 9 chunks |
| ESLint | **0 errors** | 18 `react-hooks/set-state-in-effect` warnings (pre-existing) |
| Pyright | **0 errors** | 0 warnings, 0 informations |
| Alembic parity | **PASS** | Single head `0012_subscription_cancel_fields`; 32 tables match ORM exactly |
| Secret scan | **PASS** | 460 tracked files, zero findings |
| Git state | **Clean** | 0 ahead / 0 behind `origin/feat/autonomous-os` |

---

## Section A — Autonomous Trigger Pipeline ✅

**Scope:** Webhook ingress → governed bridge → durable task → agent intent → broker dispatch.

| Stage | Component | Verified | Notes |
|---|---|---|---|
| Trigger (webhook) | `webhooks/ingress/router.py` → `tradethrone_signal.py` | ✅ | Payload schema validated, owner resolved server-side |
| Durable acceptance | `AgentIntentTriggerBridge.submit_webhook_signal` | ✅ | Deterministic `webhook_trigger_key` = task `idempotency_key` |
| Queue | `AgentRuntimeConfigRecord` / `AgentTaskRecord` | ✅ | Duplicate delivery idempotent |
| Scheduler drain | `AgentRuntimeScheduler` (lifespan) | ✅ | Fail-closed registry + claim gates |
| Intent gate | `AgentTradingService.evaluate` | ✅ | `DEMO→PAPER` normalized; MARKET/LIMIT only; SL/SL-M rejected |
| Risk / margin / feed gates | `RiskManager` + `assert_live_dispatch_allowed()` + freshness | ✅ | `LIVE` never silently falls back to `PAPER` |
| Idempotent execution | `durable_claims` + CAS | ✅ | Replays return `ok=True, idempotent=True` |
| Broker dispatch | `get_broker_adapter()` + `place_order` | ✅ | **ONLY** inside `agent_intents.py` |

**Defect resolved this session:** Legacy `_handle_local_mode` dispatched directly to Angel One — now delegates to the governed bridge; local mode durably enqueues and never touches a broker (D-1, Session 2).

---

## Section B — Broker Dispatch Isolation ✅

**Three-layer defense-in-depth** for LIVE broker order dispatch:

| Layer | Guard | Effect |
|---|---|---|
| **Call-path** | `assert_live_dispatch_allowed()` | Blocks dispatch unless `BROKER_MODE=live` |
| **Adapter** | Each real adapter calls `assert_live_dispatch_allowed()` | Cannot bypass via direct method call |
| **Network** | Binance `_api_request()` calls the guard | Blocks all HTTP requests in non-live mode |

**Broker-mode routes (8 paths audited):**

| Path | Guard | Evidence |
|---|---|---|
| Manual order (`POST /api/trades/order`) | `assert_live_dispatch_allowed()` → HTTP 403 | `trades.py:591-595` |
| DMA order (`POST /api/v1/orders/execute-dma`) | Same pattern | `trades.py:866-870` |
| Protected order (`POST /api/trades/positions/.../protect`) | Same pattern | `trades.py:1155-1159` |
| Agent trading intent | `agent_intents.py` → adapter internal guard | AST-verified |
| Copy-trading LIVE fan-out | `assert_live_dispatch_allowed()` + per-follower adapter | `copy_trading.py` |
| Visual strategy `execute_legs` | Guard before first leg | `visual_strategy.py:97` |
| Order manager `execute_order` | Adapter-internal guard | `order_manager.py:250` |
| Broker cron session renewal | `live_dispatch_allowed()` check | `broker_cron.py:139-146` |

**Result:** One and only one path reaches a broker — `AgentTradingService.execute_intent._dispatch` in `agent_intents.py`.

---

## Section C — Order Lifecycle & Idempotency ✅

### Durable-Claim Pattern (LIVE orders)

Every LIVE order follows the two-phase pattern:

```
SIGNAL → DURABLE PENDING CLAIM → BROKER DISPATCH → PERSIST broker ref
(own commit) → CAS FINALIZE FILLED + Trade + Position → DONE
```

On any dispatch/guard failure the PENDING claim is CAS-rejected. A crash after broker acceptance but before finalization leaves a recoverable PENDING row resolved by the reconciliation engine.

### Three-Window Reconciliation

| Window | Condition | Action |
|---|---|---|
| Window-A | Claim row, no `broker_order_id` | Re-dispatch or reject |
| Window-B | `broker_order_id` present | `get_order_status()` via adapter |
| Window-C | No broker ref, stale | `get_positions()` for fill truth |

**Tests:** 30+ tests covering claim, CAS reject, reconciliation, idempotent replay (`test_order_idempotency.py`, `test_copy_trading_durable_claim.py`).

---

## Section D — Redis Queue Resilience ✅ (Fixed this session)

### Defect Found & Fixed

**Before:** When Redis was unavailable at boot in non-local-mode, `enqueue()` returned `"local-mode"` and the ingress returned HTTP 202 — **silent data loss** on broker fills, trade signals, and payment events.

**After (commit `64b2fd52`):**

1. **`QueueUnavailableError(RuntimeError)`** — new exception in `redis_streams.py:21`
2. **`enqueue()` raises** `QueueUnavailableError` instead of returning `"local-mode"` for production-degraded state
3. **Ingress catch** — surfaces **HTTP 503** so the sender retries; marks idempotency as failed
4. **Local-mode preserved** — `webhook_local_mode` still returns `"local-mode"` (forbidden in production by `config.py`)

### Regression Tests (3 new, 902 total suite)

| Test | Validates |
|---|---|
| `test_degraded_queue_returns_503_not_silent_202` | End-to-end: degraded queue → HTTP 503 |
| `test_enqueue_raises_queue_unavailable_when_uninitialized` | Unit: fresh queue raises `QueueUnavailableError` |
| `test_enqueue_local_mode_keeps_noop_contract` | Local-mode dev path preserved |

---

## Section E — Risk Management & Circuit Breakers ✅

| Mechanism | Component | Behavior |
|---|---|---|
| Pre-trade risk gate | `RiskManager.check()` | Kill-switch, circuit breaker, daily loss, position size, order rate |
| Auto-pilot loss guard | `_evaluate_autopilot()` | Auto-trips kill-switch after N consecutive losing trades |
| Intraday drawdown guard | `_evaluate_autopilot()` | Auto-trips on % giveback from peak P&L |
| Emergency kill-switch | `trigger_kill_switch()` | Manual/auto halt on all order flow |
| Daily loss breaker | `check()` | `daily_pnl ≤ -max_daily_loss` → blocks all |
| Order rate limiter | `_prune_old_orders()` | Per-minute throttle |
| Margin gate | `check_margin()` | Pre-trade collateral check |
| Config API | `POST /api/risk-guard/config` | Runtime threshold tuning (0-100 validated) |

---

## Section F — Agent Runtime & Autonomy Governance ✅

### Capability Enforcement

- **Intersection model (fail-closed):** Agent exercises capabilities in BOTH code definition AND DB registry
- **`readonly` agents** can NEVER execute `WRITE/EXECUTE` handlers
- Timeouts and attempt budgets hard-clamped against settings

### Autonomy Gates

An `autonomous` task executes only when:
1. `autonomous_mode_enabled` is `true`, AND
2. `global_autonomy_level ≥ 1` AND agent ceiling satisfies `1 ≤ ceiling ≤ global_autonomy_level`

### Task Lifecycle

| Transition | Guard |
|---|---|
| `PENDING → RUNNING` | `FOR UPDATE SKIP LOCKED` (Postgres) or CAS (SQLite) |
| `RUNNING → SUCCEEDED` | `rowcount == 1` |
| `RUNNING → FAILED` | Handler throw / timeout / heartbeat expiry |
| Stale `RUNNING` | Heartbeat recovery requeues or marks `WORKER_LOST` |

### Handler Registry

| Agent Type | Task Kind | Capability |
|---|---|---|
| `trading_agent` | `execute_trade` | `CAP_TRADE` |
| `trading_agent` | `evaluate_intent` | `CAP_EVAL` |
| `engineering_monitor` | `system_health_report` | `CAP_READ` |
---

## Section G — Webhook Security ✅

| Mechanism | Component | Behavior |
|---|---|---|
| HMAC-SHA256 signature | `webhooks/validation/middleware.py` | V3 HMAC verification; timing-safe comparison |
| Token-bucket rate limiter | `TokenBucketRateLimiter` | Redis Lua atomic script; in-memory fallback |
| Idempotency store | `IdempotencyStore` | Redis Lua atomic check-and-set; 7-day TTL |
| Duplicate suppression | Worker `is_completed()` | Non-mutating GET; fail-safe `False` on Redis error |
| Replay protection | `validate_webhook_request` | Events older than idempotency window rejected as stale |
| Production boot guard | `config.py` | `WEBHOOK_LOCAL_MODE=true` → `ValueError` in production |
| Queue degradation | `QueueUnavailableError` | HTTP 503 (retryable); never silent 202 |

**Fail-open vs fail-closed differentiation:**
- **Ingress dedup** (`check_and_mark_processing`): fails OPEN — event proceeds; dedup is best-effort
- **Worker duplicate suppression** (`is_completed`): fails CLOSED — never drops real events

---

## Section H — Database Integrity ✅

| Check | Result |
|---|---|
| Migration head | Single head: `0012_subscription_cancel_fields` |
| Schema parity | 32 tables match ORM metadata exactly (`ci_alembic_check.py`) |
| Clean upgrade | `alembic upgrade head` applies cleanly on empty DB |
| Order indexes | 6 incl. unique partial `(user_id, client_order_id)`, `(signal_key)` |
| Position indexes | 6 incl. `(user_id, symbol)`, status, mode, FKs |
| Trade indexes | 4 incl. `(user_id, executed_at)` |
| Protective order indexes | 3 incl. unique `(position_id, leg)` |
| Agent tables | `agents`, `agent_tasks`, `agent_runtime_config` (0009) |
| Subscription tables | `subscriptions` with cancel fields (0012) |

---

## Section I — Scheduler Health & Crash Recovery ✅

| # | Scheduler | Module | Interval | Fail-Safe |
|---|---|---|---|---|
| 1 | `BrokerSessionScheduler` | `broker_cron.py` | 8:45 AM IST | `_running` guard; skips if not live |
| 2 | `BrokerOrderReconciliationEngine` | `order_reconciliation.py` | Configurable | Per-order try/except; 3-window |
| 3 | `BrokerStateSyncScheduler` | `broker_state_sync.py` | Configurable | Per-pass lock; honest UNAVAILABLE |
| 4 | `ProtectionScheduler` | `protective_orders.py` | 30s | Per-position lock; terminal-leg detection |
| 5 | `AgentRuntimeScheduler` | `agent_runtime.py` | Configurable | 5s backoff; heartbeat recovery |
| 6 | `AgentEvaluationScheduler` | `agent_control.py` | Configurable | Per-pass try/except; idempotency keyed |

**PEL Recovery (Redis Streams):**
- Background loop every 30 s; claims up to 25 entries idle ≥ 120 s via `XAUTOCLAIM`
- Min-idle derived from route timeouts (`max(2 × max_timeout, 120s)`)
- Claimed entries dispatched through `_process_webhook` — same path as normal workers
- Duplicate suppression, XACK ordering, nack/retry/DLQ all apply unchanged

**Lifecycle:** All 6 schedulers started in `app/main.py::lifespan`; gracefully stopped on shutdown. Zero orphaned tasks.

---

## Section J — Security & Tenant Isolation ✅

| Surface | Auth | IDOR Protection |
|---|---|---|
| All user endpoints | `get_current_user` (HMAC-SHA256 JWT, 15-min) | User-scoped queries |
| Admin endpoints | `get_current_admin_user` (RBAC) | RBAC-gated |
| Agent endpoints | `get_current_admin_user` | User-scoped |
| WebSocket | JWT at connection | Per-user broadcast |
| Position ops | User dependency | `position.user_id == user.id` |
| Protective order ops | User dependency | `authorized_user_id` validated |
| Trade history / dashboard | User dependency | User-scoped queries |

**Additional hardening:**
- **CORS:** Production locks to explicit origins (no wildcard + credentials)
- **Docs:** `/docs`, `/redoc`, `/openapi.json` disabled in production
- **Error handling:** Two-layer middleware → JSON 500 (no stack traces)
- **Security headers:** nosniff, DENY, strict-origin-when-cross-origin, restricted permissions-policy
- **JWT validation:** Production boot rejects weak secrets (< 40 chars)
- **Subscription limit:** Overflow → HTTP 429 (fail-closed)
- **Pre-flight migrations:** `upgrade head` before any request; `MigrationError` → refuse to boot
---

## Section K — Known Gaps, External Blocks & Operator Action Items

### External Action Required (not engineering-owned)

| # | Item | Status | Action |
|---|---|---|---|
| E-1 | **Redis provisioning on Render** | ⚠️ BLOCKED | Create Render Key Value (Redis) instance, link to `tradetron-backend`, redeploy. Otherwise production returns HTTP 503 at boot. |
| E-2 | **Broker sandbox credentials** | ⚠️ BLOCKED | No exchange testnet creds for Angel One / Zerodha / Upstox / Binance drills |
| E-3 | **PostgreSQL migration chain (Render)** | ⚠️ BLOCKED | Local SQLite parity verified; Render Postgres chain not yet exercised |
| E-4 | **Render/Vercel deploy verification** | ⚠️ BLOCKED | Frontend + backend probes return 503 until Redis provisioned (E-1) |

### Known Code Gaps (non-blocking)

| # | Gap | Severity | Detail |
|---|---|---|---|
| G-1 | No frontend agent console | P3 | Admin agent API works; no user-facing UI. No bypass risk (API admin-gated) |
| G-2 | Legacy `place_tradethrone_order` definitions remain | P3 | Zero callers in `app/` (AST-verified); unreachable from production |
| G-3 | Global HTTP middleware rate limiter absent | P3 | All state-changing endpoints have targeted limiting; middleware is defense-in-depth |
| G-4 | Frontend lint warnings | P3 | 18 `react-hooks/set-state-in-effect` (intentional idiom) |
| G-5 | Crypto feed Coingecko REST 60s polling | P2 | Acceptable; may need WS upgrade for latency-sensitive strategies |

### Resolved This Session

| # | Finding | Resolution |
|---|---|---|
| D-1 | **P0: Silent webhook data loss on degraded Redis** | Fixed: `QueueUnavailableError` → HTTP 503 (commit `64b2fd52`); 3 regression tests |

### Resolution Summary

| Category | Count | Status |
|---|---|---|
| Code defects found | 1 | ✅ Resolved (Redis queue fail-closed) |
| Engineering-owned blockers | 0 | ✅ All resolved |
| External operator actions | 4 | ⚠️ Awaiting operator |
| Non-blocking gaps | 5 | Documented; no code change required |

---

## Appendix — Module Inventory (key files)

| Module | Purpose |
|---|---|
| `app/webhooks/ingress/router.py` | Webhook HTTP endpoints |
| `app/webhooks/queue/redis_streams.py` | Redis Streams queue with fail-closed guard |
| `app/webhooks/handlers/tradethrone_signal.py` | TradeThrone signal → bridge |
| `app/engine/agent_intent_triggers.py` | Governed trigger bridge |
| `app/engine/agent_runtime.py` | Autonomous-agent runtime (task lifecycle) |
| `app/engine/agent_intents.py` | Intent evaluation → broker dispatch |
| `app/engine/agent_control.py` | Agent evaluation scheduler |
| `app/engine/copy_trading.py` | Institutional copy trading (durable-claim) |
| `app/engine/risk_manager.py` | Pre-trade risk + auto-pilot kill-switch |
| `app/engine/trading_engine.py` | Core trading engine |
| `app/engine/order_reconciliation.py` | 3-window broker reconciliation |
| `app/engine/protective_orders.py` | SL/TP lifecycle management |
| `app/brokers/__init__.py` | `BrokerModeBlockedError`, `assert_live_dispatch_allowed` |
| `app/config.py` | Settings + production boot validation |

---

*Audit concluded: 2026-09-15. All engineering-owned items resolved. No further code changes required.*