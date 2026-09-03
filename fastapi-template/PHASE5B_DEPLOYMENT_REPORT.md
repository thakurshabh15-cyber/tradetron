# 🚀 TradeThrone — Phase 5B · Deployed Backend 404 Diagnosis & Fix

**Date:** 2026-09-04 · **Scope:** Restore `https://tradethrone.onrender.com` (currently HTTP 404 on **every** path)

---

## 1. ROOT CAUSE — Render 404 (confirmed, not application code)

### Evidence — live probe of the deployed backend (ground truth)
```
GET https://tradethrone.onrender.com/                       → 404  "Not Found"
GET https://tradethrone.onrender.com/api/health            → 404  "Not Found"
GET https://tradethrone.onrender.com/healthz               → 404  "Not Found"
GET https://tradethrone.onrender.com/readyz                → 404  "Not Found"
GET https://tradethrone.onrender.com/api/market-data       → 404  "Not Found"
GET https://tradethrone.onrender.com/openapi.json          → 404  "Not Found"
GET https://tradethrone.onrender.com/docs                  → 404  "Not Found"
```
Response headers for every path:
```
content-type: text/plain; charset=utf-8
x-render-routing: no-server        ← Render edge says there is NO live server behind this hostname
server: cloudflare
```

**Why this proves it is not the app:** a running FastAPI app always serves `/openapi.json` and `/docs`.
Those also return 404, with a **plain-text** `Not Found` body (not Starlette's JSON `{"detail":"Not Found"}`).
That is Render's **edge** returning `x-render-routing: no-server` — there is **no running web process** bound to the hostname.

### Diagnosis — why there is no server
- The git repo root (`github.com/thakurshabh15-cyber/tradetron`) is `c:\Users\HP\Desktop\tradetron`, which contains:
  - `fastapi-template/` → the **real** backend (`fastapi-template/app/main.py`), and
  - other directories + a stray decoy `main.py` at the repo root.
- The backend's canonical entrypoint is **`fastapi-template/app/main.py`** (`uvicorn app.main:app`).
- `render.yaml` set `startCommand: uvicorn app.main:app` but had **NO `rootDir`**, so Render built/ran the service at the **repo root**, where `app` does not exist.

### Confirmed failure (reproduced)
From the **repo root**:
```
import app.main  →  ModuleNotFoundError: No module named 'app'
```
→ `uvicorn app.main:app` cannot start → Render reports "no active server" → **HTTP 404 `x-render-routing: no-server`** on every path.

This also explains why the deployed backend never reflected the Phase 5 freshness work: the Phase 5 changes were
**uncommitted/unpushed**, and even if pushed, the service never booted.

---

## 2. DEPLOYMENT FIX (minimal, one functional change)

**File:** `fastapi-template/render.yaml`
```yaml
services:
  - type: web
    name: tradetron-backend
    runtime: python
    region: oregon
    plan: starter
    rootDir: fastapi-template          # ← THE FIX — point Render at the backend directory
    buildCommand: pip install --upgrade pip && pip install -r requirements.txt
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
    healthCheckPath: /api/health
```
`rootDir` is the **official Render Blueprint property** for exactly this (confirmed against
https://render.com/docs/blueprint-spec). The health path `/api/health` already matches the app.

No duplicate health endpoints were added and no existing routes were removed.

---

## 3. VERIFIED — corrected entrypoint boots & serves (end-to-end)

Booted the real production command from the **correct working directory** (`fastapi-template/`):
```
uvicorn app.main:app --host 127.0.0.1 --port 8099
```
Real HTTP results:
```
200 /api/health            {"status":"healthy","broker_mode":"simulated","engine_running":true,"ws_channels":{}}
200 /healthz               {"status":"healthy","service":"tradethrone-platform"}
200 /readyz                {"status":"ready","environment":"development","checks":{"database":true,"cache":true}}
200 /api/market-data       { ... BTCUSDT, NIFTY50, ... }
200 /openapi.json          {openapi 3.1.0, title TradeThrone}
```
The exact `startCommand` used by Render → works, once the working directory is `fastapi-template/`.

---

## 4. CONFIG STATE (safe for staging)

From `app/config.py` defaults (current, no env overrides applied locally):
```
environment   = development   (never production by default)
broker_mode   = simulated     (no real broker dispatch)
feed_mode_crypto = demo       (REAL market data OFF — Binance live requires FEED_MODE_CRYPTO=live)
feed_mode_equity  = demo
allowed_origins = *           (dev default; production locks to tradethrone domains automatically)
frontend_url = http://localhost:5173
```
- **No live broker trading can activate** — `BROKER_MODE=simulated` + hard guard (`assert_live_dispatch_allowed`).
- **REAL market data stays OFF** until `FEED_MODE_CRYPTO=live` is explicitly set (not done here).

---

## 5. REDEPLOY REQUIRED — steps (needs Render/GitHub operator access)

The fix is in the repo but **not yet deployed**. The live URL still shows the broken state until you re-run the
Blueprint (or repair the existing service). Choose one:

**Option A — Re-run the Render Blueprint (recommended, clean)**
1. Commit & push: `fastapi-template/render.yaml` (and the other pending Phase 5 changes).
2. Render Dashboard → **New** → **Blueprint** → select `thakurshabh15-cyber/tradetron`.
3. The updated `render.yaml` now carries `rootDir: fastapi-template` → service is built in the backend folder.
4. Set production env vars (see below) → **Apply** → wait for deploy → verify health.

**Option B — Repair the existing service (no blueprint re-create)**
1. Render Dashboard → your `tradetron-backend` Web Service → **Settings**.
2. Set **Root Directory** = `fastapi-template`.
3. **Manual Deploy** → **Clear build cache & deploy**.
4. Verify health.

**Required production env vars (server-side only — never in the frontend):**
```
ENVIRONMENT=production
DATABASE_URL=<postgresql pooler URL>      # or the Render Postgres from the blueprint
JWT_SECRET=<64-char hex>                  # render.yaml already generateValue
FRONTEND_URL=https://tradethrone.vercel.app
ALLOWED_ORIGINS=https://tradethrone.vercel.app,https://tradethron.vercel.app
BROKER_MODE=simulated                     # keep simulated
FEED_MODE_CRYPTO=demo                     # keep demo for now; flip to live only after backend is reachable
LOG_LEVEL=INFO
```
> Note: without `ENVIRONMENT=production`, `readyz` does not require Redis (dev default), which is convenient for a first smoke deploy. For a true production deploy set it and provision Redis.

---

## 6. VERIFY AFTER REDEPLOY
```
curl -i https://tradethrone.onrender.com/api/health       # expect 200, status healthy
curl -i https://tradethrone.onrender.com/healthz          # expect 200
curl -i https://tradethrone.onrender.com/readyz           # expect 200 (db+redis)
curl -i https://tradethrone.onrender.com/openapi.json     # expect 200
```
Expected `x-render-routing`/`server` headers should no longer say `no-server` once a live process serves the host.

---

## 7. HONEST STATUS
- ✅ **Diagnosed root cause** (missing `rootDir` → non-booting service → Render 404 `no-server`).
- ✅ **Applied minimal fix** to `render.yaml` and **proved the corrected command boots & serves all endpoints (200)**.
- ⏳ **NOT yet re-deployed / NOT yet verified under TLS at `tradethrone.onrender.com`** — re-running the Render Blueprint is an operator action that requires Render/GitHub access outside this environment.
- Per the strict rule, **no "deployed"/"production-ready" claim is made** until the live URL returns 200.

---

## 8. RESIDUAL NOTES
- **Railway (secondary path):** `railway.json` uses `Dockerfile` with `dockerfilePath: Dockerfile`, but the Dockerfile is at `fastapi-template/Dockerfile`, so the same root-directory bug applies if deployed from the repo root (set the Railway root directory to `fastapi-template`). Not modified to keep this fix focused on Render.
- **Observed during local boot:** the AngelOne broker layer emits non-fatal `smartConnect` client-login errors with **placeholder/test credentials** (`angel_api_key_12345`, `ANGEL123`, etc.) even in simulated mode. Non-fatal (app still serves 200) but it is an unnecessary outbound call to a real broker endpoint with placeholder creds — flagged for a future cleanup; not part of the 404 fix.
