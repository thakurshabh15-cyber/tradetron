# PERFORMANCE HARDENING REPORT
## TradeThrone — Phase 15D: Minimal API Calls · Storage · CPU · WS Traffic · Honest Data

**Date:** 2026-09-12 · **Branch:** `main` @ `61b960ed` + Phase 15D hardening
**Scope:** Indian-equity market-data honesty (Phase 1) + Dashboard API-call dedupe (Phase 2)
**Regression:** `pytest tests/ -q` → **786 passed, 0 failed** (3 benign warnings)

---

## 1. API CALL REDUCTION

### 1.1 Dashboard `GET /api/market-data` — 3 identical calls → 1

| Before | After |
|---|---|
| `useApi("/api/market-data")` line 176 (fetch #1 on Dashboard mount) | **removed** — consumes shared store map |
| `useApi("/api/market-data")` line 184 (fetch #2 on Dashboard mount) | **removed** |
| `useMarketStore.fetchInitialSnapshot()` (fetch #3, MarketProvider mount) | **kept — sole owner** |

Net effect: **3 → 1 REST GET per app session** (and the surviving call is app-scoped,
not per Dashboard mount, so it never duplicates across page navigations).

**Dedupe mechanics** (`client/src/stores/useMarketStore.js`):
- `snapshotInFlight` module guard → a second concurrent caller awaits the same request
  (covers React 19 StrictMode double-effect dev mounts).
- `snapshotReady` → the REST snapshot is fetched once per session; navigating away and
  back to `/` issues **zero** extra calls (WebSocket ticks keep the map live).
- `force=true` (`Refresh All`, error retries) re-fetches on explicit user intent only.
- Honest loading/error state (`snapshotLoading` / `snapshotError`) exposed through
  `MarketContext`, so skeletons and fail-closed `ErrorState` still render exactly as
  before — the UI already had the shared store bits (`liveMarketMap`) and now simply
  sources from them instead of firing a third REST copy.

### 1.2 Residual market-data call sites (documented, in scope for Phase 2b)

| Page / Component | Call | Frequency | Verdict |
|---|---|---|---|
| Watchlist | `useApi("/api/market-data")` (authFetch) | 1 per mount + on refresh button | residual candidate to migrate to the shared store snapshot |
| Markets / CommandPalette | `/api/market-data/instruments/search` | only on debounced user query | correct (lazy, bounded) |
| Dashboard instrument search | `/api/market-data/instruments/search` | only on debounced query | correct |
| Dashboard `POST /api/market-data/subscribe` | on symbol pick | user intent | correct |

---

## 2. STORAGE

- Market data is **in-memory only** (`UnifiedMarketDataManager._quotes`,
  `provider._quotes`) — no new DB tables, no persistence writes on the hot path.
- Delayed pipeline writes **one** `NormalizedTick` per symbol per refresh (15 s default)
  into `provider._quotes`; the manager snapshot serializes the same in-memory objects —
  **no duplicate storage, no per-tick object churn beyond the refresh cadence**.
- No Redis/queue involvement on the market-data read path.

## 3. CPU

| Change | Effect |
|---|---|
| yfinance pull runs inside `asyncio.to_thread(_sync)` | network + pandas parsing off the event loop |
| **No synthetic interpolation** between delayed refreshes | O(1) per refresh per symbol — no gauss/spread math between ticks |
| `_run_real_price_sync` catches/cancels cleanly | no runaway task loops |
| Provider classifier / admin status compute on demand | cheap, no per-tick cost |
| Dashboard dedupe | removes 2 unused parallel REST payloads + their JSON.parse/merge work per Dashboard mount |

Delayed cadence is `15 s` per refresh (`_DEFAULT_DELAYED_REFRESH_SECONDS`), reading the
day's 1-min bars and taking the last close — at most one vendor HTTP call per refresh
window for all subscribed symbols (single `yf.download(tickers, ...)` batch call).

## 4. WEBSOCKET / STREAM TRAFFIC

- Dashboard WS traffic unchanged (the central `/ws/market/stream` feed already was
  single-session via `MarketProvider`); the dedupe only removes redundant REST.
- **Honest emission policy** (Indian equity):
  - demo → simulated ticks labelled `feed_state=DEMO`
  - delayed → **one genuine tick per 15 s refresh**, labelled `feed_state=DELAYED` —
    the broker/simplified stream never pumps extra fabricated micro-ticks
  - live (no verified stream) → **zero ticks** (`UNAVAILABLE`)
- Crypto remains CoinGecko REST-polled at 60 s (rate-limit aware, 5-min candle cache +
  2 s min-gap guard) — unchanged.

## 5. HONEST DATA (Phase 1 summary)

| Surface | Behaviour | Proof |
|---|---|---|
| `NormalizedTick.feed_state` | `DEMO/DELAYED/STALE/UNAVAILABLE`, never LIVE without a genuine stream | `tests/test_indian_equity_honesty.py` (7/7) |
| `IndianEquityMarketDataProvider.start()` | demo→simulated loop; delayed→`_run_real_price_sync` only; live→no task, `last_sync_error` set | same suite |
| `_with_freshness` | DELAYED stays DELAYED inside 5-min window, becomes STALE after; UNAVAILABLE propagates; DEMO never claims freshness | `test_with_freshness_delayed_vs_unavailable` + `test_market_freshness.py` |
| `get_providers_status` / `/api/market-data/providers/status` | per-provider `feed_state` from real classifier — no config-derived LIVE banner | probed live: Forex `None`/STOPPED, Crypto `UNAVAILABLE`, IndianEquity `DEMO` |
| `/api/admin/system/health` | reads `unified_market_manager.get_providers_status()` | `app/api/admin.py` |

## 6. VERDICT

- **API calls:** ✅ Dashboard market-data REST cut **3 → 1 per app session**.
- **CPU/storage:** ✅ single in-memory quote cache; to_thread'd vendor pulls; no per-tick
  synthesis in delayed mode.
- **Honesty:** ✅ live certification gate **RED by design** until a genuine broker stream
  connects (`ANGEL_JWT_TOKEN`); delayed is **AMBER** (real prices, delayed label).