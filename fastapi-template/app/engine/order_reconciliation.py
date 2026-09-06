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
    that carry a durable ``broker_order_id`` and are OLDER than the
    conservative stale threshold.  Fresh PENDING orders are never touched
    (an in-flight HTTP request always finishes well inside the dispatch
    timeouts, which are all <= 30 s, far below the 120 s threshold).
  * Bounded: fixed batch size, fixed sleep interval, no unbounded scans, no
    busy loop; a per-cycle failure never crashes the loop; graceful shutdown
    via task cancellation.
  * User/tenant isolation: an order is reconciled only through its OWN broker
    account, which must be CONNECTED, active, and owned by the order's user
    (same binding contract as ``app/brokers/postback.py``).
  * If the broker reference was never persisted (process died between broker
    acceptance and the reference commit, or the adapter returned no reference),
    the order cannot be located at the broker and stays PENDING + unreconcilable
    by design — no heuristic lookup is ever attempted.
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
                    OrderRecord.broker_order_id.is_not(None),
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
        if not order.broker_order_id:
            return "skipped", "no_broker_reference"
        if order.status != "PENDING":
            return "skipped", f"status:{order.status}"
        if order.mode != "LIVE":
            return "skipped", f"mode:{order.mode}"

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
                quantity=order.quantity,
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
                quantity=order.quantity,
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
        self, db: AsyncSession, order: OrderRecord, canonical: str
    ) -> tuple[str, str]:
        """Mark a broker-confirmed terminal state (REJECTED / CANCELLED) on the
        local row.  Both are retryable states under the existing claim CAS."""
        result = await db.execute(
            update(OrderRecord)
            .where(OrderRecord.id == order.id, OrderRecord.status == "PENDING")
            .values(
                status=canonical,
                error_message=(
                    f"Broker status reconciliation: broker reports {canonical}"
                ),
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
