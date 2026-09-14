# Phase 1 Step 9 — Full-Stack Audit: Frontend↔Backend Contract, Feature, and Safety

**Session artifacts live in this folder (untracked by git, like prior SESSION reports).**

| Field | Value |
|---|---|
| Branch | `feat/autonomous-os` |
| Head | `e77eb763` (`feat(autonomous): event-driven agent console sync and efficiency`) |
| Date | 2026-09-13/14 |
| Scope | Full FE↔BE contract, control-by-control feature audit, trading-pipeline trace, auth/IDOR sweep, P0/P1/P2 defect disposition, release-readiness verdict |
| Method | Static route/ownership review of all 25 API routers + main.py + websocket.py; executable contract regression (new); full backend pytest; vitest; eslint; vite build |

---

## 1. Executive summary & verdict

**Verdict: RELEASE-READY for the automated candidate on the current code state (no blocking defects).**

- **0 P0** and **0 P1** defects found across the full-stack contract, feature, and safety surface.
- **1 P2 defect found & fixed** (`copy-trading` resume silently discarded a LIVE follower's mode + broker linkage) with 2 new regression tests.
- **5 new Step-9 regression tests** added; full backend suite re-run to **892 passed** after the fix (baseline was **887 passed**).
- Frontend gate (vitest **71 passed** / eslint **0 errors** / build **OK**) and backend gate both green.
- **Release-hardening pass (§11, 2026-09-14):** +7 regression tests → full suite **899 passed / 0 failed** (886.76s); **Postgres parity gate green** (32/32 tables exact — no ORM↔migration drift); Alembic drift check green; CI secret scan clean (453 tracked files); vitest 71/71; eslint **0 errors / 19 warnings** (S9-D3 resolved); Vite build OK.
- The only unresolved items are the pre-existing **operator/infra actions** carried from prior reports (Render backend restart, credential rotation, Redis provisioning) — none are code defects.

## 2. Baseline evidence (all on head `e77eb763`, before Step-9 edits)

| Gate | Command | Result |
|---|---|---|
| Backend tests | `pytest -q --tb=no` (project venv `.venv`) | **887 passed, 3 warnings in 855 s** |
| Frontend tests | `npm test` (vitest) | **71 passed / 9 files** |
| Frontend lint | `npm run lint` (eslint) | **0 errors, 26 warnings** (react-hooks style, non-blocking) |
| Frontend build | `npm run build` (vite+rolldown) | **✓ built in 5.29 s** |

Notes:
- The shared root `.venv` lacks `pandas` and fails collection of `tests/test_indian_equity_honesty.py`; the *project* venv `fastapi-template\.venv` is the correct interpreter.
- Lint warnings are `react-hooks/set-state-in-effect` informationals; they do not gate the release.

## 3. Phase B — backend route inventory (snapshot)

25 API routers mounted in `app/main.py` (lines 344–371) plus `main.py` health/observability routes. Reconciled inventory:
## 4. Phase C — Frontend↔Backend contract (0 mismatches)

Extraction: all `.jsx`/`.js` under `client/src` (excluding `*.test.*`), 3 patterns — literal `'/api/…'`, template splices with `${…}`, and `${API_BASE}/api/…` absolute templates.

**Normalization rules** (documented in the new test): strip query strings; flatten `${expr}` → `{param}`; drop incomplete trailing splices; special-case the closed-enum splice `/api/agent/config/{action}` (start|pause|resume|stop — each concrete route asserted); ignore 1 prose-comment artifact (`/api/agent`).

| Metric | Value |
|---|---|
| Unique FE endpoint references | **145** |
| Unresolved FE references | **0** |
| Phantom backend routes referenced by FE | **0** |
| Legacy alias contract | `/api/trades/api/positions` = intentional alias of `/api/trades/positions` (verified; frontend uses canonical) |

Every FE reference resolves to a live route, including the newest surfaces:
- **AgentConsole** → `/api/agent/console`, `/api/agent/config` (POST/PATCH), `/api/agent/config/{start,pause,resume,stop}`, `/api/agent/tasks/{id}/approve`, `/ws/trades`
- **Marketplace** → `/api/strategies/marketplace`
- **Admin** → 13 `/api/admin/*` operations
- **OrderTerminal/FastOrder** → `/api/v1/orders/execute-dma`, `/api/trades/order`
- **TradingChart** → `/api/market/candles`; **CommandPalette** → `/api/market-data/instruments/search`
- **UseWebSocket** hooks → `/ws/trades`, `/ws/events` (tokenized) + public market feeds

## 5. Phase D — control-by-control audit (representative stakes)

| Surface / control | Endpoint(s) | AuthN/AuthZ verdict |
|---|---|---|
| Order placement (manual/DMA) | `POST /api/trades/order`, `/place`, `/api/v1/orders/execute-dma` | Bearer-required; user-scoped; per-user rate cap; idempotency claim/replay; LIVE gated by `BROKER_MODE=live` + owner-connected broker resolution |
| Close position | `POST /api/trades/positions/{id}/close` | Owner or ADMIN/SUPERADMIN; **CAS OPEN→CLOSED** (single-winner); fail-closed 503 if LIVE broker lost; PnL credited to **position owner**; protective-order teardown; exactly-once autopilot feed |
| Risk-target modify | `PATCH /api/v1/orders/positions/{id}/risk-targets` | User-scoped SELECT (404 for foreign/closed) |
| Strategy deploy/pause | `POST /api/strategies/{id}/deploy`, `/{id}/pause` | Owner or admin; broker task user-scoped; token-expiry preflight |
| Platform kill-switch | `POST /api/strategies/kill-switch`, `/api/admin/kill-switch/*` | `get_current_admin_user` (strict RBAC) |
| Broker unlink/renew/sync | `DELETE/accounts/{id}`, `POST {id}/renew`, `POST {id}/sync`, `POST /renew-all` | Owner or admin; renew-all self-scoped unless admin; 409 on unlink with OPEN LIVE positions (prior P1 fix) |
| Copy-trading join/leave/update | `/api/copy-trading/{groups,following,join}` | Owner-scoped rows; broker-ownership validation server-side (403 cross-tenant); feature-gated; **Step-9 fix** (below) |
| Visual strategies CRUD | `/api/visual-strategies/…` | User-scoped SELECT (404) |
| Dashboard tasks / summary | `/api/dashboard/summary`, `/complete-task` | Whitelisted task set; per-user completion store; honesty (no fabricated strategies) |
| Subscriptions / billing | `/api/subscriptions/*`, `/api/billing/*` | Bearer; Razorpay sandbox forced in tests; idempotent payment paths |
| Watchlist + alerts | `/api/watchlist*` | Namespace-scoped (`user.id` or NULL demo namespace) |
| Agent control plane | `/api/agent*` | Tenant-scoped service; CAS lifecycle; owner-only approval; LIVE-mode guard; user-id never client-supplied |
| WS feeds | `/ws/trades`, `/ws/events` | JWT token auth; reject 4001/4003/4408; per-user cap 4408; tenant-scoped broadcasts |
| Admin console | `/api/admin/*` (15 endpoints) | `get_current_admin_user` on all mutations; KYC queue/review; user status/role; audit logs |

No IDOR, missing-auth, or cross-tenant write path found.

| Method | Count |
|---|---|
| GET | 68 |
| POST | 78 |
| PATCH | 6 |
| DELETE | 6 |
| PUT | 3 |
| **HTTP total** | **161** |
| WebSocket | 9 (`/ws/market/stream`, `/ws/market/{symbol}`, `/ws/trades`, `/ws/events`, `/ws/optionchain/{symbol}`, legacy aliases `/ws/stream`, `/ws/dashboard`, `/ws/ticks`, `/ws/market-data`) |
| **Grand total** | **170** |

(Note: FastAPI on this branch mounts routers as lazy `_IncludedRouter` — the inventory walker must descend `original_router.routes`. The new regression test does this.)
## 6. Phase E — trading pipeline trace (contract sig + WS leg)

`feed → strategy → signal → agent → gates → order → broker → fill → position → protective → reconcile → WS → UI`

1. **Market data**: `app/market_data/unified_manager.py` — enriched ticks → `market:{symbol}` + `market:stream`.
2. **Strategy evaluation**: `app/engine/trading_engine.py` tick loop + custom strategies; reload via `engine.reload_strategies()`.
3. **Signal → durable keys**: `OrderRecord` unique partial `(user_id, client_order_id)` + `(signal_key)`; PENDING claim committed **before** dispatch.
4. **Agent loop**: `app/engine/agent_control.py` + `agent_runtime.py` — bounded evaluation loop, fresh-feed gate, approval ledger, CAS lifecycle.
5. **Gates**: `assert_live_dispatch_allowed()` (all LIVE paths), risk-manager order-rate + daily-drawdown kill switch, subscription feature access, stale-broker preflight.
6. **Broker dispatch**: adapter (`upstox` LIVE / `simulated` PAPER) `place_order`; broker ref persisted immediately post-acceptance (crash-window hardening).
7. **Fill**: order → FILLED; position OPEN with SL/TP; paper accounting credited atomically.
8. **Protective orders**: `app/engine/protective_orders.py` — fail-closed arming; teardown on close; unique `(position_id, leg)`.
9. **Reconciliation**: postback normalization (status mapping, partial fills), periodic sweep; reconciliation never re-dispatches.
10. **WS**: `ws_manager.broadcast_user("trades", user_id, payload)` for every order/trade event (tenant-scoped; `None` owner dropped); `broadcast_admins` for KILL_SWITCH/ADMIN halts; `broadcast("position:{id}")` for risk-target updates.
11. **UI**: Dashboard/Execution/AgentConsole/TradeLog subscribe `/ws/trades`; events refetch authoritative snapshots (debounced) — events never become UI truth.

Broadcast assertions in new/updated tests align with the `broadcast_user("trades", …)` pattern used by `trading_engine.py`, `agent_control.py`, `admin.py`, `strategies.py`.

## 7. Defect disposition (Step 9)

| ID | Sev | Item | Action | Status |
|---|---|---|---|---|
| S9-D1 | **P2** | `POST /api/copy-trading/join` resume of a STOPPED subscription used `req.mode` directly (`req.mode == "LIVE"` for broker attach). Because `JoinGroupRequest.mode` defaulted to `"PAPER"`, an omitted mode flipped a previously-LIVE follower row to PAPER and silently detached its broker account. | (a) schema: `mode` → `Optional[Literal[PAPER,LIVE]] = None`; (b) resume resolves `(req.mode or existing.mode)` and preserves linkage; swap-away-from-LIVE detaches; (c) fresh join defaults to PAPER at row creation. | **FIXED** + 2 regression tests |
| S9-D2 | P2 | `/api/trades/positions` uses `Optional[UserRecord] = Depends(get_current_user)` — the dependency raises 401 for anonymous, so the optional annotation is misleading. Harmless but worth documenting. | Pinned by test: anonymous → 401; no silent unscoped listing. | Documented |
| S9-D3 | P2 | Frontend `react-hooks/set-state-in-effect` iterator warnings (26) — informational. | 7 warnings cleared in 6 files (CommandPalette, FastOrderPanel, OptionChain, useApi, Markets, Portfolio); 19 informational remain, tracked in future UI cleanup | **FIXED** (partial cleanup, accepted) |
| S9-D4 | P1 (ops) | Render backend unavailability + unpushed remote commits + credential rotation — pre-existing operator items (see §10). | Operator. | Open |

**No P0/P1 code defect was confirmed in this pass.**

## 8. New Step-9 + release-hardening regression tests

| File | Tests | Proves |
|---|---|---|
| `tests/test_step9_frontend_backend_contract.py` | 3 | Every FE endpoint reference in `client/src` resolves to a registered backend route (descending lazy `_IncludedRouter`); legacy position alias wired; anonymous `/api/trades/positions` rejected (fail-closed) |
| `tests/test_step9_copy_resume_keeps_live_linkage.py` | 2 | Resume-without-mode preserves `LIVE` + broker linkage; explicit downgrade to PAPER detaches |
| `tests/test_paper_book_bounded_history.py` | 2 | Phase-E bound semantics: parseable values ≥ 2 kept verbatim up to the hard ceiling; `0`/negative/`1` → safety floor; `None`/empty → documented default |
| `tests/test_subscription_symbol_cap.py` | 3 | Subscription cap: dynamic `subscribe()` beyond `max_subscribed_symbols` raises `SubscriptionLimitError` → HTTP 429 (fail-closed); quote cache pruned on **every** successful subscribe |
| `tests/test_revoked_token_retention.py` | 2 | Best-effort expired-revoked-token pruning: never breaks logout/refresh; revocation rows with expired JWT `exp` deleted, live revocation markers retained |

Suite delta: **887 → 892 → 899 passed** (D1 fix + 5 tests; release-hardening pass added 7 more tests).

## 9. Release-readiness matrix

| Dimension | Status |
|---|---|
| Backend contract (161 HTTP + 9 WS) | 🟢 899/899 tests green |
| FE↔BE endpoint surface | 🟢 0 mismatches (executable regression) |
| AuthN / RBAC / IDOR | 🟢 Verified owner+admin boundaries on every sensitive mutator |
| LIVE-vs-PAPER separation | 🟢 `BROKER_MODE=live` guard on manual, DMA, close, agent, copy-trading LIVE paths |
| Financial correctness (CAS, idempotency, protective teardown, reconciliation) | 🟢 Verified + existing red/green tests |
| WS tenant isolation | 🟢 tokenized private feeds, owner-scoped broadcasts, admin-only global events |
| Frontend tests / lint / build | 🟢 71 passed / 0 errors / 19 informational warnings / build ok |
| DB parity (SQLite↔Postgres) | 🟢 Parity gate 32/32 tables exact; Alembic drift check green; `AgentDecision.user_id` index aligned with migration `0011_agent_control` |
| Live deployment + credentials (operator) | 🔴 Operator action required (pre-existing) |

## 10. Operator-only follow-ups (carried, not code)

1. Restart/redeploy the Render backend (`tradetron-8jkz.onrender.com`) and verify `/api/health` → 200, `/readyz` → cache=true.
2. Push `feat/autonomous-os` to origin — **executed 2026-09-14** after all gates went green (§11); remote HEAD verified.
3. Rotate any historical credentials and provision `UPSTASH_REDIS_URL` on Render per production config matrix.

---

## 11. Release hardening pass — 2026-09-14 (Phase 1 Step 9 close-out)

Post-audit remediation run: every regression surfaced by the first full-suite
re-run of the Step-9 code state was fixed and re-verified, and the CI/deploy
configuration was realigned with the local Postgres parity matrix.

### 11.1 Gate results (all green, recorded from live runs)

| Gate | Command / Scope | Result |
|---|---|---|
| Backend pytest (full suite) | `pytest -q` (`fastapi-template\.venv`) | **899 passed / 0 failed** — 886.76 s, 3 warnings |
| Postgres parity | `python scripts/ci_postgres_check.py` against `docker compose` Postgres (port 5432) | **✔ 32/32 tables exact** — no ORM↔migration drift |
| Alembic drift | `python scripts/ci_alembic_check.py` | ✔ single head, no pending ops |
| CI secret scan | `python scripts/ci_secret_scan.py` | ✔ clean — 453 tracked files, 0 findings |
| Frontend vitest | `npm test` (client) | ✔ 71 / 71 passed |
| Frontend ESLint | `npm run lint` (client) | ✔ 0 errors — 19 informational warnings |
| Frontend build | `npm run build` (client, Vite) | ✔ build OK |

### 11.2 Fixes shipped in this pass

1. **Phase-E fail-closed bounds (`order_manager.py`, `unified_manager.py`).** The
   prior `or default` pattern was unsafe because `0` is falsy and must mean
   “safety floor,” not “default.” New semantics: parseable values ≥ 2 are
   respected verbatim up to the hard ceiling; `0` / negative / `1` resolve to
   the safety floor (**100** paper-book / **200** subscriptions); `None`/empty
   fall back to the documented defaults. Comments in `app/config.py` and
   `.env.example` document the semantics.
2. **Quote-cache prune on every subscribe (`unified_manager.py`).** Pruning now
   runs on every successful `subscribe()`, not only when the subscribed
   universe *grew* — the full-suite ordering exposed that a prior subscriber
   made `new_set` empty, so stale quotes were never pruned.
3. **Subscription ceiling → HTTP 429 (`app/main.py`).** Dynamic `subscribe()`
   beyond `max_subscribed_symbols` raises `SubscriptionLimitError`, mapped to
   **429** with an actionable message (fail-closed, no partial state).
4. **ORM↔migration drift (`app/models/agent_control.py`).** `AgentDecision.user_id`
   now declares `index=True` to match migration `0011_agent_control`
   (`ix_agent_decisions_user_id`) — found by the Postgres parity gate, which the
   SQLite-only checks cannot catch.
5. **Revoked-token table hygiene (`auth.py`, `admin.py`, `app/db/maintenance.py`).**
   New best-effort `prune_expired_revoked_tokens()` bounds revocation-table growth;
   callers swallow failures so logout/rotation/reset never break (2 regression tests).
6. **CI parity port alignment (`scripts/ci_postgres_check.py`).** Default port
   `5434 → 5432` to match the `docker compose` mapping used in CI.
7. **Frontend lint cleanup (S9-D3).** 7 `react-hooks/set-state-in-effect`
   warnings cleared across 6 files; 19 informational warnings remain (tracked).

### 11.3 Commit boundaries & push

Committed on `feat/autonomous-os` as focused commits:
1. `fix(db): bound revoked-token table via best-effort expired-row pruning`
2. `fix(phase-e): fail-closed subscription/book bounds, prune on every subscribe, 429 limit errors`
3. `test(phase-e): regression tests for bounded paper-book history and subscription cap`
4. `fix(db): align AgentDecision.user_id index with migration 0011 (Postgres parity)`
5. `chore(ci): use compose-mapped Postgres port 5432 in parity check`
6. `refactor(client): clear react-hooks set-state-in-effect warnings (7)`
7. `docs(audit): record Phase 1 Step 9 release-hardening gate results`

Pushed to `origin/feat/autonomous-os`; remote HEAD verified. No destructive git
operations were used. The Postgres container used for the parity gate was
stopped after the gate completed.