# DESIGN GATE — Durable PENDING-before-dispatch for Strategy Engine LIVE Entry

**Status:** DESIGN GATE — awaiting approval (no implementation until internally consistent)
**Scope:** `app/engine/trading_engine.py::_execute_signal` LIVE branch
**Baseline:** `131ca09c` (verified untouched; only `tests/test_strategy_live_entry_orphan_repro.py` added, untracked)
**RED repro:** `tests/test_strategy_live_entry_orphan_repro.py` (fails at invariant — proven)

---

## 1. Problem statement (P1)

`_execute_signal` LIVE path dispatches `target_broker.place_order()` **first**, then calls
`_persist_trade` which writes Order/Trade/Position rows **FILLED in a single commit afterwards**.
There is **no** durable `PENDING` claim and no `client_order_id` before dispatch.

A process crash in the window **[broker acceptance → finalize commit]** leaves a REAL live
position open on the exchange with **zero** local DB record. Both recovery mechanisms fail to
rescue it:

- `BrokerOrderReconciliationEngine` scans only `client_order_id IS NOT NULL AND status='PENDING'
  AND broker_order_id IS NOT NULL` — a strategy order has neither and never existed as PENDING.
- `reconcile_broker_postback` looks up `OrderRecord.broker_order_id` — no row → `order_not_found`.

This is the **same defect class already fixed** for the manual/DMA paths (LIVE DMA commits a PENDING
claim with `client_order_id` before dispatch); the strategy engine path was missed.

---

## 2. Target state machine / lifecycle

For LIVE strategy entries only (PAPER mode is left unchanged — no broker exposure, no reconciliation
need, and `_persist_trade` already writes FILLED immediately for it).

New lifecycle for LIVE:

```
                 +----------------------------------------------------------+
                 | 1. Claim (durable, BEFORE dispatch)                      |
                 |    INSERT OrderRecord(status='PENDING', client_order_id= |
                 |    strategy_idempotency_key, ...) -> COMMIT              |
                 +-----------------------------+----------------------------+
                                                |
                              (crash here = broker never contacted -> case B
                               -> reconciliation can auto-cancel after stale
                               timeout, resolving a no-side-effect claim)
                                                v
                 +----------------------------------------------------------+
                 | 2. Dispatch: target_broker.place_order()                 |
                 +-----------------------------+----------------------------+
                                                |
          +-----------------------+-------------+-----------------+
          v (exception -> reject)                                 v (accepted)
 +-----------------------------+                    +---------------------------------+
 | 2a. Persist REJECTED         |                    | 3. Persist broker ref (durable)|
 |     status='REJECTED',       |                    |    broker_order_id = resp.order|
 |     error_message=...        |                    |    -> COMMIT (own transaction)  |
 |     -> COMMIT                |                    +---------------+-----------------+
 +-----------------------------+                                    |
                                                                     |  WINDOW C
                                                                     |  (crash here: broker accepted,
                                                                     |   broker_order_id NOT persisted)
                                                                     v
                                                 +---------------------------------+
                                                 | 4. Finalize (FILLED)            |
                                                 |    build Order FILLED,          |
                                                 |    create Trade + Position,     |
                                                 |    reap the PENDING claim       |
                                                 |    -> COMMIT (idempotent CAS)   |
                                                 +---------------------------------+
```

**Reap semantics:** rather than deleting the PENDING claim, the finalize step **transitions the same
row** to FILLED and attaches the Position/Trade, exactly mirroring `_finalize_filled` in
`order_reconciliation.py` (CAS on `status NOT IN FINALIZED`, `position_id IS NULL`, no linked Trade).
This preserves the audit trail and keeps the idempotency key's history on one row.

The helper `_persist_trade` is **reused** but its Order insert is replaced by the finalize of the
existing claim **only when a claim was created**; for the unchanged PAPER / builtin-SMA path the
current behavior is preserved.

---

## 3. Crash-window table (A–E)

| Window | What happens | Durable local record? | Broker side-effect? | Recovery on restart |
|--------|--------------|----------------------|---------------------|---------------------|
| **A** before claim commit | Nothing persisted, nothing dispatched | No record (correct) | None | N/A — no side effect, nothing to recover |
| **B** after claim commit, before `place_order` | PENDING row exists, broker never contacted | yes PENDING claim | None | Reconciliation (case B) auto-closes: status->CANCELLED, no position booked; the client_order_id CAS is cleared so the key is retryable |
| **C** after broker accepted, before broker ref persisted | **PENDING row, `broker_order_id=NULL`** | yes PENDING claim (partial) | **REAL open order on exchange** | **Resolved by design — see §4** |
| **D** after broker ref persisted, before finalize commit | `PENDING` + `broker_order_id` set | yes full keyed PENDING (broker ref present) | REAL open/filled order | Reconciliation (existing case) queries broker `get_order_status` -> finalize FILLED or mark OPEN/REJECTED |
| **E** after finalize commit | FILLED + Trade + Position | yes complete | Filled | N/A — fully booked |

Recovery summary: with this design, **every** window with a broker side-effect (C, D) is durable and
recoverable. Window **A** is the only "no record" case and is correct by construction. This closes
the orphan gap.

---

## 4. Window C resolution (the critical gap)

The existing reconciliation query requires `broker_order_id IS NOT NULL`. A crash in window C leaves
a PENDING claim with `broker_order_id IS NULL` **and** a real open order on the exchange. We must
resolve this — the current code cannot.

### 4a. Broker `client_order_id` echo (preferred, where supported)
Real brokers support caller-supplied order tags. The idea: pass the strategy's idempotency key as
the broker's own client/order reference so a post-dispatch broker status query can find the order by
**that key**, not by `broker_order_id`.

**Reality check (from source):** `order_req` is built as `OrderRequest` (schema) and passed to
`place_order(order_req)`. I inspected all four adapters: none forward a client-supplied tag to the
broker SDK via `OrderRequest`, and `get_order_status`/`get_order_status_with_symbol` take
`broker_order_id` only — there is **no** "query by client_order_id" capability in any adapter. Adding
one is a large cross-adapter change with per-broker SDK uncertainty. **Defer** this; do not block the
core fix on it.

### 4b. Stale-claim timeout + positions-query resolution (chosen, deterministic)
Add a reconciliation case for **keyed PENDING rows that are older than a stale threshold but carry
`broker_order_id IS NULL`** — i.e., window-B/C orphans. Two sub-paths:

1. **B (never dispatched):** `broker_order_id IS NULL` AND the claim is older than
   `STALE_PENDING_MIN_AGE_SECONDS` AND `broker_order_id` is still NULL. The broker was never
   contacted (a dispatched order would, in practice, have had its broker ref persisted in window D
   microseconds later — and even if it was dispatched but the ref write raced, see Path 2). Because
   we cannot distinguish "never dispatched" from "dispatched but ref not yet saved" purely from DB
   state, we resolve BOTH by querying a live adapter for positions.

   - Query `adapter.get_positions()` (all adapters have it). If the account holds a live position
     matching `(symbol, side, quantity)` for this strategy **and no position/trade row is linked to
     this claim**, treat as a real dispatched-but-orphaned order -> finalize FILLED from the position's
     `average_price` (same booking path as `_finalize_filled`).
   - **If no matching live position exists**, the broker was never reached -> mark the claim
     `CANCELLED` (no side effect occurred). The client_order_id CAS is then free and the strategy
     signal can re-claim if a newer signal fires.

2. **C (dispatched, open order still sitting live but position not yet booked):** the positions
   query in Path 1 catches this too — if the position is live, we finalize; if the broker accepted
   but the order is still OPEN and not reflected in `net` positions yet, we conservatively keep the
   claim PENDING (bounded retry, `unknown`) so we never fabricate a fill or a cancel on a possibly-live
   order.

This gives window C a **deterministic, broker-query-based resolution with zero fabrication**:
- Confirmed live exposure -> book it (FILLED/position).
- Confirmed no exposure -> cancel the claim cleanly.
- Uncertain -> stay PENDING (re-examine next pass), never cancel a possibly-live order.

### Why the positions-query approach is safe
`get_positions()` is a **read-only** call and is already used defensively elsewhere (`_execute_signal`
uses `get_margins()` before dispatch). It never submits/cancels. The booking path reuses the exact
CAS-protected `_finalize_filled` logic, so a concurrent postback/reconciliation cannot double-book.

---

## 5. Idempotency key derivation

The durable claim needs a **stable per-signal identity** so two evaluations of the *same* cross don't
double-execute, while two genuinely distinct signals (same dir later, or different qty) don't collide.

`SignalPayload` carries `symbol/side/quantity`; the strategy `dict` carries `id` (UUID) and `user_id`.
There is **no signal timestamp** currently in `_execute_signal`'s signature. Derivation:

```
key_raw = f"{strategy_id}:{symbol}:{side}:{quantity}:<signal_timestamp>"
client_order_id = "strat-" + sha256(key_raw)[:40]   # ASCII, <=64 chars
```

- `strategy_id`, `symbol`, `side`, `quantity` -> pins identity.
- `<signal_timestamp>` -> distinguishing two identical signals fired at different times (two separate
  golden crosses). A **coarse grain** is used so re-evaluation within the same tick/coarse-window
  reuses the same key and is replayed/cancelled rather than double-executed. Best choice: the
  existing `crossover_states` flip is the semantic "one signal". Simpler and deterministic: use
  `int(time.time())` (one-second grain) captured once at the top of `_execute_signal` after the signal
  fires. Two evaluations of the *same* triggered cross within the same second -> same key -> replay/409
  (no double). A *new* cross in a later second -> new key.

Key must fit the existing `ux_orders_user_client_order_id` partial unique index
(`client_order_id`), which is per-`user_id` and unique. Because key includes `strategy_id`/`quantity`,
and is per-user, this gives exactly the durable idempotency the manual path enjoys. `<=64` chars,
`[A-Za-z0-9._-]` — `sha256` hex prefix satisfies this.

> **Precision trade-off (documented):** using a time-based component means the same signal fired in a
> *later* second gets a *different* key. For LIVE, a missed `_persist_trade` (window E already booked)
> would be recoverable via the FILLED order + trade already existing; a retry with a new key would
> risk a **duplicate real order**. To fully prevent double-dispatch of the same cross, the strategy
> must not re-fire within the recovery window — see §7 "Strategy re-fire guard". The primary protection
> (claim-before-dispatch + reconciled finalize) is complete regardless; the time-grain only affects
> the duplicate-vs-distinct boundary, and one-second behavior is correct for all distinct crosses.

---

## 6. Reconciliation integration (exact changes)

`app/engine/order_reconciliation.py`:

1. **Relax the scan** so window-B/C claims are eligible. Current predicate requires
   `broker_order_id IS NOT NULL`. New predicate: scan keyed LIVE PENDING rows older than the stale
   cutoff **regardless of `broker_order_id`**. Inside `_reconcile_order`, branch:
   - `broker_order_id` present -> existing path (query by id) — unchanged.
   - `broker_order_id` NULL -> NEW window-B/C resolution (positions-query, §4) -> finalize
     FILLED / cancel / keep-PENDING.
2. Add a small helper, e.g. `_reconcile_unkeyed_ref_claim(db, order)`, that uses
   `adapter.get_positions()` and returns `("filled" | "cancelled" | "unknown", detail)`.

**No changes** needed to the FILLED/terminal CAS finalizers — they are already idempotent and reused.

---

## 7. Files to change (exact)

| File | Change |
|------|--------|
| `app/engine/trading_engine.py` | In `_execute_signal` LIVE branch: (1) derive `client_order_id`; (2) claim a durable PENDING `OrderRecord` BEFORE dispatch via a new private `_claim_strategy_order` helper that reuses the `ux_orders_user_client_order_id` unique index + CAS-replay semantics; (3) persist `broker_order_id` in its own commit right after acceptance (window D hardening); (4) on dispatch exception -> persist REJECTED on the claim; (5) finalize the claim FILLED + create Trade/Position via `_persist_trade` finalize path. PAPER/builtin-SMA path unchanged. |
| `app/engine/order_reconciliation.py` | Relax scan predicate + add window-B/C `broker_order_id IS NULL` resolution (§4, §6). |
| `tests/test_strategy_live_entry_orphan_repro.py` | **Flip RED->GREEN.** Update invariant: after the simulated crash, assert a durable PENDING claim (+ broker ref) exists, and that reconciliation resolves it to a recoverable FILLED/cancelled state. Keep all diagnostic asserts. |

**No migration / no model change needed** — `client_order_id`, `broker_order_id`, `status` already exist
on `OrderRecord`. The `ux_orders_user_client_order_id` unique index is already in place.

---

## 8. Tests required

1. **RED repro flip** (`test_strategy_live_entry_orphan_repro.py`): crash after broker acceptance ->
   assert PENDING claim + broker ref exist; assert reconciliation now **scans >=1** and resolves to
   FILLED (booking a position), so `recovered >= 1`.
2. **Window B:** crash before dispatch -> PENDING claim without broker ref; reconciliation cancels it
   (no side effect), key becomes retryable.
3. **Window C:** crash between accept and ref-persist -> claim lacks broker ref but broker holds live
   position; reconciliation's positions-query finalizes FILLED (books position), no orphan.
4. **Window D:** existing covered path — ref persisted, broker OPEN -> marked OPEN; FILLED -> finalize.
5. **Idempotency / no double-dispatch:** two evaluations in the same second for the same
   strategy/symbol/side/qty -> second claim is a replay/409, **never** a second `place_order`.
6. **SIMULATED mode invariance:** behavior unchanged (no claim path, immediate FILLED write).

Regression: `pytest` on the full suite (manual + DMA + reconciliation + postback) must stay green.

---

## 9. Rollback semantics

- The idempotency key makes the whole entry **retry-safe**: a crash during any window leaves either a
  claim (recoverable/cancellable) or a completed FILLED row (replayable). There is no "half state".
- Broker side-effects are only ever booked (FILLED) via the CAS-protected finalizer, or left open and
  re-examined — never fabricated and never cancelled while possibly live.
- If the fix must be rolled back: reverting `trading_engine.py` / `order_reconciliation.py` to
  `131ca09c` returns the pre-fix behavior; any PENDING claims created by the fix are still correctly
  cancelled/finalized by the (reverted) reconciliation only if we do **not** revert
  `order_reconciliation.py` first. Document: revert **engine first, reconciliation second** on any
  hot rollback so lingering claims resolve cleanly. No data migration required in either direction.

---

## 10. DESIGN GATE decision

**APPROVE.** Design is internally consistent:

- Closes windows B, C, D with fully durable, recoverable records — **no orphaned live position
  possible** after broker acceptance.
- Reuses the proven manual/DMA claim pattern (`client_order_id` + `ux_orders_user_client_order_id`
  unique index) and the proven CAS finalizers — no new failure modes introduced.
- Window C is resolved **without** a new/adapter-unsupported broker capability, using the existing
  read-only `get_positions()` and a conservative never-fabricate policy.
- Zero schema/migration changes; self-contained to two files + one test flip.

**Outstanding low-risk enhancement (deferred, non-blocking):** broker `client_order_id` echo (§4a) to
eliminate the positions-query dependency for window C — separate follow-up, requires per-adapter work.

Proceed to implementation after approval. Implementation will be validated by flipping the RED repro
to GREEN and running the full suite.
