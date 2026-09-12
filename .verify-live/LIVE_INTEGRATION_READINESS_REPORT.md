# LIVE INTEGRATION READINESS REPORT
## TradeThrone Phase 15 — Real Market + Broker Integration Assessment

**Date:** 2026-09-11  
**RC Baseline:** Commit `8990719` on `main`  
**Method:** Read-only architecture audit of every pipeline stage  
**Classification:** REAL · DEMO · MOCK · FALLBACK · MISSING per stage

---

---
## PHASE 15B — HONEST, FAIL-CLOSED BROKER / EQUITY DISPLAY (2026-09-12)

**Status:** ✅ SHIPPED — validated end-to-end (**19/19** local E2E + standalone repro + unit gate).

**What changed**
- **Broker-truth state engine** — `app/engine/broker_state_sync.py`, `app/models/broker_state.py`,
  migration `alembic/versions/0007_broker_state.py`.  Per-account persisted snapshot (positions hash,
  margins, equity/P&L, `captured_at`, `last_good_captured_at`, `sync_message`) with a **derived
  freshness status**: LIVE only when genuinely broker-sourced AND captured within the
  `broker_state_stale_after` (120s) threshold; otherwise STALE / ERROR / UNAVAILABLE / PAPER.
- **Fail-closed adapters** — `app/brokers/upstox.py` (and the shared sync path): margin queries
  **raise** on non-200 / invalid JSON / network failure instead of returning a fabricated ₹0.00.
- **Honest dashboard + portfolio** — `app/api/dashboard.py` `_broker_equity_block`,
  `client/src/pages/Dashboard.jsx`, `client/src/pages/Portfolio.jsx`: broker-truth equity/P&L are
  never substituted with the paper balance; missing fields render as "—"; STALE (amber) / ERROR (rose)
  badges surface; `LIVE · ZERODHA` only when the snapshot is fresh.
- **Hard-block in simulated mode** — `BROKER_MODE != live` ⇒ Zerodha adapter `connect()` raises ⇒
  `POST /sync` returns **502** `LIVE broker connection blocked: BROKER_MODE is not 'live'` ⇒ an
  ERROR snapshot is persisted (fail-closed, never fabricated numbers; last-good values preserved).
- **Scheduler** — `app/main.py` lifespan: per-account broker-state sync at
  `broker_state_sync_interval` (60s), sandboxed to BROKER_MODE.

**Validation evidence**
| Check | Result |
|---|---|
| `pytest tests/test_broker_state_sync.py` (derive/gate/margins-normalize suite) | PASS |
| Standalone repro: LIVE → STALE → sync 502 (BROKER_MODE) → ERROR snapshot | PASS |
| `_p15b_local_e2e.mjs` (PAPER unchanged; LIVE/STALE/ERROR truthful; mobile; 0 page/console errors) | **19/19 PASS** |

**Deliberately unchanged:** PAPER tenant accounting/rendering — all PAPER assertions still pass as-is.

---

---

## PHASE 15C — EXCHANGE-LEVEL PROTECTIVE ORDERS (2026-09-12)

**Status:** ✅ BACKEND SHIPPED — exchange-level protective-order engine, durable per-leg
ledger and fail-closed LIVE entry verified (**23/23** unit + **5/5** E2E + **786** full-suite
regression).  **Client/ frontend rendering of protection truth is still pending** (backend
truth exists and is exposed via the API).

**What changed**
- **Durable protective-order ledger** — `app/models/protective_order.py` + migration
  `alembic/versions/0008_protective_orders.py`.  One row per protection leg
  (STOP_LOSS / TAKE_PROFIT) with broker reference, status lifecycle
  (PLACED → COMPLETE / CANCELLED / FAILED / RESOLVED), `last_error`, and a unique
  `(position_id, leg)` index — a second live leg is rejected at the DB.
- **Honest position protection lifecycle** — `app/engine/protective_orders.py`
  (`ProtectiveOrderManager`): states UNPROTECTED / PAPER / PROTECTION_PENDING /
  PROTECTED / PROTECTION_FAILED / STOP_TRIGGERED / TARGET_TRIGGERED; `reconcile_once`
  sweep (stale-refresh, broker-truth status pull, local resolution of orphans, bounded
  re-arm that **never re-places while the previous broker order could still be live**).
- **Fail-closed LIVE entry** — `app/api/trades.py`: a LIVE entry with SL/TP commits its
  order/trade/position as PROTECTION_PENDING **before** dispatch; protection is armed as
  soon as the fill is durable; **any arm failure tears the position down**
  (`cancel_position_protection` + CLOSED + PROTECTION_FAILED + honest `protection_error`);
  the user gets a **503 failure-closed** response (told protection could not be
  guaranteed).  Brokers that authorise an order but return no `order_id` now **raise**
  instead of fabricating a `"UPSTOX_ORDER"` reference (`app/brokers/upstox.py`) — a fake
  reference would falsely mark a position PROTECTED against an order that does not exist
  on the exchange.
- **Open-positions API exposes protection truth** — `GET /api/v1/orders/open-positions`
  now returns `stop_loss_price`, `take_profit_price`, `protection_state`,
  `protection_error`, `protected_at`.

**Validation evidence (2026-09-12)**
| Check | Result |
|---|---|
| `pytest tests/test_protective_orders.py` (23) — lifecycle, idempotency, replace/cancel honesty, crash recovery, tenant isolation, reconcile contract | **23/23 PASS** |
| `pytest tests/test_protective_orders_e2e.py` (5) — PAPER no-arm; LIVE entry→replace→close; LIVE SIMULATED-broker fail-closed arm; broker-unavailable 502 no-ghost-position; protective-reject 503 FAIL-CLOSED tear-down | **5/5 PASS** |
| **Cancel-gated re-arm** — FAILED row with a live broker ref is never re-placed while the broker rejects the cancel (`_CancelFailingBroker` fixture) | **PASS** |
| Related trading/broker/reconciliation suites (11 files) | **77 passed** |
| Full backend regression `pytest tests/ -q` | **786 passed, 0 failed, 3 benign warnings** |
| Migrations — single head `0008_protective_orders`, no dev-DB drift | **PASS** |

**Remaining before real-money production** (in addition to §10 list)
- Independent broker-sandbox verification of real SL/TP place / cancel / fill semantics
  per adapter (Zerodha / Upstox / Angel One) with live credentials.
- **Client/ dashboard rendering of the new protection fields** (badges, actions, error
  lanes) — the backend truth exists; frontend rendering is a 15C follow-up.
- Real-time WebSocket market data + periodic live position sync (15C follow-up track).
## 1. COMPLETE PIPELINE MAP

```
MARKET DATA → NORMALIZATION → STRATEGY ENGINE → SIGNAL → RISK ENGINE
  → ORDER INTENT → BROKER ADAPTER → BROKER ORDER → ORDER STATUS
    → RECONCILIATION → POSITION → P&L → EQUITY → UI
```

---

## 2. STAGE-BY-STAGE ASSESSMENT

### 2.1 MARKET DATA

| Dimension | Detail |
|---|---|
| **Files** | `app/market_data/base.py`, `unified_manager.py`, `manager.py`, `simulator.py` |
| **Providers** | `providers/indian_equity.py`, `providers/crypto.py`, `providers/forex.py` |
| **Classification** | **DEMO + FALLBACK** (see detail below) |
| **Persistence** | In-memory quote cache (`UnifiedMarketDataManager._quotes`) — no DB persistence |
| **Failure Handling** | Per-symbol try/except in tick loops; exponential backoff reconnect on provider errors |
| **Tenant Isolation** | N/A (public feed) |
| **Paper/Live Separation** | `DataFeedMode` enum: `DEMO_SIMULATED`, `LIVE_BROKER_VENDOR`, `PUBLIC_EXCHANGE_STREAM` |

**Per-Provider Classification:**

| Provider | DEMO Mode | LIVE Mode | Actual Data Source in LIVE | Classification |
|---|---|---|---|---|
| Indian Equity | Random walk around seed prices | Micro-ticks around yfinance anchor (15s refresh) | yfinance REST (delayed, not streaming) | **FALLBACK** |
| Crypto | Random walk simulation | CoinGecko REST polling (60s interval) | CoinGecko free public API | **REAL** (delayed) |
| Forex | Always DEMO (`forex_live = False`) | N/A | N/A | **DEMO** |
| Option Chain | Black-Scholes model with IV surface | Same model, re-priced with live spot tape | Live spot from other providers | **MOCK** |

**Critical Gap:** No real-time WebSocket market data feed exists. The original Binance WebSocket was removed (HTTP 451 from cloud IPs). No Angel One SmartStream or Zerodha Ticker SDK is integrated. Even in `LIVE_BROKER_VENDOR` mode, Indian equity ticks are synthetic micro-fluctuations around a 15-second-delayed yfinance anchor price.
---

### 2.2 NORMALIZATION

| Dimension | Detail |
|---|---|
| **Files** | `app/market_data/base.py` (NormalizedTick), `app/brokers/position_normalizer.py` |
| **Classification** | **REAL** — fully implemented |
| **Data Model** | `NormalizedTick` with `age_seconds()`, `is_stale()`, `to_dict()` |
| **Position Normalization** | 5 adapters: Zerodha, Upstox, Angel One, Binance, Simulated — canonical contract (symbol, signed quantity, side, avg_price) |
| **Failure Handling** | Invalid timestamps → stale (fail-closed); zero-quantity positions → dropped |
| **Paper/Live Separation** | `DataFeedMode` embedded in every NormalizedTick |

### 2.3 STRATEGY ENGINE

| Dimension | Detail |
|---|---|
| **Files** | `app/engine/strategy_evaluator.py`, `trading_engine.py`, `visual_strategy.py` |
| **Classification** | **REAL** — fully implemented for both paper and live |
| **Strategy Types** | SMA Crossover (50/200), Visual Builder (no-code), External webhook signals |
| **Indicators** | RSI, SMA, EMA, MACD, Bollinger Bands, ATR, VWAP, crossover detection |
| **Persistence** | Strategies in `strategies` table with `execution_mode` (PAPER/LIVE) |
| **Failure Handling** | Per-tick try/except; strategy errors don't crash engine |
| **Paper/Live Separation** | `execution_mode` field; `broker_account_id` for live routing |
| **Risk Integration** | OrderManager validates risk constraints before every broker dispatch |

### 2.4 SIGNAL

| Dimension | Detail |
|---|---|
| **Files** | `app/webhooks/handlers/tradethrone_signal.py`, `app/webhooks/ingress/`, `app/webhooks/queue/redis_streams.py` |
| **Classification** | **REAL** — fully implemented with Redis-backed queue |
| **Ingress** | Signature-verified webhook endpoints with HMAC validation |
| **Idempotency** | Redis-backed idempotency store with configurable TTL (5 min default) |
| **Queue** | Redis Streams with consumer groups; bounded worker pool |
| **Resiliency** | Circuit breaker, bulkhead isolation, rate limiter |
| **Durability** | Durable PENDING claim pattern — signal → DB claim → broker dispatch → CAS finalize |

### 2.5 RISK ENGINE

| Dimension | Detail |
|---|---|
| **Files** | `app/engine/risk_manager.py`, `app/api/risk_guard.py` |
| **Classification** | **REAL** — fully implemented |
| **Controls** | Kill switch (manual + auto-pilot), circuit breaker (daily loss), position size limits, order rate limiting |
| **Auto-Pilot** | Consecutive loss tracking, intraday drawdown from peak P&L |
| **Persistence** | In-memory state (resets daily by design) |
| **Enforcement** | Pre-trade gate: `risk_check()` before every order submission |

### 2.6 ORDER INTENT

| Dimension | Detail |
|---|---|
| **Files** | `app/schemas/trading.py`, `app/api/dma_engine.py`, `app/api/trades.py` |
| **Classification** | **REAL** — fully implemented |
| **Entry Points** | Market order (`POST /api/trades/order`), DMA (`POST /api/v1/orders/execute-dma`) |
| **Pre-Trade Analytics** | Lot-size auto-correction, asset classification, statutory charges, margin calculation |
| **Idempotency** | Optional `client_order_id` with durable PENDING claim pattern |
| **Tenant Isolation** | Server-derived `user.id` from JWT; client-supplied `user_id` ignored |

### 2.7 BROKER ADAPTER

| Dimension | Detail |
|---|---|
| **Files** | `app/brokers/base.py`, `angelone.py`, `zerodha.py`, `upstox.py`, `binance.py`, `simulated.py`, `__init__.py` |
| **Classification** | **REAL** — 4 production adapters + simulated |
| **Abstract Interface** | `BrokerClient` ABC: connect, place_order, modify_order, cancel_order, get_order_status, get_positions, get_margins, get_holdings |
| **SDKs Used** | AngelOne: `smartapi-python` (sync→to_thread); Zerodha: `kiteconnect`; Upstox: `httpx` REST; Binance: `aiohttp` + HMAC-SHA256 |
| **Timeout** | 30s hard timeout on every SDK call (`DISPATCH_TIMEOUT_SECONDS`) |
| **Failure Handling** | Real broker errors re-raised (never masked with fabricated success) |

**BROKER_MODE Safety (Defense-in-Depth):**

| Layer | Guard | Effect |
|---|---|---|
| Call-path | `assert_live_dispatch_allowed()` | Blocks order dispatch unless `BROKER_MODE=live` |
| Adapter | Each real adapter calls `assert_live_dispatch_allowed()` | Cannot bypass via direct class method call |
| Connection | `assert_live_broker_connect_allowed()` | Blocks broker login in simulated mode |
| Network | Binance `_api_request()` calls the guard | Blocks all HTTP requests in non-live mode |

### 2.8 BROKER ORDER

| Dimension | Detail |
|---|---|
| **Classification** | **REAL** — all 4 adapters execute real API calls |
| **Angel One** | `client.placeOrder()` via SmartAPI SDK with symbol token resolution |
| **Zerodha** | `kite.place_order()` via KiteConnect SDK |
| **Upstox** | `POST https://api.upstox.com/v2/order/place` with OAuth Bearer |
| **Binance** | `POST /api/v3/order` with HMAC-SHA256 signed parameters |
| **Fail-Safe** | Real broker errors are never masked; re-raised to caller |

### 2.9 ORDER STATUS

| Dimension | Detail |
|---|---|
| **Classification** | **REAL** — all 4 adapters query real broker APIs |
| **Angel One** | `client.getOrderHistory()` via SmartAPI SDK |
| **Zerodha** | `kite.orders()` via KiteConnect SDK |
| **Upstox** | `GET /api/v2/order/retrieve-all` via HTTP |
| **Binance** | `GET /api/v3/order` with HMAC signing |

### 2.10 RECONCILIATION
| Dimension | Detail |
|---|---|
| **Files** | `app/engine/order_reconciliation.py`, `app/brokers/postback.py` |
| **Classification** | **REAL** — comprehensive crash-recovery and postback |
| **Window-A (fresh)** | PENDING orders < 120s: skipped (in-flight) |
| **Window-B (stale, broker ref)** | PENDING > 120s with `broker_order_id`: `get_order_status()` |
| **Window-C (no broker ref)** | PENDING without ref: `get_positions()` match |
| **Postback** | Signature-verified (V3 HMAC); broker-account-bound; CAS mutual exclusion |
| **Scheduler** | Background: startup pass + periodic interval |
| **Duplicate Prevention** | CAS rowcount=1 claim; `FINALIZED_ORDER_STATUSES` tuple |

### 2.15 PROTECTIVE ORDERS (Phase 15C — exchange-level SL/TP)

| Dimension | Detail |
|---|---|
| **Files** | `app/engine/protective_orders.py`, `app/models/protective_order.py`, migration `0008`, `app/api/trades.py`, `app/brokers/upstox.py` |
| **Classification** | **REAL — backend shipped**; Client/ U.I. rendering pending |
| **Ledger** | One DB row per protection leg; unique `(position_id, leg)` index; status lifecycle PLACED → COMPLETE / CANCELLED / FAILED / RESOLVED |
| **Lifecycle** | UNPROTECTED → PAPER → PROTECTION_PENDING → PROTECTED → PROTECTION_FAILED / STOP_TRIGGERED / TARGET_TRIGGERED |
| **Arming** | After FILLED confirmation; failure tears down the position (CLOSED + PROTECTION_FAILED + 503) — fail-closed, never a silent unprotected LIVE position |
| **Reconcile** | `reconcile_once`: broker-truth status pull, local orphan resolution, cancel-gated re-arm (no re-poke while a prior broker order may still be live) |
| **No fabricated refs** | Adapters raise when an accepted order has no `order_id` — the `"UPSTOX_ORDER"` placeholder was removed |

---

### 2.11 POSITION

| Dimension | Detail |
|---|---|
| **Files** | `app/models/trading.py` (PositionRecord), `app/engine/order_manager.py` |
| **Classification** | **REAL** — DB-persisted position lifecycle |
| **Lifecycle** | Created on FILLED → OPEN → CAS close → CLOSED |
| **Tenant Isolation** | `user_id` scoped; index on (user_id, symbol) |
| **Paper/Live Separation** | `mode` field (PAPER/LIVE) |

### 2.12 P&L

| Dimension | Detail |
|---|---|
| **Classification** | **PARTIAL** — Paper is REAL; Live P&L not from broker truth |
| **Paper P&L** | Owner-scoped `credit_paper_pnl()`: `paper_balance = 1,000,000 + Σ(realized_pnl)` |
| **Live P&L** | Calculated from entry_price vs current_price — NOT synced from broker |
| **Realized P&L** | Recorded on position close; fed to auto-pilot risk guard |

### 2.13 EQUITY

| Dimension | Detail |
|---|---|
| **Classification** | **REAL (Phase 15B)** — broker-truth equity snapshot now wired to dashboard/portfolio via `_broker_equity_block` (freshness-derived, fail-closed, paper never substituted)
---

### 2.14 UI

| Dimension | Detail |
|---|---|
| **Classification** | **REAL** — full frontend for all pipeline stages |
| **Market Feed** | `useMarketStore` → WebSocket `/ws/market/stream` with auto-reconnect |
| **Private Feeds** | `useWebSocket` → `/ws/trades`, `/ws/events` with JWT auth |
| **Reconnect** | Frontend: exponential backoff [1s, 2s, 4s, 8s, 15s]; no-reconnect on 4001/4003/4408 |
| **Paper/Live** | `LiveOptInModal` gates LIVE switch; checks `connectedBrokers.length > 0` |

---

## 3. BROKER READINESS MATRIX

| # | Question | Status | Evidence |
|---|---|---|---|
| 1 | Which broker integrations exist? | ✅ **4 BROKERS** | Angel One, Zerodha, Upstox, Binance + Simulated |
| 2 | Which market-data integrations exist? | ⚠️ **PARTIAL** | CoinGecko (crypto, REST), yfinance (equity anchor), no WebSocket feeds |
| 3 | Credentials safe for production? | ✅ **YES** | AES-256 encrypted at rest (`BrokerAccountRecord`); `BROKER_MODE` env gate |
| 4 | OAuth/token lifecycle real? | ✅ **YES** | Zerodha OAuth, Upstox OAuth 2.0, Angel One TOTP+JWT, Binance HMAC. Daily 8:45 AM IST auto-renewal cron. |
| 5 | WebSocket market data real? | ❌ **NO** | No SmartAPI SmartStream, no Kite Ticker. CoinGecko REST (60s) is closest to real. |
| 6 | Instrument mapping deterministic? | ⚠️ **PARTIAL** | Angel One scrip master sync (3000+); static catalogue; no Zerodha contract master |
| 7 | Order placement implemented? | ✅ **YES** | All 4 adapters have real `place_order()` |
| 8 | Order status updates implemented? | ✅ **YES** | All 4 adapters have real `get_order_status()` |
| 9 | Broker fills reconcile into trades/positions? | ✅ **YES** | Postback + reconciliation engine with CAS mutual exclusion |
| 10 | Live equity/P&L from broker truth? | ❌ **NO** | P&L internal calc; `get_margins()` not wired to equity curve |
| 11 | Disconnect/reconnect handled? | ⚠️ **PARTIAL** | Market data: reconnect with backoff. Broker: daily cron only. No heartbeat. |
---

## 4. SECURITY RISKS

| Risk | Severity | Current Mitigation | Remaining Exposure |
|---|---|---|---|
| Broker credentials leaked | HIGH | AES-256 encrypted at rest; never logged; masked in API responses | — |
| Unauthorised live order | CRITICAL | 3-layer BROKER_MODE guard; JWT auth on all endpoints | — |
| Webhook signature bypass | HIGH | V3 HMAC verification; `webhook_local_mode` bypass only in dev | Configurable `webhook_local_mode` flag must be `False` in production |
| Cross-tenant order mutation | HIGH | Postback bound to order's own broker account; reconciliation user-scoped | — |
| Frontend as authoritative state | MEDIUM | Frontend WS feed is display-only; trading state server-derived | — |
| Fake data presented as live | MEDIUM | `DataFeedMode` enum; `data_status` FRESH/STALE enrichment | `feed_mode_equity=live` yields yfinance anchor (not streaming) |
| Orphan positions on engine crash | HIGH | Reconciler finalizes pending rows | SL/TP are engine-internal, not exchange-placed |

---

## 5. MISSING PIECES (Gaps)

### CRITICAL GAPS (block real-money trading)

| # | Gap | Impact | Current State |
|---|---|---|---|
| C-1 | **No real-time WebSocket market data** | Order prices may be stale; SL/TP on 15s-old data | CoinGecko REST 60s (crypto), yfinance 15s anchor (equity), no WebSocket |
| C-2 | **No live position sync from broker** | Internal positions diverge from broker truth | `get_positions()` exists; no periodic sync job |
| C-3 | **No live equity/P&L from broker** | Portfolio shows internal calc, not broker balance | `get_margins()` exists; not wired to equity curve |
| C-4 | **SL/TP not on exchange** | Engine crash leaves orphan positions unprotected | Engine-internal monitoring only |

### IMPORTANT GAPS (degrade reliability)

| # | Gap | Impact | Current State |
|---|---|---|---|
| I-1 | **No broker connection heartbeat** | Stale session undetected until next op fails | 8:45 AM IST daily cron only; no intraday ping |
| I-2 | **No market hours enforcement** | Orders rejected outside hours | No trading calendar in codebase |
| I-3 | **Indian equity feed is synthetic in LIVE** | `LIVE_BROKER_VENDOR` = fake micro-ticks around real anchor | Honest labelling but misleading UX |
| I-4 | **No broker order expiry management** | GTD orders may expire without notification | No expiry management |

### MINOR GAPS (enhance robustness)

| # | Gap | Impact | Current State |
|---|---|---|---|
| M-1 | **Risk state not persisted** | Kill switch resets on restart | By design — daily reset |
| M-2 | **No multi-leg order atomism** | Visual strategy legs execute sequentially | Partial fill = partial execution |
| M-3 | **No Zerodha contract master sync** | May miss new F&O contracts | Angel One master synced; Zerodha not |
| 12 | Duplicate orders prevented? | ✅ **YES** | Idempotency keys, CAS claims, unique client_order_id, concurrent duplicate → 409 |
| 13 | Live mode impossible without broker + safety? | ✅ **YES** | `BROKER_MODE=live` required; 3-layer guard (call-path + adapter + connection) |
| **Paper Equity** | Starting balance 1M + cumulative realized P&L |
---

## 6. EXACT IMPLEMENTATION PLAN

### Phase 15A: Real-Time WebSocket Market Data (CRITICAL)

| Step | Task | Files | Priority |
|---|---|---|---|
| 1 | Integrate SmartAPI SmartStream for Angel One WebSocket LTP feed | `app/market_data/providers/indian_equity.py` | P0 |
| 2 | Integrate Kite Ticker for Zerodha WebSocket feed | `app/market_data/providers/indian_equity.py` | P0 |
| 3 | Wire CoinGecko; accept 60s REST as sufficient for crypto | `app/market_data/providers/crypto.py` | P1 |
| 4 | WebSocket health monitoring (ping/pong, reconnect on drop) | `app/market_data/providers/*.py` | P0 |
| 5 | Data freshness SLA check before order dispatch | `app/engine/trading_engine.py` | P0 |

### Phase 15B: Live Position & Equity Sync (CRITICAL)

| Step | Task | Files | Priority |
|---|---|---|---|
| 1 | Background position sync (poll `get_positions()` every 60s in LIVE) | `app/engine/broker_cron.py` or new `app/engine/position_sync.py` | P0 |
| 2 | Wire `get_margins()` into equity curve (periodic broker balance poll) | `app/engine/broker_cron.py`, `app/api/dashboard.py` | P0 |
| 3 | Detect internal-vs-broker drift; alert and pause strategy on divergence | New `app/engine/position_reconciliation.py` | P1 |

### Phase 15C: Exchange-Level Order Protection (CRITICAL)

| Step | Task | Files | Priority |
|---|---|---|---|
| 1 | ~~Place GTC/SL-M orders on broker after FILLED confirmation~~ — **DONE (15C)** | `app/engine/protective_orders.py` + broker adapters | P0 |
| 2 | ~~Protective SL-M + target limit placed in same transaction as fill~~ — **DONE (15C)** | `app/models/protective_order.py`, `app/engine/protective_orders.py`, migration `0008` | P0 |
| 3 | ~~Modify/cancel protective orders on position close~~ — **DONE (15C)** | `cancel_position_protection` in `app/engine/protective_orders.py` | P0 |
| 4 | Independent broker-sandbox verification of real SL/TP semantics (Zerodha/Upstox/Angel One) | Broker adapters + sandbox credentials | P0 |
| 5 | Client/ rendering of protection truth (badges, actions, error lanes) | `client/src/pages/` (Positions/Dashboard) | P1 |

### Phase 15D: Broker Health & Market Calendar (IMPORTANT)

| Step | Task | Files | Priority |
|---|---|---|---|
| 1 | Broker session heartbeat (ping every 5 min; alert on failure) | `app/engine/broker_cron.py` | P1 |
| 2 | NSE/BSE/MCX/Binance trading calendar (holidays + session hours) | New `app/market_data/trading_calendar.py` | P1 |
| 3 | Pre-trade market-hours check before order dispatch | `app/engine/risk_manager.py` | P1 |
| 4 | Market-open auto-start / market-close auto-halt for strategies | `app/engine/trading_engine.py` | P2 |

### Phase 15E: Equity & P&L Hardening (IMPORTANT)

| Step | Task | Files | Priority |
|---|---|---|---|
| 1 | Broker balance snapshot → `PositionRecord.current_price` updated from broker | `app/engine/position_sync.py` | P1 |
| 2 | Equity curve data source: broker margins for live, internal for paper | `app/api/dashboard.py` | P1 |
| 3 | Realized P&L reconciliation: broker trade history vs internal ledger | New `app/engine/pnl_reconciliation.py` | P2 |

---

## 7. BLOCKERS

| # | Blocker | Type | Resolution |
|---|---|---|---|
| B-1 | No real-time WebSocket market data feed | Technical | Integrate SmartAPI SmartStream / Kite Ticker SDKs |
| B-2 | No live position sync from broker | Technical | Implement background position reconciliation job |
| B-3 | No live equity from broker truth | Technical | Wire `get_margins()` into periodic sync |
| B-4 | No exchange-level SL/TP orders | Technical | **RESOLVED (15C backend)** — `ProtectiveOrderManager` + fail-closed LIVE entry; remaining: broker-sandbox validation + U.I. rendering |
| B-5 | No broker API credentials for testing | Operational | Need sandbox/testnet credentials per broker |
| B-6 | SmartAPI/Kite SDK packages may need install | Dependency | `pip install smartapi-python kiteconnect` (conditionally imported) |

---

## 8. RECOMMENDED IMPLEMENTATION ORDER

```
Phase 15A  →  Real-Time WebSocket Market Data (P0 — foundation)
Phase 15B  →  Live Position & Equity Sync (P0 — prevents divergence)
Phase 15C  →  Exchange-Level SL/TP (P0 — crash protection)
Phase 15D  →  Broker Health & Market Calendar (P1 — operational maturity)
Phase 15E  →  Equity & P&L Hardening (P1 — reporting accuracy)
```

**Rationale:** Market data is the foundation — without real-time prices, strategy evaluation, SL/TP triggers, and P&L all operate on stale/synthetic data. Position sync prevents the most dangerous failure mode: orphan positions the system doesn't know about. Exchange-level SL/TP is the single most important safety measure for live trading.
| **Live Equity** | `get_margins()` exists on adapters but not wired to equity curve |
| **Frontend** | `EquityCurve` component feeds from trade data |
| **Gap** | No periodic sync of broker margins → internal equity state |
---

## 9. WHAT REMAINS SOLID FROM RC BASELINE

| Component | Status |
|---|---|
| Broker adapter abstraction (BrokerClient ABC) | ✅ Production-ready |
| BROKER_MODE 3-layer safety guard | ✅ Production-ready |
| AES-256 credential encryption | ✅ Production-ready |
| OAuth/token lifecycle (Zerodha, Upstox, Angel One) | ✅ Production-ready |
| Daily TOTP renewal cron (8:45 AM IST) | ✅ Production-ready |
| Order idempotency (durable claim pattern) | ✅ Production-ready |
| Postback signature verification (V3) | ✅ Production-ready |
| Order reconciliation (3-window crash recovery) | ✅ Production-ready |
| Risk engine (kill switch, circuit breaker, rate limit) | ✅ Production-ready |
| Paper accounting (owner-scoped, once-per-close) | ✅ Production-ready |
| WebSocket connection manager (tenant-scoped) | ✅ Production-ready |
| Position normalizer (5 adapters) | ✅ Production-ready |
| DMA engine (lot-size correction, statutory charges) | ✅ Production-ready |
| Frontend reconnect logic | ✅ Production-ready |
| Protective-order engine (durable per-leg ledger, fail-closed LIVE arm, cancel-gated reconcile, no fabricated broker refs) | ✅ Production-ready (pending broker-sandbox validation of real SL/TP semantics) |

---

## 10. VERDICT

**Overall Readiness: AMBER — Partially Ready**

- ✅ **Broker integration:** 4 real adapters with order placement, status query, positions, margins, holdings. OAuth lifecycle, encrypted credentials, daily renewal.
- ⚠️ **Market data:** Crypto (CoinGecko REST) is real but delayed. Indian equity is synthetic in all modes. No WebSocket streaming.
- ✅ **Order lifecycle:** Durable claims, CAS finalization, idempotency, reconciliation, postback verification.
- ❌ **Live equity/P&L:** Not derived from broker truth. Internal calculation only.
- ⚠️ **Exchange-level protection:** Engine + durable ledger + fail-closed LIVE arm SHIPPED (15C); pending broker-sandbox validation of real SL/TP semantics and Client/ rendering of protection truth.
- ✅ **Safety:** 3-layer BROKER_MODE guard, encrypted credentials, tenant isolation, fail-closed design.

**The system is ready for BROKER SANDBOX testing (Binance testnet, broker paper-trading modes). Phase 15B (honest, fail-closed broker/equity display) and Phase 15C backend (exchange-level protective orders: durable ledger, cancel-gated reconcile, fail-closed LIVE entry) are complete and validated (786 passed / 0 failed full regression). It is NOT yet ready for real-money production — remaining follow-ups: periodic live position sync from broker, real-time WebSocket market data, independent broker-sandbox validation of real SL/TP semantics, and Client/ rendering of the new protection fields.**

---

## APPENDIX A — EVIDENCE TRACE

| Claim | Evidence |
|---|---|
| No WebSocket market data provider | `app/market_data/providers/crypto.py` docstring: "Binance WebSocket + REST implementation was removed because Binance blocks Render infrastructure with HTTP 451" |
| Indian equity live = yfinance anchor + synthetic ticks | `app/market_data/providers/indian_equity.py` `_run_real_price_sync()` (yfinance 15s) + `_run_feed_loop()` (random gauss micro-ticks) |
| Crypto live = CoinGecko REST 60s | `app/market_data/providers/crypto.py` `_POLL_INTERVAL = 60.0`, no WebSocket |
| BROKER_MODE 3-layer guard | `app/brokers/__init__.py`: `assert_live_dispatch_allowed`, `assert_live_broker_connect_allowed`; adapter + network layers documented |
| Daily broker renewal cron | `app/engine/broker_cron.py`: 8:45 AM IST scheduler + TOTP renewal |
| 3-window reconciliation | `app/engine/order_reconciliation.py` docstring (Window-B, Window-C) |
| Postback signature verification | `app/brokers/postback.py` `verify_broker_postback_signature` (V3 HMAC) |
| AES-256 credential encryption | `app/models/broker_account.py` `encrypt_secret` / `decrypt_secret` |
| Idempotent durable claims | `app/api/trades.py` (client_order_id PENDING claim) + `app/engine/copy_trading.py` `_claim_copy_follower_order` |
| No exchange-level SL/TP | `app/engine/order_manager.py` — SL/TP evaluated on ticks in-process, not placed as broker orders |
| ADS LiveOptIn gate | `client/src/pages/Dashboard.jsx` `handleModeSwitch("LIVE")` checks `connectedBrokers.length === 0` → fail-closed |
| Protective-order ledger + lifecycle | `app/models/protective_order.py`, `app/engine/protective_orders.py` (`ProtectiveOrderManager` states + `reconcile_once`) |
| Fail-closed LIVE entry (503) + no fabricated broker refs | `app/api/trades.py` (arm failure → tear-down + 503), `app/brokers/upstox.py` (raise when an accepted order has no `order_id`) |
| Open-positions protection fields | `GET /api/v1/orders/open-positions` response: `stop_loss_price`, `take_profit_price`, `protection_state`, `protection_error`, `protected_at` |