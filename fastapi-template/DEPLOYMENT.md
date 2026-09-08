# 🚀 TradeThrone — Production Deployment Manual

**Version:** 1.0 · **Status:** ✅ Verified against live codebase
**Stack:** React 19 + Vite (Vercel) · FastAPI + Uvicorn (Render/Railway) · Supabase PostgreSQL · Upstash Redis

---

## 0. Pre-Flight Checklist

- [x] `pytest` → **156/156 passed** (114 core + 42 Phase 3 staging)
- [x] `npm run build` → zero-warning `/dist`
- [x] `/healthz` → `{"status": "healthy", "service": "tradethrone-platform"}`
- [x] `/readyz` → HTTP 200 when Supabase DB + Upstash Redis reachable
- [x] Alembic schema baseline verified — `alembic check` reports no drift on a clean DB
- [x] CORS locked to `https://tradethrone.vercel.app`, `https://tradethron.vercel.app`
- [x] HMAC webhook verification enforced (`SKIP_SIGNATURE_VERIFICATION=false`)
- [x] JWT HS256 + configurable `ACCESS_TOKEN_EXPIRE_MINUTES=1440`

---

## 1. Supabase — PostgreSQL (Database of Record)

1. Create project at <https://supabase.com> → note the **project ref** (`<project-ref>`).
2. **Connection Pooler** (mandatory for ASGI/serverless workloads):
   `Project Settings → Database → Connection Pooling → Transaction mode (port 6543)`.
   ```
   DATABASE_URL=<paste your Supabase connection pooler URL here>
   ```
   > The app auto-normalizes `postgresql://` → `postgresql+asyncpg://`. **Schema
   > is managed by Alembic** (introduced in Phase 3). For a new database run
   > `alembic upgrade head` — do NOT rely on the legacy `init_db()` bootstrapper,
   > which historically created schema drift. See `STAGING.md` §5 and `alembic/`.
3. **RLS:** TradeThrone enforces authorization at the API layer via SQLAlchemy session scoping (`get_db` + per-user query filters). If you additionally expose the DB via Supabase client SDKs, enable RLS and add `ENABLE ROW LEVEL SECURITY;` per table with owner-only policies:
   ```sql
   ALTER TABLE users ENABLE ROW LEVEL SECURITY;
   CREATE POLICY owner_only ON users FOR ALL USING (auth.jwt() ->> 'sub' = id::text);
   ```
4. Copy pooler URL into backend env var `DATABASE_URL`.

---

## 2. Upstash — Serverless Redis (TLS)

1. Create Redis DB at <https://upstash.com> (same region as backend).
2. Copy the **TLS endpoint** (must start with `rediss://`):
   ```
   UPSTASH_REDIS_URL=<paste your Upstash TLS endpoint here>
   ```
3. Used by: webhook queue (Redis Streams), rate limiting, idempotency store, readiness probe.

---

## 3. Backend — Render / Railway / Cloudflare

### Option A: Render (Blueprint included — `render.yaml`)
1. New → **Blueprint** → point at repo root `fastapi-template/`.
2. Replace env vars with production values from `.env.production` (**never commit real secrets**):
   | Key | Value |
   |---|---|
   | `DATABASE_URL` | Supabase pooler URL |
   | `UPSTASH_REDIS_URL` | `rediss://…` |
   | `JWT_SECRET` | 64-char hex (`python -c "import secrets;print(secrets.token_hex(32))"`) |
   | `JWT_ALGORITHM` | `HS256` |
   | `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` |
   | `SKIP_SIGNATURE_VERIFICATION` | `false` ← **strict HMAC enforced** |
   | `ALLOWED_ORIGINS` | `https://tradethrone.vercel.app,https://tradethron.vercel.app` |
   | `FRONTEND_URL` | `https://tradethrone.vercel.app` |
   | `BROKER_MODE` | `simulated` (flip to `live` post-KYC) |
   | Broker keys | `ANGEL_*`, `DHAN_*`, `FYERS_*`, `ZERODHA_*`, `BINANCE_*` |
   | Feed keys | `ALPACA_API_KEY/SECRET`, `OANDA_API_TOKEN` |
   | Payments | `RAZORPAY_KEY_ID/SECRET` (`rzp_live_…`), `STRIPE_SECRET_KEY` (`sk_live_…`) |
3. Start command (already in blueprint):
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
   ```
4. Health check path: `/healthz` (liveness) / `/readyz` (readiness).
5. SSL: automatic (`*.onrender.com`) — add custom domain for full HSTS preload eligibility.

### Option B: Railway (`railway.json` committed)
```bash
railway init && railway up          # Dockerfile build, auto SSL
railway variables set DATABASE_URL=… UPSTASH_REDIS_URL=… JWT_SECRET=…
```

### Option C: Docker (any host incl. Cloudflare Workers-facing LB)
```bash
cd fastapi-template
docker build -t tradethrone-api .      # uses fastapi-template/Dockerfile
docker run -p 8080:8080 --env-file .env.production tradethrone-api
```
> Do NOT `docker build` from the repo root — the root `Dockerfile` has been
> removed; the only container build context is `fastapi-template/`.
> Behind Cloudflare proxy: enable *Full (strict)* SSL + "Always Use HTTPS".

---

## 4. Frontend — Vercel

1. Import repo → **Root Directory:** `fastapi-template/client` → Framework: **Vite**.
2. Build settings (auto-detected): Build `npm run build` · Output `dist`.
3. Environment Variables (Production scope):

   ```env
   VITE_API_URL=https://<your-backend>.onrender.com
   VITE_WS_URL=wss://<your-backend>.onrender.com
   ```
4. `vercel.json` already ships SPA rewrites + immutable asset caching.
5. Domains: `tradethrone.vercel.app` (primary) and alias `tradethron.vercel.app`.
6. Post-deploy smoke test: login → market ticker streams over WSS → place paper order.

---

## 5. Monetization Engine — Verified Infrastructure

| Tier | Price | Seeded Code | Entitlements |
|---|---|---|---|
| Free Starter | ₹0 | `FREE` | 1 broker · 3 algos · no copy trading |
| Pro Trader | **₹7,999/mo** · ₹76,790/yr | `PRO` | multi-broker · copy trading |
| Creator Pro | **₹14,999/mo** · ₹1,43,990/yr | `CREATOR` | marketplace publishing · payouts |
| Elite Institutional (B2B API) | ₹24,999/mo · ₹2,39,990/yr | `ELITE` | unlimited algos · priority feeds |

- **Marketplace split:** creators retain **80%** (`CREATOR_REVENUE_SHARE = 0.80`) / platform treasury **20%** — automated in `app/engine/subscription.py`.
- Razorpay Live handles INR subscriptions; Stripe Live for global cards/enterprise invoicing. Webhook signatures verified via `RAZORPAY_WEBHOOK_SECRET` (HMAC).

---

## 6. Go-Live Runbook (ordered)

1. Provision Supabase + Upstash → collect URLs.
2. Deploy backend (Render/Railway) with all `.env.production` values → verify `/readyz` = 200.
3. Deploy frontend on Vercel with `VITE_API_URL`/`VITE_WS_URL`.
4. DNS: map both Vercel domains; enable HTTPS-only.
5. Register admin → seed plans auto-created on first boot.
6. Flip `BROKER_MODE=live` only after SEBI KYC + broker API activation; TOTP sessions auto-renew daily 08:45 IST via built-in scheduler.
7. Monitor: Sentry DSN optional; structured JSON logs ship to platform stdout.
---

## 7. Production Hardening — P2 (CI, Alembic Governance, Observability)

Facts for operators; each item maps to a completed P2 hardening workstream.

### 7.1 CI gate (`.github/workflows/ci.yml`)
The repository-root workflow runs on every push/PR against `fastapi-template/` and
must pass before merge:
1. Backend suite: `pytest` with `BROKER_MODE=simulated` and `ENVIRONMENT=testing`
   (full ~250-test baseline; safety guarantees require the simulated env).
2. `pip check` (warns only on the **undeclared** `kiteconnect` legacy pin —
   see §7.4).
3. Alembic drift guard: `python scripts/ci_alembic_check.py` — single-head,
   clean `upgrade head` on an isolated temp SQLite, and ORM-schema parity.
4. Frontend production build: `npm ci && npm run build` in `client/`.
5. Secret scan: greps the diff for committed credential patterns.

### 7.2 Alembic owns the schema in production (P2-6)
- **Schema authority:** Alembic migrations (`alembic/versions/`) are the only
  schema owner in production. `app.db.session.init_db()` **refuses** to run
  `create_all` or the legacy ad-hoc `ALTER TABLE` list when
  `ENVIRONMENT=production`; it seeds reference data only. On a fresh DB run:
  ```bash
  alembic upgrade head
  ```
- **Automatic migrate-before-serve (all plans):** `app/db/migrations.py`
  provides `run_migrations()` which the FastAPI lifespan executes as its
  **first** startup step, before `init_db()` and before any request can be
  served (uvicorn does not accept connections until the lifespan completes).
  Every boot therefore runs `alembic upgrade head` using the same runtime
  `DATABASE_URL`; on an already-current schema it is an idempotent no-op.
  If the migration fails the app **fails closed** — startup aborts and no
  request is served ($7.5 recovery runbook). This is what makes the **Render
  Free tier** self-healing: Free has no Pre-Deploy / Release command and the
  dashboard-created service ignores `render.yaml`, so the app-startup gate is
  the only reliable migration hook available.
- **Blueprint-only releaseCommand (paid plans):** `render.yaml` also declares
  `releaseCommand: alembic upgrade head` as a best practice for Blueprint-managed
  services on paid plans (idempotent — Alembic's version table makes re-runs a
  no-op). Both mechanisms coexist safely; the startup gate is authoritative on
  Free.
- **Drift guard:** `python scripts/ci_alembic_check.py` validates the chain on
  an isolated temp DB (no network, no production datastore). Baseline
  `0001_baseline` generates the schema from the ORM (`Base.metadata.create_all`),
  so treat **new model changes as migration changes** (add a revision, do not
  rely on create_all to self-adapt):
  ```bash
  alembic revision --autogenerate -m "describe change" && alembic upgrade head
  ```

### 7.3 Observability (P2-3 / P2-4)
- **Logging:** production emits single-line JSON (`timestamp`, `level`,
  `logger`, `message`, plus caller `extra` context) via `app/core/logging.py`;
  dev/testing keeps the human-readable format. `setup_logging()` is idempotent
  under uvicorn reload.
- **Metrics:** main API exposes Prometheus text format at `GET /metrics`
  (`tradetron_http_requests_total`, `tradetron_engine_state`,
  `tradetron_broker_mode_live`, `tradetron_ws_channels`). The webhook service
  exposes its own `GET /metrics` (`webhook_*` series). Scrape both.
- **Secrets in logs:** alerting paths (`app/core/monitoring.py`) redact
  configured secrets and no longer print the Telegram bot-token prefix.

### 7.4 Dependency consistency (P2-5)
- `pyproject.toml` and `requirements.txt` are kept in sync (CI installs from
  `requirements.txt`; uv/local installs from `pyproject.toml`).
- **Known, accepted `pip check` warning:** the Zerodha live SDK
  (`kiteconnect==5.2.1`) pins legacy `autobahn==19.11.2`; it is deliberately
  NOT declared and shows up only if installed manually for Zerodha live
  trading. Fresh CI installs (no kiteconnect) report no conflicts. Install it
  only on a dedicated Zerodha-live host:
  ```bash
  pip install kiteconnect==5.2.1
  ```

### 7.5 Defense-in-depth broker dispatch (P2-10)
Beyond the call-path guards, every real-broker adapter now gates at its own
function boundary (`AngelOneBroker/ZerodhaKiteBroker/UpstoxBroker.place_order`
and `BinanceBroker._api_request`), so a direct class-level call can never reach
a broker while `BROKER_MODE != live`.

### 7.6 Repository-root hygiene (P2-8)
Files at the repo root (`AUDIT.md`, `AUDIT_PROD.md`, `PRODUCTION_READINESS_AUDIT.md`)
are documentation; the real, deployable application lives entirely in
`fastapi-template/`.
The **stale decoy deployment files** (`Dockerfile`, `requirements.txt`,
`init_db.py`, `.dockerignore`) that previously sat at the repo root and would
build a non-functional image (webhook-only entrypoint, 5-installed packages,
legacy `create_all`) were **removed** in this pass, and the root `.gitignore`
now covers `*.db-shm`/`*.db-wal` alongside `*.db`. Operators should never type
`docker build .` / `pip install -r requirements.txt` at the repo root; the
canonical Docker/build context is `fastapi-template/`. Untracked scratch
artifacts (`trading.db`, `_scan*.txt`, `logs/`, `__pycache__/`,
`_deployed_*.js`, `_ws_verify.py`) should be deleted by an operator.
