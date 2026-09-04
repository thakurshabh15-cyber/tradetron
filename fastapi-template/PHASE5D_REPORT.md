# Phase 5D — Live Deployment, Real Data Provenance & Operational Readiness

**Date:** 2026-09-04 · **Status:** RESUME after VS Code / agent crash — recovered from the
exact checkpoint the previous agent left, preserved completed work, fixed the two
inconsistencies found, verified, and prepared for deployment.

> This report **merges and updates** the earlier Phase 5 findings (see
> `PHASE5_REPORT.md`). It does not discard prior work. Where Phase 5B/5C/5D
> conclusions were superseded by live verification, the newer conclusion wins.

---

## RECOVERY CHECKPOINT (what the previous agent had done when the crash hit)

- **Completed (locally implemented + uncommitted, tests and build verified green):**
  - URL truth fix in `client/src/config.js` → production backend is
    `https://tradetron-8jkz.onrender.com` (the legacy `tradethrone.onrender.com` host
    is dead — verified HTTP 404 `x-render-routing: no-server`).
  - Backend market-data freshness model: `data_freshness_*` settings in
    `app/config.py`; `NormalizedTick.age_seconds()` / `is_stale()` in
    `app/market_data/base.py`; `_with_freshness()` / `_count_stale_for()` in
    `app/market_data/unified_manager.py`, wired into `get_quote`, `get_snapshot`,
    both WebSocket broadcast channels, and `get_providers_status`.
  - Frontend provenance badges: `DataStatusBadge` in `MarketTicker.jsx` and
    `ProvenanceBadge` in `MarketDetail.jsx` (REAL / DEMO / STALE).
  - `render.yaml`: `rootDir: fastapi-template` (fixes the old Render 404), `ENVIRONMENT
    = production`, CORS locked to the Vercel frontends, `BROKER_MODE=simulated`,
    `FEED_MODE_CRYPTO=live` (market data only).
  - `tests/test_env_separation.py` updated for the removed `.env.production`.
  - New `tests/test_market_freshness.py` (+7 freshness tests).
  - `PHASE5_REPORT.md` (the Phase 5 report that predates the 5D resume).

- **In progress / inconsistent when the crash hit (fixed during this resume):**
  - `render.yaml` contained a **placeholder** `UPSTASH_REDIS_URL =
    rediss://REPLACE_WITH_UPSTASH_TLS_URL`. This must never be committed/applied. It
    was redacted and replaced with a BLOCKED note (see section 6).
  - Two Vercel serverless routes (`client/api/orders/place.js`,
    `client/api/trades/execute.js`) still hard-coded the **dead**
    `tradethrone.onrender.com` as the backend fallback — inconsistent with the URL
    truth. Updated to `tradetron-8jkz.onrender.com`.

- **Still pending (requires operator / infrastructure action; NOT completed by the
  agent):**
  - **Deploying** the Phase 5D backend changes to Render (the live backend is still
    running the previous build — it reports `environment=development`, no freshness
    metadata on quotes, crypto in demo mode, and `cache=false`).
  - **Providing a real Redis URL** (Upstash TLS) in Render — Redis is BLOCKED on
    external infrastructure; `cache=true` cannot be claimed until a real endpoint is set.
  - **Observing real production crypto ticks** before labeling the feed LIVE.

- **What this resume resumed from:** the branch point right before the operator-side
  deployment step of Phase 5D, with all local code complete, tested (194 passed) and
  built (frontend), two small consistency defects fixed, and the report scaffolded.

---

## 1. LIVE ARCHITECTURE

```
Vercel (SPA + serverless)
   │  REST https://tradetron-8jkz.onrender.com/api/*
   │  WS   wss://tradetron-8jkz.onrender.com/ws? ...
   ▼
Render backend (uvicorn app.main:app)  — https://tradetron-8jkz.onrender.com
   ├─ PostgreSQL (Supabase / managed Render DB)   → /readyz database=true
   ├─ Redis/Upstash (TLS)                         → /readyz cache=BLOCKED (see §6)
   ├─ Market-data providers (crypto=CoinGecko REST, equity/forex=DEMO)
   ├─ WebSocket manager (market:<SYMBOL>, market:stream)
   └─ Webhook queue / rate limiter / OTP store (Redis-backed when available)
```

BROKER_MODE remains **simulated**; real-money broker dispatch is impossible (guarded).

## 2. ACTUAL BACKEND URL

`https://tradetron-8jkz.onrender.com`

- Verified 2026-09-04: `/api/health` → 200 (`broker_mode=simulated`,
  `engine_running=true`); `/healthz` → 200; `/readyz` → 200 (degraded: `cache=false`);
  `/api/market-data/quote/BTCUSDT` → 200.
- The legacy `https://tradethrone.onrender.com` is DEAD (HTTP 404 `x-render-routing:
  no-server` on every path) — a different, conflicting Render service no longer serves.

## 3. FRONTEND → BACKEND URL

`client/src/config.js` resolves the API base with this precedence:
`VITE_API_URL` → `VITE_API_BASE_URL` → localhost dev → `PROD_API_URL =
https://tradetron-8jkz.onrender.com`.

WebSocket URL (`WS_BASE`) likewise uses `wss://tradetron-8jkz.onrender.com` unless a
`VITE_WS_URL` override is set. The two Vercel serverless routes
(`client/api/orders/place.js`, `client/api/trades/execute.js`) now also fall back to the
live host (was `tradethrone.onrender.com`).

> Deployment note: if a `VITE_API_URL`/`VITE_WS_URL` override is not set on Vercel, the
> compiled bundle falls back to the live host automatically. Keeping the hard-coded
> PROD_* constants pointing at the live host is the source of truth.

## 4. ENVIRONMENT STATUS

- **Local default:** `environment=development` (`app/config.py` default).
- **Render blueprint (`render.yaml`) now sets `ENVIRONMENT=production`** and locks CORS —
  but this has **NOT yet been deployed**.
- **Live now (dated 2026-09-04):** `GET /readyz` reports `environment=development`
  because the currently-running Render instance predates the blueprint change.
- Production fail-fast guards in `app/config.py` enforce a strong `JWT_SECRET` (≥32
  chars) and forbid `SKIP_SIGNATURE_VERIFICATION=true` before `environment=production`
  will boot. `render.yaml` uses `generateValue: true` for `JWT_SECRET`, satisfying this.
- **Verification target:** after deploy, /readyz must report `environment=production`.

## 5. DATABASE STATUS

- **DONE / verified live:** `GET /readyz` → `checks.database=true` (PostgreSQL reachable).
- `/api/health`, `/healthz`, `/api/market-data` all 200.

## 6. REDIS STATUS — BLOCKED (requires external infrastructure)

- **Root cause of `cache=false` + `localhost:6379 connection refused`:** no managed
  Redis URL is set, so the app falls back to `settings.redis_url =
  redis://localhost:6379/0`, and Render has nothing listening there.
- **Code support (already present):** `settings.effective_redis_url` prefers
  `upstash_redis_url` (TLS) over the plain `redis_url`; `redis_url`/`upstash_redis_url`
  fall back gracefully to in-memory stores when unreachable (rate limiter, OTP, webhook
  queue), so the service still boots in single-process mode.
- **ACTION REQUIRED (operator):** in the Render dashboard for `tradetron-backend`, add
  environment variable `UPSTASH_REDIS_URL = rediss://<host>:6379` (a managed TLS Redis,
  e.g. Upstash) **without any credentials committed to the repo**. A prior placeholder
  value (`rediss://REPLACE_WITH_UPSTASH_TLS_URL`) was removed from `render.yaml` — it
  must not be re-added.
- **Verification target:** `GET /readyz` → `checks.cache=true` (only claim production
  `cache=true` after this is observed live).
- In production (`ENVIRONMENT=production`), `/readyz` will return HTTP **503** until the
  cache passes (see `cache_required = settings.environment == "production"` in
  `app/main.py`), so Redis is a hard production-readiness block.

## 7. CORS STATUS

- `ALLOWED_ORIGINS="*"` is invalid with `allow_credentials=True`. `render.yaml` now
  locks `ALLOWED_ORIGINS` to `https://tradethrone.vercel.app,https://tradethron.vercel.app`
  and sets `FRONTEND_URL=https://tradethrone.vercel.app`.
- The app also auto-locks to these domains when `ENVIRONMENT=production`.
- **Not yet deployed** — CORS lock takes effect on next Render deploy.

## 8. WEBSOCKET STATUS

- Backend exposes `market:<SYMBOL>` and `market:stream` channels with freshness
  metadata now attached to every broadcast tick (via `_with_freshness`).
- **Live now:** `/api/health` reports `ws_channels={}` (engine running but no active
  client channel subscriptions at probe time) — this is expected pre-deploy and does not
  indicate a defect.
- Frontend `useWebSocket` connects to `wss://tradetron-8jkz.onrender.com` (via
  `config.js`). After deploy, verify a subscribed client actually receives
  `data_status` on ticks.

## 9. MARKET DATA PROVIDER MATRIX

| Asset class | Provider | Feed mode (current) | LIVE feed path | Demo fallback |
| :--- | :--- | :--- | :--- | :--- |
| CRYPTO | `CryptoMarketDataProvider` | `demo` (live pending deploy) | CoinGecko public REST (`PUBLIC_EXCHANGE_STREAM`) — credential-free | `DEMO_SIMULATED` |
| EQUITY | `IndianEquityMarketDataProvider` | `demo` (needs Angel One creds) | Angel One WS when `feed_mode_equity=live` + creds | `DEMO_SIMULATED` |
| FNO | above | demo | above | above |
| COMMODITY | above | demo | above | above |
| FOREX | `ForexMarketDataProvider` | `demo` (always) | none (no free real-time forex API) | `DEMO_SIMULATED` |

Classification (honest): **CRYPTO = REAL (implemented, enable via
`FEED_MODE_CRYPTO=live`) · EQUITY/FNO/COMMODITY = DEMO/UNVERIFIED (no creds) · FOREX =
DEMO/UNAVAILABLE**. Nothing is silently upgraded to LIVE without observing real ticks.

## 10. REAL / DEMO / STALE STATUS

- Freshness model implemented and unit-tested (194 passed, incl. 7 new):
  - `data_status ∈ {LIVE, DEMO, STALE, UNKNOWN}`, `is_stale`, `age_seconds`.
  - **DEMO feeds are never flagged STALE** (`is_stale=None`, `data_status="DEMO"`).
  - **Real feeds fail closed**: an old or unparsable timestamp is reported `STALE`,
    never LIVE.
  - `get_quote`, `get_snapshot`, and both WS broadcast channels carry the metadata.
- Frontend badges render REAL / SIMULATED / STALE from that metadata.
- **Not yet deployed.** Live `/api/market-data/quote/BTCUSDT` currently returns
  `feed_mode=DEMO_SIMULATED` (no `data_status`) — i.e. the old build.

## 11. PAPER TRADING STATUS

- `BROKER_MODE=simulated` (live verified). Paper flow: frontend → API → simulated broker
  → order → fill → position → P&L → balance → close → history, entirely in-process.
- Paper orders never reach a real broker; the simulated broker is the engine's broker
  unless `BROKER_MODE=live` AND a live broker is connected.
- Verified by `tests/test_live_mode_guard.py` (`test_paper_order_never_blocked_when_simulated`).

## 12. LIVE TRADING SAFETY STATUS

- `assert_live_dispatch_allowed()` remains active on all eight dispatch paths
  (REST order, DMA, position close, strategy engine; webhook path safe by construction)
  — confirmed present in `app/api/trades.py`, `app/engine/trading_engine.py`,
  `app/brokers/__init__.py`.
- `mode="LIVE"` is hard-blocked (HTTP 403) while `BROKER_MODE != live`
  (`tests/test_live_mode_guard.py::test_live_order_blocked_when_broker_mode_simulated`).
- `order_manager.py` was **not modified** — no defect was proven.

## 13. AUTH / SECURITY STATUS (unchanged by Phase 5D)

- JWT + OTP, strong-JWT & skip-signature production fail-fast guards (see §4).
- CORS locked to production frontends (see §7).
## 14. TEST RESULTS

- Backend: `python -m pytest -q` → **194 passed** (matches the previous verified
  baseline exactly; includes +7 from the Phase 5 freshness tests and no weakening).
- The count is unchanged from the 194 baseline because the +7 new freshness tests were
  already added during Phase 5 with no other tests removed.

## 15. BUILD RESULTS

- Frontend: `npm run build` → **built successfully** (includes the provenance badge
  changes). Dist output present.

## 16. DEPLOYMENT VERIFICATION

- **PENDING — operator action required (Render).** The uncommitted Phase 5D backend
  changes are ready to commit/push; after the commit triggers a Render redeploy
  (GitHub → Render), verify the ACTUAL live backend:
  - `/api/health` → 200 · `/healthz` → 200 · `/readyz` → 200
  - `/readyz` → `environment=production`, `database=true`, `cache=true` (needs Redis, §6)
  - `/api/market-data/quote/BTCUSDT` → fresh `data_status=LIVE|STALE`
- Vercel: after the frontend build/deploy, confirm the deployed SPA reaches
  `tradetron-8jkz.onrender.com` and shows the REAL/DEMO/STALE badge.

## 17. ISSUES FOUND

1. `cache=false` / `localhost:6379` — no managed Redis URL (BLOCKED, §6).
2. `environment=development` on the live backend — blueprint sets `production`, but the
   change is not deployed yet (§4).
3. Crypto feed still demo live — `FEED_MODE_CRYPTO=live` not deployed (§9).
4. Two Vercel serverless routes pointed at the dead `tradethrone.onrender.com` (FIXED).
5. `render.yaml` had an un-committable placeholder Redis URL (FIXED — redacted).

## 18. FIXES IMPLEMENTED

- `client/api/orders/place.js`, `client/api/trades/execute.js`: dead backend fallback →
  live host.
- `render.yaml`: removed the bogus `UPSTASH_REDIS_URL` placeholder and documented the
  required manual env var (Redis stays BLOCKED rather than shipping a fake endpoint).

## 19. REMAINING UNVERIFIED ITEMS

- Production `/readyz` cache=true (needs Redis endpoint + deploy).
- Production `environment=production` (needs deploy).
- Real production BTCUSDT/ETHUSDT ticks observed and confirmed LIVE (needs deploy with
  `FEED_MODE_CRYPTO=live`).
- WebSocket connection from the deployed frontend receiving `data_status`.
- CORS lock against the live Vercel origin.
- Deployed frontend badge rendering.

## 20. EXACT NEXT ACTIONS

1. **(Operator) Render:** add `UPSTASH_REDIS_URL` (managed TLS Redis) as an env var,
   with no committed secret.
2. **(Agent) Commit** the verified Phase 5D changes (this report, freshness model,
   frontend badges, URL fixes, render.yaml) — no secrets, no placeholder.
3. **(Operator) Push** → Render redeploys the backend + Vercel redeploys the frontend.
4. **(Agent) Verify live:** `/healthz`, `/api/health`, `/readyz`
   (`environment=production`, `database=true`, `cache=true`).
5. **(Agent) Verify market data:** `GET /api/market-data/quote/BTCUSDT` (and
   ETHUSDT) return fresh `data_status=LIVE`; hot-restart ticks stay LIVE; stop feeding →
   STALE (never DEMO).
6. **(Agent) Verify WS:** deployed frontend receives `data_status` over `market:stream`.
7. **(Agent) Verify CORS:** production frontend calls succeed with credentials.
8. **(Agent) Update this report** with the live verification evidence once deployed.

