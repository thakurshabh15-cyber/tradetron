"""Broker-acceptance crash-window reconciliation for keyed DMA / manual orders.

The exact incident this closes
-----------------------------
  1. A keyed order is durably claimed (``orders.status = 'PENDING'``) and
     committed BEFORE any broker dispatch.
  2. The broker accepts the order; the broker's returned order reference is
     durably persisted on the SAME PENDING row IMMEDIATELY after acceptance,
     in its own commit, before any further local work — see the LIVE dispatch
     blocks in ``app/api/trades.py``.
  3. If the process dies before local finalization (``FILLED`` + trade +
     position commit), the stale PENDING row now carries the broker reference.
     This engine reads the broker's own order status back (READ-ONLY) and
     finalizes the local row from CONFIRMED broker state.

Hard rules (enforced by construction)
-------------------------------------
  * NEVER calls ``place_order()`` — reconciliation is broker-read-only.
  * NEVER fabricates a final state: an UNKNOWN, unexpected, unsupported, or
    errored status read leaves the row PENDING (no terminal write, no second
    submission).
  * ONLY reconciles keyed (``client_order_id`` NOT NULL), LIVE, PENDING rows
    that are OLDER than the conservative stale threshold.  Fresh PENDING
    orders are never touched (an in-flight HTTP request always finishes well
    inside the dispatch timeouts, which are all <= 30 s, far below the 120 s
    threshold).  Rows with a ``broker_order_id`` are reconciled via the
    broker's read-only ``get_order_status`` API.  Rows WITHOUT a
    ``broker_order_id`` (Window-C: crash before the broker reference was
    persisted) are reconciled via the broker's read-only ``get_positions``
    API to detect confirmed live exposure.
  * Bounded: fixed batch size, fixed sleep interval, no unbounded scans, no
    busy loop; a per-cycle failure never crashes the loop; graceful shutdown
    via task cancellation.
  * User/tenant isolation: an order is reconciled only through its OWN broker
    account, which must be CONNECTED, active, and owned by the order's user
    (same binding contract as ``app/brokers/postback.py``).
  * If the broker reference was never persisted (process died between broker
    acceptance and the reference commit, or the adapter returned no reference),
    the order is reconciled through Window-C recovery: the broker's READ-ONLY
    ``get_positions()`` is queried for a (symbol, side, quantity) match.
    Confirmed live exposure → finalize FILLED; no confident match (including
    confirmed no exposure) → stay PENDING.  Never a heuristic lookup, never
    a fabricated fill, never a new broker call.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# Referenced through the module (``app.brokers.get_broker_adapter``) rather
# than a from-import so tests can monkeypatch the factory at its canonical
# location and the lookup happens at call time.
import app.brokers
from app.brokers.postback import FINALIZED_ORDER_STATUSES
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord

logger = get_logger("engine.order_reconciliation")

# Conservative stale threshold: only PENDING claims older than this are ever
# reconciled.  Every broker dispatch on this codebase is bounded by a timeout
# of at most 30 s (Angel One SDK timeout, httpx timeouts), so a live in-flight
# request can never be older than this threshold unless the process died.
STALE_PENDING_MIN_AGE_SECONDS: float = 120.0
# Maximum orders examined per reconciliation pass (no unbounded scans).
RECONCILIATION_BATCH_SIZE: int = 10
# Sleep between scheduled passes (bounded loop, not busy).
RECONCILIATION_INTERVAL_SECONDS: float = 60.0

# Broker status vocabulary → canonical outcome.  Every entry is an upper-cased,
# exact-match token.  Anything not listed is UNKNOWN (never guessed).
_FILLED_TOKENS = frozenset({"FILLED", "COMPLETE", "COMPLETED"})
_CANCELLED_TOKENS = frozenset({"CANCELLED", "CANCELED", "EXPIRED", "CANCELLED/REJECTED"})
_REJECTED_TOKENS = frozenset({"REJECTED"})
_OPEN_TOKENS = frozenset({
    "OPEN", "NEW", "PENDING", "PENDING_NEW", "PARTIALLY_FILLED",
    "PARTIALLY FILLED", "TRIGGER PENDING", "TRIGGERED", "PENDING APPROVAL",
    "PENDING REVIEW", "PENDING MIS",
})


def _positive_float(value: Any) -> float | None:
    """Return ``value`` as a finite positive float, else None."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _classify_broker_status(raw: Any) -> str:
    """Map an adapter status token to FILLED / OPEN / REJECTED / CANCELLED /
    UNKNOWN.  Conservative: unknown vocabularies are never guessed."""
    if raw is None:
        return "UNKNOWN"
    token = str(raw).strip().upper()
    if token in _FILLED_TOKENS:
        return "FILLED"
    if token in _REJECTED_TOKENS:
        return "REJECTED"
    if token in _CANCELLED_TOKENS:
        return "CANCELLED"
    if token in _OPEN_TOKENS:
        return "OPEN"
    return "UNKNOWN"


# Canonical localized order statuses emitted downstream.  These are the ONLY
# values the reconcilers / ledger understand; every broker raw token must be
# reduced to one of them before any mutation (see ``normalize_broker_status``).
# They mirror the project's ``OrderStatus`` enum (FILLED / REJECTED / CANCELLED /
# OPEN).  Anything unrecognized is mapped to ``None`` (fail-safe, never guessed).
def normalize_broker_status(raw: Any) -> str | None:
    """Map a raw broker status token to a canonical order status.

    Reduction (single source of truth - the token sets above):
      FILLED / COMPLETE / COMPLETED        -> "FILLED"
      REJECTED                             -> "REJECTED"
      CANCELLED / CANCELED / EXPIRED / ... -> "CANCELLED"
      OPEN / NEW / PENDING / PARTIALLY...  -> "OPEN"
      anything else                        -> None  (fail-safe, never guessed)

    Returns ``None`` for an unknown/unrecognized status so callers can fail
    safe: they MUST NOT fabricate a fill and MUST NOT guess a terminal state.
    """
    classified = _classify_broker_status(raw)
    if classified == "UNKNOWN":
        return None
    return classified
class BrokerOrderReconciliationEngine:
    """Runs bounded, read-only broker-status reconciliation passes over stale
    keyed PENDING orders.  Shares the ``SessionLocal`` + asyncio scheduler
    primitives used by the broker session renewal engine."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.is_running = False

    async def reconcile_once(
        self,
        *,
        now: Optional[datetime] = None,
        max_orders: Optional[int] = None,
    ) -> dict[str, Any]:
        """Run ONE bounded reconciliation pass.  Never overlaps with a previous
        pass (in-process lock) and never raises: every per-order failure is
        contained and counted in the returned summary."""
        async with self._lock:
            return await self._run_pass(now=now, max_orders=max_orders)

    async def _run_pass(
        self, *, now: Optional[datetime], max_orders: Optional[int]
    ) -> dict[str, Any]:
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        cutoff = timestamp - timedelta(seconds=STALE_PENDING_MIN_AGE_SECONDS)
        batch = (
            RECONCILIATION_BATCH_SIZE
            if max_orders is None
            else max(1, int(max_orders))
        )
        summary: dict[str, Any] = {
            "scanned": 0,
            "filled": 0,
            "open": 0,
            "rejected": 0,
            "cancelled": 0,
            "unknown": 0,
            "skipped": 0,
            "errors": 0,
            "details": [],
        }

        async with SessionLocal() as db:
            stmt = (
                select(OrderRecord)
                .where(
                    OrderRecord.mode == "LIVE",
                    OrderRecord.status == "PENDING",
                    OrderRecord.client_order_id.is_not(None),
                    OrderRecord.created_at < cutoff,
                )
                .order_by(OrderRecord.created_at.asc())
                .limit(batch)
            )
            rows = (await db.execute(stmt)).scalars().all()
            summary["scanned"] = len(rows)

            for order in rows:
                try:
                    outcome, detail = await self._reconcile_order(db, order)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive
                    await db.rollback()
                    summary["errors"] += 1
                    summary["details"].append(
                        {"id": order.id, "outcome": "error", "detail": type(exc).__name__}
                    )
                    logger.warning(
                        "[OrderReconciliation] order %s failed: %s", order.id, exc
                    )
                    continue

                bucket = outcome if outcome in summary else "unknown"
                summary[bucket] += 1
                summary["details"].append(
                    {"id": order.id, "outcome": outcome, "detail": detail}
                )
                logger.info(
                    "[OrderReconciliation] order %s -> %s (%s)",
                    order.id, outcome, detail,
                )

        return summary

    async def _reconcile_order(
        self, db: AsyncSession, order: OrderRecord
    ) -> tuple[str, str]:
        """Reconcile ONE stale keyed PENDING order.  Returns (outcome, detail)."""
        if not order.client_order_id:
            return "skipped", "unkeyed"
        if order.status != "PENDING":
            return "skipped", f"status:{order.status}"
        if order.mode != "LIVE":
            return "skipped", f"mode:{order.mode}"

        # ── Window-B: broker ref present → existing get_order_status path ──
        if order.broker_order_id:
            return await self._reconcile_with_broker_ref(db, order)

        # ── Window-C: no broker ref (crash before ref persist) ────────────
        # Use read-only get_positions() to detect confirmed live exposure.
        return await self._resolve_window_c(db, order)

    async def _reconcile_with_broker_ref(
        self, db: AsyncSession, order: OrderRecord
    ) -> tuple[str, str]:
        """Reconcile a stale PENDING order that HAS a broker reference.

        Uses the broker's read-only get_order_status API (Window-B/D path).
        """
        if not order.broker_order_id:
            return "skipped", "no_broker_reference"

        account = (
            await db.execute(
                select(BrokerAccountRecord).where(
                    BrokerAccountRecord.id == order.broker_account_id,
                    BrokerAccountRecord.status == "CONNECTED",
                    BrokerAccountRecord.is_active.is_(True),
                    BrokerAccountRecord.user_id == order.user_id,
                )
            )
        ).scalars().first()
        if account is None:
            return "skipped", "account_unavailable"

        adapter = app.brokers.get_broker_adapter(account)
        try:
            if hasattr(adapter, "get_order_status_with_symbol"):
                resp = await adapter.get_order_status_with_symbol(
                    order.symbol, order.broker_order_id
                )
            else:
                resp = await adapter.get_order_status(order.broker_order_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Adapter missing a status method, BROKER_MODE != live, network
            # error... all collapse to UNKNOWN: never fabricate, never submit.
            return "unknown", f"status_error:{type(exc).__name__}"

        payload = resp if isinstance(resp, dict) else {}
        canonical = _classify_broker_status(payload.get("status"))
        if canonical == "FILLED":
            return await self._finalize_filled(db, order, payload)
        if canonical == "OPEN":
            return "open", "broker_open"
        if canonical in ("REJECTED", "CANCELLED"):
            return await self._mark_terminal(db, order, canonical)
        return "unknown", f"raw_status:{payload.get('status')!r}"

    async def _resolve_window_c(
        self, db: AsyncSession, order: OrderRecord
    ) -> tuple[str, str]:
        """Window-C recovery: no broker reference on the order row.

        The broker accepted the order (confirmed by the durable claim being
        committed before dispatch), but the process crashed before the broker
        reference was persisted.  We use the broker's READ-ONLY
        ``get_positions()`` to check for confirmed live exposure.

        Adapters now return **canonical** position dicts with keys:
            ``symbol`` (str), ``quantity`` (signed int), ``side``
            (``"LONG"`` | ``"SHORT"``), ``average_price`` (float).

        Resolution semantics:
          - Confident match (same symbol, same side, sufficient quantity)
            → finalize FILLED using broker-reported data.
          - Everything else (no exposure, partial, opposite direction,
            ambiguous multi-position, error, non-list)
            → stay PENDING.  Never fabricate CANCELLED from a positions
            snapshot: the order might have been rejected, might still be
            working, or could have filled then immediately netted out.
        """
        account = (
            await db.execute(
                select(BrokerAccountRecord).where(
                    BrokerAccountRecord.id == order.broker_account_id,
                    BrokerAccountRecord.status == "CONNECTED",
                    BrokerAccountRecord.is_active.is_(True),
                    BrokerAccountRecord.user_id == order.user_id,
                )
            )
        ).scalars().first()
        if account is None:
            return "unknown", "window_c_account_unavailable"

        adapter = app.brokers.get_broker_adapter(account)

        # Call the broker's READ-ONLY get_positions() to detect live exposure.
        if not hasattr(adapter, "get_positions"):
            return "unknown", "window_c_no_get_positions"

        try:
            positions = await adapter.get_positions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Network / broker error → uncertain, never fabricate.
            return "unknown", f"window_c_positions_error:{type(exc).__name__}"

        if not isinstance(positions, list):
            return "unknown", "window_c_positions_not_a_list"

        # --- Canonical position matching --------------------------------
        # Positions are already normalized by the adapter: every dict has
        #   symbol (str), quantity (signed int), side (LONG|SHORT),
        #   average_price (float).
        target_symbol = order.symbol.upper()
        target_qty = abs(order.quantity)
        target_side = "LONG" if order.side.upper() == "BUY" else "SHORT"

        matched_position = None
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            pos_symbol = str(pos.get("symbol", "")).upper()
            if pos_symbol != target_symbol:
                continue
            pos_side = str(pos.get("side", "")).upper()
            if pos_side and pos_side != target_side:
                continue
            try:
                pos_qty = abs(int(pos.get("quantity", 0)))
            except (TypeError, ValueError):
                continue
            if pos_qty < target_qty:
                continue
            matched_position = pos
            break

        if matched_position is not None:
            # Confirmed live exposure → finalize FILLED using broker data.
            fill_price = _positive_float(matched_position.get("average_price")) or _positive_float(order.price)
            if fill_price is None:
                return "unknown", "window_c_confirmed_but_no_price"
            return await self._finalize_filled(db, order, {
                "status": "FILLED",
                "average_price": fill_price,
                "filled_quantity": target_qty,
            })

        # No confident match → uncertain.  Stay PENDING: the order might
        # have been rejected, might still be working at the broker, or
        # could have filled and been immediately netted out.
        return "unknown", "window_c_no_confirmed_exposure"

    async def _finalize_filled(
            self, db: AsyncSession, order: OrderRecord, resp: dict[str, Any]
        ) -> tuple[str, str]:
            """Finalize a confirmed broker FILLED order: state the row from the
            broker's own prices, create the trade + position (postback pattern)."""
            fill_price = (
                _positive_float(resp.get("average_price"))
                or _positive_float(resp.get("filled_price"))
                or _positive_float(order.price)
            )
            if fill_price is None:
                # Confirmed fill but no price anywhere → refusing to fabricate one.
                return "unknown", "filled_without_price"

            try:
                fill_qty = int(resp.get("filled_quantity") or order.quantity)
            except (TypeError, ValueError):
                fill_qty = order.quantity
            fill_qty = max(1, fill_qty)
            fill_price = round(fill_price, 2)

            # CAS: only an unfinalized, unbooked row may be finalized.  A
            # concurrent retry / postback / worker that already finished loses
            # and we skip.
            result = await db.execute(
                update(OrderRecord)
                .where(
                    OrderRecord.id == order.id,
                    # Same atomic claim predicate as the broker postback
                    # reconciler (app/brokers/postback.py).  The row may only be
                    # finalized while it is not already finalized, has no linked
                    # position, and carries no fill trade - so a concurrent
                    # postback that already booked (or is booking) the fill makes
                    # this CAS match 0 rows and the reconciliation finalizer
                    # skips instead of double-booking trade + position.
                    OrderRecord.status.notin_(FINALIZED_ORDER_STATUSES),
                    OrderRecord.position_id.is_(None),
                    ~exists(
                        select(TradeRecord.id).where(
                            TradeRecord.order_id == OrderRecord.id
                        )
                    ),
                )
                .values(
                    status="FILLED",
                    filled_price=fill_price,
                    filled_quantity=fill_qty,
                    error_message=None,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await db.rollback()
                return "skipped", "concurrent_finalize_won"
            await db.refresh(order)

            trade = TradeRecord(
                id=str(uuid.uuid4()),
                order_id=order.id,
                strategy_id=order.strategy_id,
                symbol=order.symbol,
                side=order.side,
                quantity=fill_qty,
                price=fill_price,
                entry_price=fill_price,
                pnl=0.0,
                mode=order.mode,
                user_id=order.user_id,
                exit_reason="RECONCILIATION_FILL",
            )
            position = PositionRecord(
                id=str(uuid.uuid4()),
                user_id=order.user_id,
                broker_account_id=order.broker_account_id,
                symbol=order.symbol,
                side="LONG" if order.side.upper() == "BUY" else "SHORT",
                quantity=fill_qty,
                entry_price=fill_price,
                current_price=fill_price,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                mode=order.mode,
                status="OPEN",
                opened_at=datetime.now(timezone.utc),
            )
            db.add(trade)
            db.add(position)
            await db.flush()
            order.position_id = position.id
            await db.commit()
            return "filled", f"average_price={fill_price}"

    async def _mark_terminal(
        self, db: AsyncSession, order: OrderRecord, canonical: str,
        *,
        message: str | None = None,
    ) -> tuple[str, str]:
        """Mark a broker-confirmed terminal state (REJECTED / CANCELLED) on the
        local row.  Both are retryable states under the existing claim CAS.

        *message* optionally overrides the default error message; used by
        Window-C resolution which bases the terminal state on
        ``get_positions()`` rather than a broker order-status response."""
        effective_msg = message or (
            f"Broker status reconciliation: broker reports {canonical}"
        )
        result = await db.execute(
            update(OrderRecord)
            .where(OrderRecord.id == order.id, OrderRecord.status == "PENDING")
            .values(
                status=canonical,
                error_message=effective_msg,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            return "skipped", "concurrent_finalize_won"
        await db.commit()
        return canonical.lower(), canonical

class BrokerOrderReconciliationScheduler:
    """Background loop running bounded reconciliation passes on a fixed
    interval.  Mirrors ``BrokerSessionScheduler`` lifecycle: start/stop wired
    from the application lifespan, graceful cancellation on shutdown."""

    def __init__(
        self,
        engine: Optional[BrokerOrderReconciliationEngine] = None,
        interval_seconds: float = RECONCILIATION_INTERVAL_SECONDS,
    ) -> None:
        self.engine = engine or BrokerOrderReconciliationEngine()
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "[OrderReconciliation] Scheduler STARTED (interval=%ss)",
            self.interval_seconds,
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[OrderReconciliation] Scheduler STOPPED.")

    async def _run_loop(self) -> None:
        # Startup pass: pick up crash survivors left over from a previous
        # process (bounded, error-contained, logs only).
        try:
            await asyncio.sleep(2)  # brief delay for db startup
            if not self._running:
                return
            summary = await self.engine.reconcile_once()
            logger.info("[OrderReconciliation] startup pass: %s", summary)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("[OrderReconciliation] startup pass notice: %s", exc)

        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                if not self._running:
                    break
                summary = await self.engine.reconcile_once()
                logger.info("[OrderReconciliation] pass complete: %s", summary)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[OrderReconciliation] pass failure: %s", exc)
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    break


broker_order_reconciliation_engine = BrokerOrderReconciliationEngine()
broker_order_reconciliation_scheduler = BrokerOrderReconciliationScheduler(
    broker_order_reconciliation_engine
)

__all__ = [
    "RECONCILIATION_BATCH_SIZE",
    "RECONCILIATION_INTERVAL_SECONDS",
    "STALE_PENDING_MIN_AGE_SECONDS",
    "BrokerOrderReconciliationEngine",
    "BrokerOrderReconciliationScheduler",
    "broker_order_reconciliation_engine",
    "broker_order_reconciliation_scheduler",
    "normalize_broker_status",
]
