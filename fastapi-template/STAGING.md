# TradeThrone — Phase 3: Staging & Controlled Integration Validation

**Status:** ✅ STAGING READY (no real money / no live trades / no real users)
**Date:** 2026-09-04

This is the authoritative **Phase 3 staging verification checklist**. Every
item maps to an automated test or a documented procedure. The staging suite
runs fully **offline** — no broker sandbox/testnet keys, no payment live keys,
no Redis/Docker required.

---

## ⚡ Guardrail (immutable)

- **`BROKER_MODE=simulated` is the source-code default.** Flipping to `live`
  requires a deliberate config change and passes production fail-fast guards.
- **Runtime LIVE-routing guard:** Every real-broker order dispatch
  (`/api/trades/order`, DMA, position close, and the strategy engine) calls
  `assert_live_dispatch_allowed()`. If a request specifies `mode="LIVE"` (even
  from a user with a connected broker account) while `BROKER_MODE != live`, the
  dispatch is hard-blocked with **HTTP 403** and no order reaches any broker.
  Verified by `tests/test_live_mode_guard.py`.
- **No real credentials** (broker, payment, email/SMS) may go in `.env.staging`
  or be committed to `tests/`. The simulated broker + sandbox Razorpay are the
  only execution targets in staging.
- **`ENVIRONMENT=production` refuses to boot** without a strong `JWT_SECRET`
  (≥32 chars) and with `SKIP_SIGNATURE_VERIFICATION=true`.

---

## ✅ Verification Matrix (9 areas)

| # | Area | Status | Automated Test(s) |
| :-: | :--- | :--- | :--- |
| 1 | Broker adapters (offline harness) | ✅ VERIFIED | `test_staging_brokers.py` |
| 2 | Trading safety (risk gate + LIVE/Paper separation) | ✅ VERIFIED | `test_staging_safety.py`, `test_live_mode_guard.py` |
| 3 | Payments (sandbox) | ✅ VERIFIED | `test_staging_payments_otp.py::TestRazorpayGateway` |
| 4 | OTP & notifications | ✅ VERIFIED | `test_staging_payments_otp.py::TestOTP` |
| 5 | DB / migrations (Alembic) | ✅ VERIFIED | `test_staging_db.py` |
| 6 | Credential encryption at rest | ✅ VERIFIED | `test_staging_payments_otp.py::TestCredentialEncryption` |
| 7 | Redis multi-worker / rate-limit | ✅ VERIFIED (live Redis 7.4) | `test_staging_redis.py`, `scripts/_phase4_redis_multiprocess.py` |
| 8 | Webhook HMAC validation | ✅ VERIFIED | `test_staging_webhook_config.py` |
| 9 | Deployment hardening / config guards | ✅ VERIFIED | `test_staging_webhook_config.py::TestPaperLiveSeparation` |

---

## 1. Broker adapters — offline harness

All brokers implement `BrokerClient` (`app/brokers/base.py`). Verified:

- **SimulatedBroker** is a complete paper-trading path: `connect` → `place_order`
  (instant fill) → `get_positions`/`get_holdings`/`get_margins`; `sell` closes a
  position fully.
- **Live brokers refuse to operate with missing credentials** — Zerodha, Upstox,
  Binance raise a clear `RuntimeError`; Angel One `validate_credentials()` returns
  `False` for short/placeholder keys. No misconfigured staging box can route real
  capital.

> **Known limitation / remaining `UNVERIFIED` sub-item:** no broker sandbox/testnet
> keys are available on this machine, so real network round-trips to test endpoints
> are not yet verified. Binance ships `testnet=True` (`https://testnet.binance.vision`)
> ready for testing the moment a testnet keypair is provisioned.

## 2. Trading safety

`app/engine/risk_manager.py` is consulted **before every order** reaches the broker.
Verified: kill-switch blocks/reset restores; daily-loss circuit-breaker; margin gate;
Auto-Pilot kill-switch on N consecutive losses or intraday drawdown; winning trade
resets the streak; guard is disable-able.

## 3. Payments — sandbox / unconfigured

`app/core/payment_gateway.py` produces a **sandbox `order_...` id** with no keys
configured and rejects invalid payment signatures. Real gateway calls need test-mode
keys (unavailable here — sub-item `UNVERIFIED`).

## 4. OTP & notifications

`app/core/security.py`: 6-digit OTP keyed by identifier; **single-use** (second
verify fails); **isolated per identifier** (user A's code never verifies user B).
`app/engine/email_service.py` returns `{dispatched: False, provider: "unconfigured"}`
without a `RESEND_API_KEY`, so staging never emails real users.

## 5. DB & migrations — Alembic (NEW in Phase 3)

This project previously had **no migration system** — the legacy `init_db()`
bootstrapper used `create_all()` + hand-maintained `ALTER TABLE` fallbacks and
produced **schema drift** (the legacy `trading.db` was missing dozens of indexes/FKs
and carried an orphaned `mobile_otps` table). Phase 3 introduces Alembic:

```
alembic/versions/
  0001_baseline.py            # full ORM schema (create_all-based)
  0002_drop_mobile_otps.py    # removes orphaned legacy table
alembic/env.py                # async-aware, uses app normalize_database_url()
```

**Bringing an existing legacy-bootstrapped DB under Alembic:**
```bash
alembic stamp 0001_baseline && alembic upgrade head
```

**Fresh staging DB:**
```bash
DATABASE_URL=postgresql://... alembic upgrade head
```

`alembic check` on a clean DB reports **no drift** (test-verified); on the legacy
`trading.db` it flags the missing indexes/FKs — proving the migration system catches
what the bootstrapper missed.

## 6. Credential encryption at rest

`app/core/crypto.py` encrypts broker keys/tokens with **Fernet** (AES-128-CBC +
HMAC-SHA256, key derived from `JWT_SECRET`). Tests verify round-trip fidelity and
unique ciphertexts per identical plaintext (random IV). `mask_secret()` prevents
secret leakage into logs/UI.

## 7. Redis multi-worker & rate limiting — 🟡 PARTIAL

`app/core/security.py` uses Redis for rate limiting/OTP/shared state with an
**in-process sliding-window fallback** so the app runs single-node without Redis.
The staging suite exercises the fallback. The Redis-backed multi-worker paths are
`UNVERIFIED` until a Redis instance is available (see `SETUP.md`).

## 8. Webhook HMAC validation

`app/webhooks/validation/signatures.py` verifies every provider webhook with
constant-time HMAC-SHA256. Verified: valid accepted; invalid/tampered rejected;
missing header rejected; `sha256=`/`v1,` prefixes handled; TradeThrone verifier
rejects payload tampering.

## 9. Deployment hardening & config fail-fast

`app/config.py` refuses to boot `ENVIRONMENT=production` unless `JWT_SECRET` ≥32
chars **and** `SKIP_SIGNATURE_VERIFICATION=false`. Default `broker_mode` is
`"simulated"`; default `environment` is `"development"`.

---

## 📋 Manual staging smoke checklist (operator)

- [ ] `BROKER_MODE=simulated` in deployed env
- [ ] `ENVIRONMENT=staging` (not `production`)
- [ ] No real broker/payment/email keys in `.env.staging`
- [ ] `alembic upgrade head` applied; `alembic check` → no drift
- [ ] Payments in sandbox/unconfigured mode only
- [ ] `SKIP_SIGNATURE_VERIFICATION=false`
- [ ] Live Redis up for multi-worker (else single node)
- [ ] Admin bootstrap password rotated after first login

