# API CALL AUDIT
## TradeThrone — Market-Data Call Inventory (Phase 15D)

**Date:** 2026-09-12 · **Method:** static inventory of every `GET/POST /api/market-data*`
client call + one live TestClient probe of the status endpoint.

---

## 1. Client call sites

| # | File | Call | Trigger | Calls/mount (before) | Calls/mount (after) |
|---|---|---|---|---|---|
| 1 | `client/src/pages/Dashboard.jsx` | `GET /api/market-data` (`useApi`, public) | Dashboard mount | 1 | **0** (removed) |
| 2 | `client/src/pages/Dashboard.jsx` | `GET /api/market-data` (`useApi`, public, widgets) | Dashboard mount | 1 | **0** (removed) |
| 3 | `client/src/context/MarketContext.jsx` → `useMarketStore.fetchInitialSnapshot` | `GET /api/market-data` | app mount (once) | 1 | **1** (sole owner, deduped) |
| 4 | `client/src/pages/Dashboard.jsx` | `GET /api/market-data/instruments/search?q=…&limit=15` | debounced user query | lazy | lazy (unchanged) |
| 5 | `client/src/pages/Markets.jsx` | `GET /api/market-data/instruments/search?q=…&limit=8` | debounced user query | lazy | lazy (unchanged) |
| 6 | `client/src/components/CommandPalette.jsx` | `GET /api/market-data/instruments/search?q=…&limit=6` | user query | lazy | lazy (unchanged) |
| 7 | `client/src/pages/Dashboard.jsx` | `POST /api/market-data/subscribe` | user picks a symbol | on intent | on intent (unchanged) |
| 8 | `client/src/pages/Watchlist.jsx` | `GET /api/market-data` (`useApi`, authFetch) | Watchlist mount + refresh | 1/mount | 1/mount (**residual**, Phase 2b candidate) |

**Result:** the Dashboard's 3 identical `GET /api/market-data` on mount collapsed to the
single app-level store snapshot (#3). Maximum market-data REST volume on a Dashboard
mount: **1** request + lazy search/subscribe.

## 2. Duplicate-prevention guarantees (`useMarketStore`)

| Guard | Effect |
|---|---|
| `snapshotInFlight` (module-level promise) | concurrent callers share one in-flight request — StrictMode double-effect safe |
| `snapshotReady` | no repeat snapshot fetch for the rest of the session (WS keeps it live) |
| `force` param | only explicit user refresh / retry re-fetches |

## 3. Backend call surfaces (server-side, per request)

| Endpoint | Backend work | Notes |
|---|---|---|
| `GET /api/market-data` | reads in-memory `unified_market_manager` quote cache, builds `market` list, enriches with `_with_freshness` | no DB, no vendor REST call |
| `GET /api/market-data/providers/status` | `get_providers_status()` — reads provider classifiers | no DB, no vendor REST call |
| `GET /api/market-data/quote/{symbol}` | in-memory lookups | no DB, no vendor REST call |
| `GET /api/market-data/instruments/search` | instrument-master search (in-memory master) | bounded `limit`, lazy by design |
| delayed provider loop (background) | single batched `yf.download(tickers, period="1d", interval="1m")` per 15 s refresh via `to_thread` | 1 HTTP / refresh window; **0 between refreshes** |
| crypto provider loop (background) | CoinGecko single poll / 60 s (rate-limit aware) | unchanged |

## 4. Honesty guarantees on the wire

- Every `NormalizedTick` exposed via API/WS carries `feed_state` + `feed_mode` +
  `data_source`; the client renders provenance (REAL/DEMO/DELAYED/STALE) rather than
  assuming LIVE.
- With `feed_mode_equity=delayed`, ticker/live widgets show delayed prices with the
  DELAYED label; with `=live` and no verified stream, quotes are **UNAVAILABLE** and the
  UI shows the fail-closed state (no fabricated price, no false LIVE banner).
- `/api/market-data/providers/status` (probed live): IndianEquity `DEMO` / Crypto
  `UNAVAILABLE` / Forex `None` — **no fake LIVE claims**.

## 5. Follow-ups (Phase 2b)

- Migrate `Watchlist.jsx` `useApi("/api/market-data")` to the shared store snapshot
  (`snapshotRefetch(true)` on the refresh button) to finish the per-page dedupe.
- Instrument-search calls are already lazy/bounded — keep as-is.