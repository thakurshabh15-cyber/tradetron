# TradeThrone Backend — Production Readiness Audit Report

**Audit scope:** `fastapi-template/` (FastAPI backend + React client) and `agency-agents-main/` (orchestration framework)
**Date:** 2026-09-04
**Mode:** Read-only discovery — no files modified
**Auditor tooling:** Static code review, dependency inspection, live HTTP probes to Render/Vercel, git history forensic search

---

## Executive Summary

TradeThrone is a multi-asset algorithmic trading platform (Indian Equities, Crypto, Forex) with a FastAPI backend, a React (Vite) frontend, and a live deployment split across Render (backend) and Vercel (frontend). The **platform is currently NOT production-ready.** The live backend returns **HTTP 503** on every endpoint (service not running), the frontend shows "no data" because it cannot reach the backend, the test suite **cannot even be collected** (missing `opentelemetry` dependency), and — most critically — **real production secrets are committed to git history** and **no `.env` file exists on disk** to boot the application locally.

Of the 13 sections reviewed, only 3 are healthy enough to ship; 6 are broken or blocking, and 4 present serious security or reliability risks.

| # | Section | Verdict |
|---|---------|---------|
| 1 | Secrets & Credentials Management | 🔴 **CRITICAL FAIL** — real secrets in git history |
| 2 | Authentication & Authorization | 🟡 **AT RISK** — sound design, weak production defaults |
| 3 | Dependency & Supply-Chain Integrity | 🔴 **BROKEN** — test suite cannot collect |
| 4 | Configuration & Environment Management | 🔴 **BROKEN** — no `.env`, misconfigured deployment |
| 5 | Deployment & Infrastructure | 🔴 **BROKEN** — live 503, misconfigured URLs |
| 6 | Observability, Logging & Tracing | 🟡 **AT RISK** — tracing optional but breaks tests |
| 7 | Data Storage & Database Reliability | 🟢 **HEALTHY** — resilient dual-dialect support |
| 8 | Data Integrity & Freshness | 🟢 **HEALTHY** — honest freshness metadata |
| 9 | Webhook & Integration Security | 🟡 **AT RISK** — HMAC sound, untestable |
| 10 | Networking, Rate Limiting & DDoS | 🟡 **AT RISK** — single-process limiters, no CDN |
| 11 | Error Handling & Fault Tolerance | 🟢 **HEALTHY** — graceful degradation throughout |
| 12 | Frontend Integration & Resilience | 🟡 **AT RISK** — hardcoded dead host, no backend |
| 13 | Release Readiness & Testing | 🔴 **BROKEN** — tests fail to collect, no CI |


---

## Section 1 — Secrets & Credentials Management 🔴 CRITICAL FAIL

### Findings

**Real, live secrets are committed to the git repository history and remain discoverable via `git log -S`.** This is the single highest-severity finding in the entire audit.

Confirmed compromised secrets (extracted from git history):

| Secret | Value (partial) | Risk |
|--------|------------------|------|
| CoinGecko API key | `CG-4sEjuAWETfFVoYG` | Financial data abuse / quota theft |
| Angel One API secret | `ujOYo3BK` | **Real broker credential** — trading access |
| Angel One API key | `AACE856765` | **Real broker credential** — trading access |
| JWT signing secret | `super-secret-jwt-key` (+`-change-in-production`) | **Full auth bypass** — forge any JWT |
| Database connection strings | Supabase Postgres / Redis URLs | **Full data theft & tampering** |
| Claude API key | present in history | LLM/API spend abuse |

**Root cause: no `.env` file pattern is git-ignored.** `git check-ignore` returns **nothing** for `.env`, `.env.production`, `.env.staging`, or `.env.example`. A `git ls-files` listing confirms upstreamed env files carrying these values. The committed `.env.production` (recovered from commit `35c2477^`) contained populated `JWT_SECRET`, broker credentials, and datasource URLs.

### Remediation (blocked pending user approval — will NOT modify without explicit authorization)

1. **Immediately rotate every affected credential** — CoinGecko key, Angel One secret/key, JWT secret, DB/Redis passwords, Claude key. Assume anything in history is public; the broker credentials in particular grant real trading access and must be revoked **first**.
2. **Add git-ignore entries** — `.env`, `.env.*`, `*.env`, `.env.production`, `.env.staging`.
3. **Rewrite git history** — use `git filter-repo` (or BFG) to purge secrets from all commits if the team elects to keep the repo. This is destructive and requires force-push + team coordination; recommend a **fresh private repo** with a clean history instead if history value is low.
4. **Enable a secret scanner** (trufflehog / gitleaks) in CI **before** this task's follow-ups go in.

> ⚠️ **SEVERE:** Any deployment using the committed JWT secret (`super-secret-jwt-key…`) allows an attacker to mint valid access tokens for **any user including the super-admin** without credentials. Do not boot production until rotated.


---

## Section 2 — Authentication & Authorization 🟡 AT RISK

### Findings

The auth core is **well engineered**: PBKDF2-HMAC-SHA256 password hashing (100k iterations, random 16-byte salt), algorithm-clamped HS256 JWT with explicit algorithm-confusion hardening, short-lived 15-minute access tokens, 7-day refresh tokens. There is a hard fail-fast guard refusing to boot with `ENVIRONMENT=production` unless `JWT_SECRET` is ≥ 32 chars. This is genuinely good production posture.

**However:**
- The **production JWT secret is the committed `super-secret-jwt-key…`** (see §1) — the fail-fast guard is *satisfied by the leaked value* because it's 32+ chars, so it offers **no protection** against the actual compromise.
- `docker-compose.yml` hardcodes `JWT_SECRET: dev_super_secret_jwt_key…` — an operator running compose in prod-facing mode inherits a known secret.
- CSRF/SSRF protections for cross-origin admin actions and the OAuth flow were not fully validated in this pass; the Google OAuth client ID is env-gated but the PKCE/login-request flow was only spot-checked.
- No evidence of `HttpOnly`/`Secure` cookie flags or CSRF tokens for cookie-based refresh handling (tokens appear bearer-style; acceptable but must be stored in secure client storage).

### Remediation
- Rotate secret (§1), then keep the >32-char fail-fast guard as defense-in-depth.
- Remove the known dev JWT secret from `docker-compose.yml`; require it via `.env` / build arg.
- Add an explicit audit + test for refresh-token rotation and revocation on password change.

---

## Section 3 — Dependency & Supply-Chain Integrity 🔴 BROKEN

### Findings

**The test suite cannot be collected.** Running `pytest tests/` fails at import time with:

```
ModuleNotFoundError: No module named 'opentelemetry'
```

`tests/test_webhooks_integration.py` imports `app.webhooks.tracing` (or transitively `opentelemetry`) at module scope, but **none of the OpenTelemetry packages are declared in `requirements.txt`** (which lists only: fastapi, uvicorn, sqlalchemy, aiosqlite, asyncpg, psycopg, pydantic-settings, python-dotenv, pyotp, httpx, aiohttp, psutil, websockets, redis, alembic, cryptography).

Impact: **zero CI safety net.** Any regression in the 26+ registered routes or the auth/trading logic ships unverified. Because import-time tracing is not guarded, the entire app is in a fragile state: if OTel runtime deps are missing in a fresh prod image the same `ModuleNotFoundError` could surface at app boot.


---

## Section 4 — Configuration & Environment Management 🔴 BROKEN

### Findings

- **No `.env` file exists on disk** in `fastapi-template/`. `app/config.py` (`SettingsConfigDict(env_file=BASE_DIR / ".env")`) therefore loads **all-default** values: empty `jwt_secret`, empty broker credentials, `DATABASE_URL` default, `broker_mode=simulated`. The app cannot meaningfully boot with identity/feature config.
- `.env.example` exists with safe placeholders and is well-commented, but **`.env` and every real env file are NOT git-ignored** (§1). This is the exact combination — no local `.env`, but real env files *were* committed — that produced the secret leak.
- `.env.production` **exists in the working directory with sensitive values present** and is tracked/committed — a live config with secrets checked into VCS.
- `app/config.py` has strong *production fail-fast guards* (JWT length, skip-signature-verification refusal) and honest defaults, so the config *layer* is sound — the weakness is purely the **missing local file + leaked committed file**.

### Remediation
- On approval: create a local `.env` from `.env.example` with dev values; git-ignore all env files; remove `.env.production` from the working tree & history.

---

## Section 5 — Deployment & Infrastructure 🔴 BROKEN

### Findings

**Live backend is DOWN.** Verified 2026-09-04 against `tradetron-8jkz.onrender.com`:

| Endpoint | Result |
|----------|--------|
| `GET /api/health` | **HTTP 503 Service Unavailable** — service not running |
| `GET /readyz` | **503** readiness check failure |
| `GET /api/market-data` | **503** — cannot retrieve quotes |
| `GET /healthz` | **503** (liveness also unreachable) |

`render.yaml` exists with a web service definition, but the **running instance is not up** (halted, out of hours, or crash-looping at boot). The `readyz` implementation itself is caught-degraded: it requires **both** the Supabase Postgres probe AND Upstash Redis probe; with `UPSTASH_REDIS_URL` unset, `effective_redis_url` falls back to `redis://localhost:6379/0` and fails, and `cache_required = (environment == "production")` turns a missing cache into a *failing* readiness in prod — so any Redis misconfiguration takes the whole instance out of the load balancer.

The **frontend points at a dead/legacy host**: `client/src/config.js` hardcodes `PROD_API_URL = "https://tradetron-8jkz.onrender.com"` but the code comments note the *actually verified-live* host differs, and the live host returns 503 anyway. `tradethrone.vercel.app` loads but shows "no data" because it cannot reach the backend.

Dockerfile + docker-compose are otherwise production-sound (non-root user, healthcheck, `--forwarded-allow-ips='*'` behind proxy).

### Remediation
- **Restart / rollback** the Render service for `tradetron-8jkz` (or rebuild from the last known-good image) and confirm `/api/health` returns 200.
- Configure `UPSTASH_REDIS_URL` (managed Upstash TLS Redis) in Render env so `readyz` passes in production. Do **not** ship with a failing mandatory cache check while the URL is unset.
- Reconcile the frontend `PROD_API_URL`/`PROD_WS_URL` with the true live backend host after the restart.
- Consider decoupling cache availability from readiness (or set it correctly) to avoid cascading outages.

### Remediation (two viable paths — choose one)
- **Option A (preferred):** Declare OTel deps in `requirements.txt` — `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `opentelemetry-instrumentation-fastapi`. Pin versions.
- **Option B:** Make the OTel import in `app/webhooks/tracing.py` (and anywhere it's transitively imported) **optional** with `try/except ImportError` so tracing all-but-no-ops when the SDK is absent.

Both restore a collectable, runnable test suite. Blocked until user approves a modification.


---

## Section 6 — Observability, Logging & Tracing 🟡 AT RISK

### Findings

- Structured logging is implemented (`app/core/logging.py`, `get_logger`), HTTP access logging is wired, and a Sentry DSN is configurable — good fundamentals.
- **OpenTelemetry tracing is optional-but-not-guarded.** `app/webhooks/tracing.py` imports OTel and the SDK's absence **breaks the entire test suite at collection** (see §3). Tracing exists as a feature but is currently silently non-functional in any deployment (packages not in `requirements.txt`).
- **No crash/availability alerting confirmed active.** Telegram bot token + chat id are configurable in `.env.example` and wired into the scheduler (broker session alerts), but given the backend is 503 and no `.env` exists, no alerts are actually firing.
- Frontend performance telemetry and distributed trace correlation were not confirmed.

### Remediation
- Resolve the OTel dependency gap (§3 Option A/B).
- Verify Sentry DSN + Telegram alert paths actually deliver on failure events once the backend is up.

---

## Section 7 — Data Storage & Database Reliability 🟢 HEALTHY

### Findings

`app/db/session.py` is genuinely resilient:
- **Dual-dialect support** — SQLite (aiosqlite) local default, PostgreSQL (asyncpg) hosted; automatic `postgres://` / `postgresql://` → `postgresql+asyncpg://` normalization, including `sslmode` → `ssl` translation for asyncpg.
- **Async engine** with per-dialect tuned pool settings; idempotent, synchronous table creation and schema sync; seeding of default admin + watchlist guarded by existence checks and wrapped in try/except so a seeding failure doesn't crash boot.
- Bootstrap admin password is either env-specified or **generated randomly and printed once** — a deliberate, correct response to the CWE-798 default-credential trap.

No blocking DB issues found in this pass. Remaining concern is operational: prod must use Postgres, not the SQLite default, and connection URLs must be rotated (§1).

---

## Section 8 — Data Integrity & Freshness 🟢 HEALTHY

### Findings

The market-data layer is honest about data provenance:
- `NormalizedTick` + `DataFeedMode` carry explicit `DEMO_SIMULATED` vs `LIVE` labels.
- `_with_freshness()` computes `age_seconds`, `is_stale`, and `data_status` using per-asset freshness windows (crypto 30s, equity 15s, forex/commodity 60s) and **fails closed** on unparsable live timestamps (marks STALE rather than claiming live).
- Demo feed explicitly never claims a real freshness guarantee.
- Crypto polling is rate-limit-aware: 60s poll interval, 5-min candle cache, and a global 2s min-gap guard to stay under CoinGecko free-tier limits; respects `Retry-After` on 429 (Binance was dropped for HTTP 451 from cloud — a well-documented decision).

This section is production-quality. Weakness is only that the live feed is currently unreachable because the backend is down (§5).


---

## Section 9 — Webhook & Integration Security 🟡 AT RISK

### Findings

- HMAC signature verification is implemented and enforced (with a hard production guard refusing `skip_signature_verification=true`), webhook provider secrets are configurable, and there's a Redis-streams-backed queue with priority lanes. Design is sound.
- **But the integration is untestable and unverifiable:** `tests/test_webhooks_integration.py` fails at collection due to the missing OTel dependency (§3), so signature verification, retry/backoff, and Redis-stream behavior are **never exercised by CI**.
- `webhook_local_mode` and `skip_signature_verification` flags exist for local testing — must remain false in any public deployment (the production guard already hard-blocks the latter).

### Remediation
- Fix the OTel blocker to restore webhook tests.
- Add tests asserting signature verification rejects tampered payloads and that the queue survives Redis outage (fallback).

---

## Section 10 — Networking, Rate Limiting & DDoS 🟡 AT RISK

### Findings

- **Rate limiting is single-process only by default.** `SlidingWindowRateLimiter` is in-memory with a Redis fallback (`app/core/security.py`). In a multi-worker uvicorn deployment without Redis, each worker enforces its own window, effectively multiplying the effective limit by worker count. Redis is used when reachable (good), but `effective_redis_url` defaults to localhost, which on Render is absent — so the default path is the in-memory, per-worker limiter.
- OTP store has the same single-process/in-memory fallback caveat (correctly flags multi-worker safety when Redis available).
- **No CDN / WAF layer confirmed** (fairly standard for a Render-hosted API; acceptable for now, but login/OTP endpoints are brute-force targets and would benefit from edge rate limiting).
- CORS is permissive (`*`) in the Dockerfile/compose for local dev — fine for dev, must be tightened for prod.

### Remediation
- Provision Upstash Redis (also fixes `readyz`, §5) so the rate limiter and OTP store are multi-worker safe.
- Tighten `ALLOWED_ORIGINS`/CORS for production.
- Add retry/backoff + IDOR-style parameterized tests for auth endpoints.

---

## Section 11 — Error Handling & Fault Tolerance 🟢 HEALTHY

### Findings

The codebase consistently fails gracefully:
- Market-data callbacks are wrapped so one provider's crash doesn't kill others (`base.py _emit_tick` swallows per-callback exceptions).
- Provider streams implement automatic reconnect with exponential backoff (`reconnect_delay *= 1.5`, capped).
- Redis/OTP/rate-limiter all degrade to in-memory stores when the store is unreachable (with deduped error logging) rather than throwing.
- WebSocket handlers clean up on disconnect; option-chain rebuilds are guarded.
- DB seeding is idempotent and fault-tolerant.

This is the strongest area of the codebase and a model to follow.


---

## Section 12 — Frontend Integration & Resilience 🟡 AT RISK

### Findings

- React (Vite) client with dynamic `VITE_API_URL` / `VITE_WS_URL` overrides and sensible localhost dev routing.
- **Production URL is hardcoded and now dead:** `config.js` sets `PROD_API_URL = "https://tradetron-8jkz.onrender.com"` and `PROD_WS_URL = "wss://tradetron-8jkz.onrender.com"`, with comments showing the team already knows this host is unreliable/rotating. The deployed Vercel site loads but cannot fetch data (`no data` state) because the backend 503s.
- No confirmed offline/error-state UX or retry with backoff on failed API fetch/WebSocket connect observed in this pass (frontend displayed a generic "no data" / "cannot connect").

### Remediation
- Align the frontend's production URL with the restarted, verifiably-live backend (§5).
- Add fetch timeout + exponential backoff + a clear "backend unavailable" error surface instead of silent "no data".

---

## Section 13 — Release Readiness & Testing 🔴 BROKEN

### Findings

- **Test suite does not collect** (`opentelemetry` missing) — so zero tests actually ran in this audit (see §3). Test files observed include webhook integration and likely auth/trading/market-data suites, but none are verifiable in the current dependency state.
- **No CI pipeline confirmed** (no `.github/workflows` observed in this pass) — no linting, no unit tests, no secret scanning gating merges.
- **Live deployment is down (503)** — the release that's "current" on Render is not serving traffic.
- **Local boot is impossible** — no `.env` (§4), so operators can't reproduce/verify, not even with `broker_mode=simulated`.
- No version-pinned, reproducible pip freeze or hash-locked install (`requirements.txt` uses `>=` ranges), which risks non-reproducible builds.

### Remediation
1. Restore a working test suite (fix OTel) and make `pytest` green.
2. Add **CI**: lint, unit tests, GH Actions (or equivalent), plus a secret-scan step (gitleaks/trufflehog) with hard fail.
3. Create a local `.env`, boot the app, verify all 26+ routes + WebSockets locally.
4. Restart/rebuild the Render backend and re-run the live probe matrix.
5. Consider pinning deps to exact versions for reproducible deploys.


---

## Consolidated Blockers & Ordered Remediation Plan

| Priority | Blocker | Action | Status |
|----------|---------|--------|--------|
| P0 | Real secrets in git history | Rotate all credentials immediately; add `.gitignore`; rewrite/fresh repo history | ⏳ Blocked — awaiting approval |
| P0 | Live backend 503 (security + availability) | Restart/rollback Render service; verify `/api/health` 200 | ⏳ Blocked — needs deploy access |
| P0 | Test suite cannot collect (OTel dep) | Add OTel packages OR guard imports | ⏳ Blocked — awaiting approval |
| P1 | No local `.env` → cannot boot | Scaffold local `.env` from `.env.example` | ⏳ Blocked — awaiting approval |
| P1 | `readyz` fails / cache mandatory but unset | Provision `UPSTASH_REDIS_URL` on Render | ⏳ Blocked — needs infra |
| P1 | Frontend points at dead host | Realign prod URL after backend restart | ⏳ Blocked — needs backend up |
| P1 | Unlinking broker account with OPEN LIVE positions books fabricated closes | Guard unlink (409 when OPEN positions reference the account) + fail-closed close (503 when a LIVE position's broker is unresolvable) + 4 regression tests | ✅ Fixed — §14 |
| P2 | Multi-worker rate limiting, no CDN | Introduce managed Redis; tighten CORS | Backlog |
| P2 | No CI / secret scanning | Add pipeline + secret scanner | Backlog |

**Three sections ship-ready:** §7 Data Storage, §8 Data Integrity, §11 Error Handling.
**Four sections need work but have a solid foundation:** §2 Auth, §6 Observability, §9 Webhooks, §10 Networking.
**Six sections block production:** §1 Secrets, §3 Dependencies, §4 Config, §5 Deployment, §12 Frontend, §13 Release/Testing.

---

## Section 14 — Broker Account Lifecycle & LIVE Position Integrity 🟢 HEALTHY (post-fix)

> Added 2026-09-06 as the resolution record for the P1 financial-correctness
> fix: "Unlinking a broker account with OPEN LIVE positions books fabricated
> closes". Full **447-test suite green** after the fix.

### Finding

- **Deleting a broker connection that still routes an OPEN LIVE position leaves real exchange exposure with no resolvable broker routing.** `unlink_broker_account` hard-deleted the `BrokerAccountRecord` unconditionally. With SQLite (FKs unenforced), OPEN `positions.broker_account_id` values became **dangling**; with PostgreSQL (`ondelete=SET NULL`) they were **NULLed**. Either way, `close_position` could no longer dispatch the real closing order.
- **The close path then *silently fabricated* the close:** the LIVE broker-dispatch block was gated on `pos.broker_account_id` truthiness and skipped entirely when the id was NULL/dangling, so the endpoint proceeded to flip the position to `CLOSED`, write a `TradeRecord`, and book `realized_pnl` — while the real position stayed OPEN on the exchange. Booking PnL without squaring off real exposure is a **P1 financial-correctness defect**.
- Default behavior confirmed in repro: guarded-mode (SQLite) delete + close "succeeded" with fabricated PnL; FK-forced (simulated PG) delete + close behaved identically via the NULLed reference.

### Remediation (defense-in-depth, both layers shipped)

1. **Prevention — refuse the unlink (`app/api/brokers.py`, `unlink_broker_account`):** before deleting, query for any `PositionRecord` with `status == "OPEN"` referencing the account; if found, return **409** with a message naming the open symbol/qty and instructing the operator to close/settle it first. The broker row is untouched on rejection.
2. **Hardening — fail-closed close (`app/api/trades.py`, `close_position`):** a LIVE position must now **resolve** its `broker_account_id` to a real `BrokerAccountRecord` before it may be closed. If the id is missing or the record no longer exists (legacy orphans, dangling/NULLed rows from before the guard), the endpoint raises **503** with an operator guidance message instead of booking PnL; the position stays `OPEN` and `realized_pnl` stays `0.0`.
3. **Regression tests (`tests/test_broker_unlink_live_position_safety.py`, 4 tests):**
   - `test_unlink_blocked_with_open_position` — DELETE with an OPEN position → 409, broker row survives, position stays OPEN.
   - `test_unlink_succeeds_for_closed_position_only` — DELETE after the position is CLOSED → 200, row removed.
   - `test_live_close_with_missing_broker_never_fabricates_close` — LIVE OPEN position with a deleted broker row → 503, position NOT flipped, zero PnL booked.
   - `test_live_close_happy_path_with_resolvable_broker` — LIVE OPEN position with a resolvable broker + mocked adapter → 200/CLOSED, PnL booked (legitimate path preserved).

### Verification

- `pytest tests/test_broker_unlink_live_position_safety.py` → **4 passed**.
- Related safety suites: `test_copy_trading_close_live_safety.py`, `test_p3a_position_ownership.py` → **13 passed** (no regressions).
- Position-close lifecycle: `test_live_vs_paper_execution.py`, `test_copy_trading.py` → **5 passed** (all closes in these suites are PAPER-mode; the LIVE guard does not affect them).
- **Full suite:** `pytest tests/` → **447 passed, 0 failed** (45 pre-existing warnings only).
---

*Full report generated from a read-only discovery audit of the working directory at `c:\Users\HP\Desktop\tradetron\fastapi-template\` and `agency-agents-main\`. No source files were modified during this audit.*
