# Phase 5 — Real Data Provenance, Staleness Detection & Frontend Transparency

Phase 5 makes the platform *honest* about where market data comes from: it wires
real feed metadata through to the UI, detects when a **real** feed has gone stale,
and never lets synthetic demo data masquerade as live quotes.

## 1. Freshness / staleness model (backend)

- **`app/config.py`** — added per-asset-class freshness windows:
  `data_freshness_crypto=30s`, `data_freshness_equity=15s`,
  `data_freshness_forex=60s`, `data_freshness_commodity=60s`,
  `data_freshness_default=30s`.
- **`app/market_data/base.py`** — `NormalizedTick` gained:
  - `age_seconds(now=None)` — age of the tick since its UTC `timestamp`.
  - `is_stale(max_age_seconds, now=None)` — fail-closed: an unparsable/absent
    timestamp is treated as **stale**, never as live.
- **`app/market_data/unified_manager.py`**:
  - `_with_freshness(q)` attaches honest metadata to every serialised quote:
    - `data_status` ∈ {`LIVE`, `DEMO`, `STALE`, `UNKNOWN`}
    - `is_stale` (`True`/`False`, or `None` for demo feeds)
    - `age_seconds`
  - **DEMO feeds are never flagged STALE** — they report `data_status="DEMO"`
    and `is_stale=None` (no real freshness guarantee to violate).
  - **Real feeds fail closed** — a real tick older than its asset-class window,
    or with an unparsable timestamp, is reported `STALE`.
  - `get_quote`, `get_snapshot`, and both WebSocket broadcast channels
    (`market:SYMBOL`, `market:stream`) now carry the metadata.
  - `get_providers_status` gained `stale_symbols_count` per provider.

## 2. Frontend transparency

- **`client/src/components/MarketTicker.jsx`** — new `DataStatusBadge` renders a
  **REAL/DEMO/STALE** pill next to the data-source label on every ticker card
  (shared by Markets and Dashboard), with tooltips explaining each state.
- **`client/src/pages/MarketDetail.jsx`** — new `ProvenanceBadge`
  (**REAL FEED / SIMULATED / STALE**) on the selected-symbol header.

## 3. Verified

- Backend: `pytest -q` → **194 passed** (baseline was 187; +7 new tests in
  `tests/test_market_freshness.py`).
- New tests cover: tick age/freshness, unparsable-timestamp fail-closed,
  demo-never-stale, live-when-fresh, stale-when-old, quote-endpoint metadata,
  and snapshot metadata honesty.
- Frontend: `npm run build` → **built successfully** with the badge changes.

## 4. Real-data integration notes

The crypto provider uses a **credential-free** CoinGecko public REST API
implementation. Enable the
real feed by setting `feed_mode_crypto=live`; real ticks are then reported
`data_status="LIVE"`/`"STALE"` while the default `demo` mode reports
`data_status="DEMO"` — the UI reflects whichever is actually in use.

> NOTE: The previous Binance public WebSocket + REST klines implementation was
> removed because Binance blocks Render infrastructure with HTTP 451. CoinGecko
> has no such restrictions.

> Deployment blocker remains: the backend at `tradethrone.onrender.com` is
> unreachable (404). Phase 5 backend logic is fully testable locally; frontend
> badges render the honest metadata whenever the app reaches a live backend.
