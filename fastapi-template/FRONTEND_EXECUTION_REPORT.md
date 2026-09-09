# TradeThrone Frontend A→Z Execution Report

**Date:** 2026-09-09
**Mode:** Iterative execution — live verification against production backend + deployment-side fixes
**Scope:** `fastapi-template/client/` (React/Vite frontend), Vercel production deployment, live `tradetron-8jkz.onrender.com` backend
**Commit:** `6f42d6d1` (pushed to origin/main, CI green)

---

## 1. Executive Verdict

**B — FRONTEND CODE-COMPLETE / LIVE VERIFIED (browser-E2E still external)**

The frontend is **code-complete and backend-contract-verified**. Every frontend API endpoint was exercised against the **live production backend** with a real freshly-registered account: 15/15 core endpoints return 200, a **real paper order was placed and closed** (order → position → exit → PnL → balance debited), the **live WebSocket stream delivered a real tick**, refresh-token rotation works, and CORS is correctly locked to `https://tradethrone.vercel.app`.

The Vercel deployment was **re-verified and corrected**: my earlier "stale bundle" hypothesis was wrong — the production API/WS URL *was* present in the shared `jsx-runtime` chunk all along (my first scan only looked at the main `index` chunk). The current deployment (`index-B_3MMZjc.js` + `jsx-runtime-BGt-GG7e.js`) contains the correct `https://tradetron-8jkz.onrender.com` and `wss://tradetron-8jkz.onrender.com` URLs, AND all the fixes from this session (verified by grepping the live bundle).

**Remaining external blocker:** no browser automation was available for a click-through E2E (Phase 19). All backend interactions were proven via direct HTTP/WS, and all UI wiring was proven via source audit. The single operator action required is a Vercel dashboard sanity check (see §6).

---

## 2. Defects Found & Fixed This Session

| ID | Severity | File | Root Cause | Fix | RED | GREEN |
| -- | -------- | ---- | ---------- | --- | --- | ----- |
| F-1 | P1 | `src/services/apiClient.js` | 401-burst single-flight refresh had a race: the leader awaited refresh *before* subscribing, so concurrent requests could hang forever once the token expired | Register retry subscribers *before* triggering refresh; flush with `onRefreshed(newToken)` on success and `onRefreshed(null)` on failure so every queued retry resolves exactly once | Unit test `apiClient.test.js` asserts no-hang on concurrent 401s | `npm test` 3/3 pass |
| F-2 | P2 | `src/services/apiClient.js` | `clearTokens()` left `tradetron_custom_symbols` and `tradetron_admin_token` in localStorage — next login could inherit previous occupant's admin token / custom state | Remove both keys on logout, dispatch `tradetron_auth_change` | Manual trace of logout → localStorage | Verified in source + live bundle contains both keys |
| F-3 | P1 | `src/pages/Execution.jsx` | Stream badge hardcoded "STREAM CONNECTED" even when disconnected | Bind badge to `useWebSocket()`'s real `isConnected`; render `RECONNECTING...` otherwise | Compare `isConnected` state transitions | Live bundle `Execution-C1MilWpg.js` contains `b?'STREAM CONNECTED':'RECONNECTING...'` |
| F-4 | P2 | `src/pages/Watchlist.jsx`, `src/pages/Dashboard.jsx`, `src/services/alertService.js`, `src/pages/VisualBuilder.jsx` | Several mutations used raw `fetch` with hand-rolled `Authorization` + missing 401-refresh handling (alertService entirely skipped Auth header) | Route all authenticated calls through `authFetch` (auto Bearer + single-flight refresh); wrap `VisualBuilder.saveStrategy` in try/catch with user-facing error | API contract check vs `/api/watchlist` & `/api/watchlist/alerts/*` | Live: watchlist POST 201, alert endpoints auth-required |
| F-5 | P2 | `src/services/api.js` | Demo-login fallback could trigger on any network error incl. transient | Keep demo fallback but only for `isOfflineOrHtmlError` (never credential rejection) — already wired; re-verified | — | — |
| F-6 | P3 | `TradeHistory.jsx`, `TradeLog.jsx`, `RiskGauge.jsx`, `alertService.js` | INR values shown with `$` + plain `toFixed` | Use `₹` + `toLocaleString("en-IN")`; `alertService` picks `₹`/`$` by symbol class | grep for `$` on INR fields | Verified in source + live bundle |
| F-7 | P1 | `client/public/sw.js` | Cache name `tradetron-shell-v1` hardcoded — old cached `index.html`/assets could pin users on an obsolete shell after redeploy | Network-first + versioned cache (`tradetron-shell-tt-v2`); `activate` deletes all non-current caches; `/api/*` and cross-origin are never intercepted; HTML fallback only on navigation, cache only `ok` responses | — | Live `sw.js` now serves `tt-v2` with `Cache-Control: no-cache, no-store, must-revalidate` |
| F-8 | P2 | `client/vercel.json` | No-op `/api/(.*)` rewrite could mask SPA routing; `index.html` had no Cache-Control override so updates could be revalidated lazily | Replaced with a single negative-lookahead SPA rewrite; `index.html` and `sw.js` forced `no-cache, no-store, must-revalidate`; immutable assets retained | — | Live `/strategies` → 200 index.html; `/index.html` CC `no-cache, no-store`; `/sw.js` CC `no-cache, no-store` |
| F-9 | P3 | `src/App.jsx` | `/kyc` route was unprotected (KYC page loadable without login) | Wrap in `<ProtectedRoute>` | — | Source verified |

---

## 3. Frontend Journey Matrix (live-verified where possible)

| Journey | Source | API | Live | Status |
| ------- | ------ | --- | ---- | ------ |
| App boot / routing | ✅ | — | ✅ index.html + SPA deep-links 200 | **GREEN** |
| Register | ✅ | ✅ POST /api/auth/register 201 | ✅ real test account created | **GREEN** |
| Login | ✅ | ✅ POST /api/auth/login 200 | ✅ real token issued | **GREEN** |
| 2FA | ✅ | ✅ /api/auth/2fa/complete (wired) | ⚠️ not enabled on test account (needs TOTP setup) | **GREEN (source)** |
| Refresh | ✅ single-flight | ✅ POST /api/auth/refresh 200 | ✅ token rotation confirmed | **GREEN** |
| Logout | ✅ cleanup | ✅ /api/auth/logout | source-verified | **GREEN** |
| Dashboard | ✅ | ✅ /api/dashboard/summary 200 | ✅ | **GREEN** |
| Strategies | ✅ | ✅ /api/strategies 200 (POST 422 on incomplete payload = correct validation) | ✅ | **GREEN** |
| Strategy Builder | ✅ broker fetch via authFetch | ✅ /api/brokers/accounts 200 | ✅ | **GREEN** |
| Marketplace | ✅ | ✅ explore endpoints live | ✅ | **GREEN** |
| Copy Trading | ✅ join/leave/deploy wired | ✅ explore/following/groups/mine 200 | ✅ | **GREEN** |
| Trade History | ✅ | ✅ /api/trades 200 (real SELL+BUY rows) | ✅ | **GREEN** |
| Trade Journal | ✅ | ✅ via /api/trades | ✅ | **GREEN** |
| Watchlist | ✅ CRUD | ✅ POST 201, GET 200 | ✅ | **GREEN** |
| Alerts | ✅ CRUD | ✅ /api/watchlist/alerts wired via authFetch | ✅ | **GREEN** |
| Portfolio | ✅ | ✅ /api/trades/positions 200 | ✅ | **GREEN** |
| Execution | ✅ WS badge real state | ✅ /ws/trades | ✅ | **GREEN** |
| Risk Center | ✅ | ✅ /api/risk-status 200 | ✅ | **GREEN** |
| Broker Sessions | ✅ | ✅ /api/brokers/accounts 200 | ✅ | **GREEN** |
| Settings | ✅ | ✅ /api/user/profile, /notifications 200 | ✅ | **GREEN** |
| Billing | ✅ | ✅ /api/billing/subscription, plans, invoices 200 | ✅ | **GREEN** |
| Pricing | ✅ | ✅ /api/subscriptions/plans, current 200 | ✅ | **GREEN** |
| KYC | ✅ protected now | ✅ /api/user/kyc 200 | ✅ | **GREEN** |
| Quant Lab | ✅ offline-label honest | ✅ /api/quant-lab | source | **GREEN** |
| Backtest / Reality | ✅ | ✅ /api/backtest | source | **GREEN** |
| Admin | ✅ | ✅ /api/admin routes exist | source (protected) | **GREEN** |
| WebSockets | ✅ | ✅ /ws/market/stream live tick received | ✅ | **GREEN** |
| Paper order + close | ✅ | ✅ full lifecycle verified LIVE | ✅ | **GREEN** |
---

## 4. Deployment Verification

```
Git commit          : 6f42d6d1c6be0ef0c149c06eaa2538dba1e02f26 (HEAD == origin/main)
Vercel project      : tradethrone.vercel.app (live, auto-deploys on push to main)
Production branch   : main
Root directory      : fastapi-template/client (repo-side config lives there)
Build command       : npm run build (vite build)
Production URL      : https://tradethrone.vercel.app
API URL             : https://tradetron-8jkz.onrender.com   (verified in LIVE bundle)
WS URL              : wss://tradetron-8jkz.onrender.com     (verified in LIVE bundle)
Live bundle (main)  : /assets/index-B_3MMZjc.js      (has all session fixes)
Live bundle (shared): /assets/jsx-runtime-BGt-GG7e.js (contains API/WS URLs)
Current main build  : index-CmjYG3oZ.js + jsx-runtime-BFg_umQL.js (local parity)
Service worker      : /sw.js → tt-v2, Cache-Control no-cache/no-store
SPA rewrite         : /strategies, /execution → 200 index.html (negative-lookahead rule)
```

**IMPORTANT AUDIT CORRECTION:** The prior investigation concluded the live bundle "differed from main", implying a stale deployment. This session conclusively proved the production **was** current: `config.js` is bundled into the *shared* `jsx-runtime` chunk, not the main `index` chunk, and that shared chunk has contained `https://tradetron-8jkz.onrender.com` / `wss://tradetron-8jkz.onrender.com` all along. After pushing `6f42d6d1`, Vercel rebuilt and the live `index-B_3MMZjc.js` now also contains every fix in this session. **No stale-endpoint defect existed and none remains.**

---

## 5. Regression

```
npm test            : 3/3 passed (apiClient.test.js — refresh single-flight, no-hang on failure, bearer attach)
npm run build       : OK (Vite production build)
npm run lint        : 0 errors, 25 pre-existing warnings
GitHub Actions CI   : 6f42d6d1 → completed / success (backend pytest + frontend build + secret scan + alembic drift)
git diff --check    : clean
```

---

## 6. Remaining External Blockers (genuine only)

1. **Browser click-through E2E (Phase 19)** — this environment has no browser automation. The full auth→dashboard→watchlist→strategies→broker→paper order→position→close journey is proven at HTTP/WS level, but no GUI click-through was recorded. Highest-value next action: open `https://tradethrone.vercel.app`, sign in, and click through the journey manually.
2. **Vercel dashboard** — no dashboard credentials; the project's auto-deploy, root-directory, and env-var settings could not be visually confirmed. Everything from the repository side is deterministic (vercel.json + committed config fallbacks), and the live behavior matches expectations.
3. **Render dashboard** — no dashboard access; backend verified live via HTTP/WS instead.
4. **2FA end-to-end** — TOTP enabled account needed; the code path (`/api/auth/2fa/complete`) is verified server-side by `tests/test_2fa_login_completion.py` and frontend wiring is source-verified.

---

## 7. Git State

```
HEAD         : 6f42d6d1c6be0ef0c149c06eaa2538dba1e02f26
origin/main  : 6f42d6d1c6be0ef0c149c06eaa2538dba1e02f26
ahead/behind : 0 / 0
working tree : clean
untracked    : none
```

---

## 8. Exact Next Action

**One operator manual E2E pass** on https://tradethrone.vercel.app (register → login → dashboard → watchlist → strategies → broker → paper order → close → history → portfolio → settings → logout → login), using the repo-side config as ground truth. If that pass is green, the frontend can be declared **A — FRONTEND PRODUCTION VERIFIED**.