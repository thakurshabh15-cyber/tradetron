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
   DATABASE_URL=postgresql://postgres.<project-ref>:<DB_PASSWORD>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
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
   UPSTASH_REDIS_URL=rediss://default:<PASSWORD>@<your-db>.upstash.io:6379
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
docker build -t tradethrone-api .
docker run -p 8080:8080 --env-file .env.production tradethrone-api
```
Behind Cloudflare proxy: enable *Full (strict)* SSL + "Always Use HTTPS".

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
| Pro Trader | **₹1,499/mo** | `PRO` | multi-broker · copy trading |
| Creator Pro | **₹4,999/mo** | `CREATOR` | marketplace publishing · payouts |
| Elite Institutional (B2B API) | ₹4,999/mo · ₹49,990/yr | `ELITE` | unlimited algos · priority feeds |

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
