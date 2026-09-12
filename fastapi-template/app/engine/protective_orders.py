"""Exchange-level protective-order lifecycle (Phase 15C).

Brings genuine broker-side SL/TP protection to LIVE positions while keeping
PAPER trading exactly as-is.

Domain (position-level) protection states:

    UNPROTECTED          no SL/TP configured (or nothing to protect)
    PAPER                PAPER mode position with engine-simulated SL/TP
    PROTECTION_PENDING   LIVE, SL/TP configured, broker placement in-flight
                         (also the crash-survivor state reconciled on restart)
    PROTECTED            LIVE - genuine broker protective orders are placed
                         and their broker references are persisted.  NEVER set
                         without broker evidence.
    PROTECTION_FAILED    LIVE - placement failed / broker rejected the leg /
                         the broker does not support native protection.  The
                         position stays OPEN but is honestly UNPROTECTED.
    STOP_TRIGGERED       LIVE - broker reported the SL leg FILLED.
    TARGET_TRIGGERED     LIVE - broker reported the TP leg FILLED.
    CLOSED               position closed / protection intentionally torn down.

Hard rules
----------
* NO FABRICATION - a position reaches PROTECTED only after every configured
  leg returned a genuine broker_protective_order_id from the adapter.
* Provider capability is read from supports_native_protection() BEFORE any
  placement; an unsupported leg fails closed to PROTECTION_FAILED.
* Idempotent - one live protective row per (position_id, leg) (unique index);
  retries CAS-reclaim FAILED/CANCELLED rows and never double-dispatch.
* Crash-hardened - the PENDING_PLACEMENT row is committed BEFORE the broker
  call and the broker reference is persisted in its own commit immediately
  after acceptance; a crash between broker acceptance and reference commit
  leaves a reference-less PENDING row that reconciliation fails-closed for
  manual review instead of re-poking the broker blind (no false duplicate).
* Tenant isolation - every operation re-validates the position owner against
  the broker account owner; user B can neither read nor mutate user A's
  protection.
* Orphans are NEVER auto-closed - a protective row whose position no longer
  exists is only resolved locally; we never invent a close or a broker id.
* PAPER mode is untouched - no protective rows are ever created for PAPER.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import (
    BrokerProtectionCapability,
    PROTECTIVE_ORDER_TYPE_SL_LIMIT,
    PROTECTIVE_ORDER_TYPE_SL_MARKET,
    PROTECTIVE_ORDER_TYPE_TP_LIMIT,
)
from app.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.broker_account import BrokerAccountRecord
from app.models.protective_order import ProtectiveOrderRecord
from app.models.trading import PositionRecord
from app.schemas.trading import OrderRequest, Side

logger = get_logger("engine.protective_orders")

# ── Position protection states ───────────────────────────────────────────────
PROTECTION_STATE_UNPROTECTED = "UNPROTECTED"
PROTECTION_STATE_PAPER = "PAPER"
PROTECTION_STATE_PENDING = "PROTECTION_PENDING"
PROTECTION_STATE_PROTECTED = "PROTECTED"
PROTECTION_STATE_FAILED = "PROTECTION_FAILED"
PROTECTION_STATE_STOP_TRIGGERED = "STOP_TRIGGERED"
PROTECTION_STATE_TARGET_TRIGGERED = "TARGET_TRIGGERED"
PROTECTION_STATE_CLOSED = "CLOSED"

PROTECTION_STATES = frozenset(
    {
        PROTECTION_STATE_UNPROTECTED,
        PROTECTION_STATE_PAPER,
        PROTECTION_STATE_PENDING,
        PROTECTION_STATE_PROTECTED,
        PROTECTION_STATE_FAILED,
        PROTECTION_STATE_STOP_TRIGGERED,
        PROTECTION_STATE_TARGET_TRIGGERED,
        PROTECTION_STATE_CLOSED,
    }
)

# ── Protective-order row statuses ────────────────────────────────────────────
ROW_PENDING = "PENDING_PLACEMENT"
ROW_PLACED = "PLACED"
ROW_COMPLETE = "COMPLETE"
ROW_CANCELLED = "CANCELLED"
ROW_FAILED = "FAILED"
ROW_RESOLVED = "RESOLVED"

LEG_STOP_LOSS = "STOP_LOSS"
LEG_TAKE_PROFIT = "TAKE_PROFIT"
LEGS = (LEG_STOP_LOSS, LEG_TAKE_PROFIT)
# A PENDING_PLACEMENT row without a broker reference older than this is treated
# as a crash survivor whose placement outcome is unknowable — it is failed
# closed for manual review instead of being blindly re-poked (no duplicate
# broker orders from an ambiguous crash window).
CRASH_WINDOW_STALE_SECONDS = 120.0

# How often the periodic protect-pass runs (mirrors the broker order
# reconciliation cadence; a startup pass always runs first).
PROTECTION_RECONCILE_INTERVAL_SECONDS: float = 30.0

# Broker statuses that mean the protective leg is actually live at the broker
# (used to honestly promote a crash-survivor PENDING row whose placement the
# broker confirms).
_LIVE_OPEN_STATUSES = frozenset({"OPEN", "NEW", "PARTIALLY_FILLED"})

# Broker statuses that mean the protective leg reached its terminal fill.
_FILLED_STATUSES = frozenset({"FILLED", "COMPLETE", "COMPLETED"})
# Broker statuses that mean the protective leg is no longer at the broker.
_GONE_STATUSES = frozenset({"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"})


def normalize_protection_state(raw: str | None) -> str:
    """Return a canonical protection state (honest fallback to UNPROTECTED)."""
    if raw and raw.upper() in PROTECTION_STATES:
        return raw.upper()
    return PROTECTION_STATE_UNPROTECTED


@dataclass
class ProtectionOutcome:
    """Deterministic result of a protection operation."""

    ok: bool
    state: str
    position_id: Optional[str] = None
    mode: str = "PAPER"
    error: Optional[str] = None
    legs: list[dict[str, Any]] = field(default_factory=list)
    # True only when the caller should fail the LIVE entry closed because
    # protection could not be established and protection is mandatory.
    fail_closed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "protection_state": self.state,
            "position_id": self.position_id,
            "mode": self.mode,
            "error": self.error,
            "legs": self.legs,
            "fail_closed": self.fail_closed,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _closing_side_for(position: PositionRecord) -> Side:
    """Closing leg side: the opposite of the open position side."""
    return Side.SELL if (position.side in ("LONG", "BUY")) else Side.BUY


def _protection_capability(broker: Any) -> BrokerProtectionCapability:
    """Read the adapter's native-protection capability (fail-closed default)."""
    probe = getattr(broker, "supports_native_protection", None)
    if probe is None:
        return BrokerProtectionCapability(
            adapter=type(broker).__name__,
            native_sl=False,
            native_tp=False,
            bracket=False,
            replace=False,
            reason=(
                f"{type(broker).__name__} does not declare protective-order "
                "support — treated as unsupported (fail-closed)"
            ),
        )
    try:
        cap = probe()
        if isinstance(cap, BrokerProtectionCapability):
            return cap
    except Exception as exc:  # noqa: BLE001 - a broken capability probe must fail closed
        logger.warning("[Protection] capability probe failed for %s: %s", type(broker).__name__, exc)
    return BrokerProtectionCapability(
        adapter=type(broker).__name__,
        native_sl=False,
        native_tp=False,
        bracket=False,
        replace=False,
        reason=f"capability probe failed for {type(broker).__name__}",
    )
# ── protective-leg planning helpers ─────────────────────────────────────────


def _num(value: Any) -> Optional[float]:
    """None-safe float coercion used for change detection."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _row_age(row: Any) -> Optional[float]:
    """Age in seconds of a protective row (None if the timestamp is missing)."""
    created = getattr(row, "created_at", None)
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


def _leg_plan(
    *,
    position: PositionRecord,
    capability: BrokerProtectionCapability,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compose the desired protective-leg plan for a LIVE position.

    Returns ``(plan, failures)``; each plan entry::

        leg            LEG_STOP_LOSS / LEG_TAKE_PROFIT
        order_type     normalized broker literal (SL_MARKET / SL_LIMIT / TP_LIMIT)
        trigger_price  the SL/TP level (adapter maps it into its trigger field)
        limit_price    LIMIT legs: same level; SL_MARKET: None
        side           closing side (opposite the open side)

    Provider capability is read BEFORE planning: an unsupported leg is a hard
    failure (fail-closed) — never a fabricated success.  Prices come from the
    position's committed SL/TP columns, so replacements automatically reflect
    freshly PATCHed levels.
    """
    plan: list[dict[str, Any]] = []
    failures: list[str] = []

    if position.stop_loss_price is not None:
        if not capability.native_sl:
            failures.append("broker does not support native stop-loss")
        else:
            sl = _num(position.stop_loss_price)
            plan.append(
                {
                    "leg": LEG_STOP_LOSS,
                    "order_type": (
                        PROTECTIVE_ORDER_TYPE_SL_LIMIT
                        if PROTECTIVE_ORDER_TYPE_SL_LIMIT in capability.order_types
                        else PROTECTIVE_ORDER_TYPE_SL_MARKET
                    ),
                    "trigger_price": sl,
                    "limit_price": (
                        sl
                        if PROTECTIVE_ORDER_TYPE_SL_LIMIT in capability.order_types
                        else None
                    ),
                    "side": _closing_side_for(position),
                }
            )

    if position.take_profit_price is not None:
        if not capability.native_tp:
            failures.append("broker does not support native take-profit")
        else:
            tp = _num(position.take_profit_price)
            plan.append(
                {
                    "leg": LEG_TAKE_PROFIT,
                    "order_type": PROTECTIVE_ORDER_TYPE_TP_LIMIT,
                    "trigger_price": tp,
                    "limit_price": tp,
                    "side": _closing_side_for(position),
                }
            )

    return plan, failures


def _order_request_for(
    position: PositionRecord, entry: dict[str, Any]
) -> OrderRequest:
    """Compose the adapter ``OrderRequest`` for one protective leg.

    ``trigger_price`` carries the SL/TP level; ``price`` carries the LIMIT
    price for SL_LIMIT / TP_LIMIT legs (None for trigger-only SL_MARKET).
    Both stay empty for plain MARKET/LIMIT orders, so legacy ordering paths
    are unaffected.
    """
    return OrderRequest(
        symbol=position.symbol,
        side=entry["side"],
        quantity=position.quantity,
        order_type=entry["order_type"],
        price=entry.get("limit_price"),
        trigger_price=entry.get("trigger_price"),
    )


def _derive_position_state(rows: Any, previous_state: str) -> str:
    """Derive the honest position protection state from its protective rows.

    * A COMPLETE STOP_LOSS row means the broker reported the SL leg FILLED ->
      STOP_TRIGGERED (broker truth, never guessed).
    * A COMPLETE TAKE_PROFIT row -> TARGET_TRIGGERED.
    * Any FAILED row -> PROTECTION_FAILED.
    * Any PENDING_PLACEMENT row -> PROTECTION_PENDING.
    * All surviving rows PLACED with broker references -> PROTECTED.
    * Otherwise the previous state is kept (an empty set -> UNPROTECTED).
    """
    rows = list(rows or [])
    if not rows:
        return PROTECTION_STATE_UNPROTECTED
    complete_legs = {r.leg for r in rows if r.status == ROW_COMPLETE}
    if LEG_STOP_LOSS in complete_legs:
        return PROTECTION_STATE_STOP_TRIGGERED
    if LEG_TAKE_PROFIT in complete_legs:
        return PROTECTION_STATE_TARGET_TRIGGERED
    if any(r.status == ROW_FAILED for r in rows):
        return PROTECTION_STATE_FAILED
    if any(r.status == ROW_PENDING for r in rows):
        return PROTECTION_STATE_PENDING
    if rows and all(
        r.status == ROW_PLACED and r.broker_protective_order_id for r in rows
    ):
        return PROTECTION_STATE_PROTECTED
    return previous_state or PROTECTION_STATE_UNPROTECTED


def _outcome_legs(rows: Any) -> list[dict[str, Any]]:
    """Snapshot protective rows into an API-safe ``legs`` list."""
    legs: list[dict[str, Any]] = []
    for r in rows or []:
        legs.append(
            {
                "leg": r.leg,
                "status": r.status,
                "order_type": r.order_type,
                "trigger_price": r.trigger_price,
                "limit_price": r.limit_price,
                "broker_protective_order_id": r.broker_protective_order_id,
                "broker_reported_status": r.broker_reported_status,
                "error": r.last_error,
            }
        )
    return legs


class ProtectiveOrderManager:
    """Idempotent, crash-hardened lifecycle for exchange-level protective orders."""

    MAX_WORKERS = 8

    def __init__(self) -> None:
        self._position_locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(self.MAX_WORKERS)

    def _lock_for(self, position_id: str) -> asyncio.Lock:
        return self._position_locks.setdefault(position_id, asyncio.Lock())

    # ── public API ───────────────────────────────────────────────────────────

    async def ensure_position_protection(
        self,
        position_id: str,
        *,
        authorized_user_id: Optional[str] = None,
        force: bool = False,
    ) -> ProtectionOutcome:
        """Arm protective orders for a LIVE position (idempotent).

        No-op for PAPER positions (they keep the in-engine simulated SL/TP).
        Never raises for broker/placement failures — they produce an honest
        PROTECTION_FAILED outcome.  Raises only for systemic config errors.
        """
        async with self._semaphore:
            async with self._lock_for(position_id):
                return await self._ensure_locked(
                    position_id, authorized_user_id=authorized_user_id, force=force
                )

    async def cancel_position_protection(
        self,
        position_id: str,
        *,
        authorized_user_id: Optional[str] = None,
        reason: str = "",
        state_after: str = PROTECTION_STATE_CLOSED,
    ) -> ProtectionOutcome:
        """Cancel remaining protective orders for a position (idempotent).

        Called on position close and on manual disarm.  Best-effort at the
        broker (cancellation failure is logged, never fatal).
        """
        async with self._semaphore:
            async with self._lock_for(position_id):
                return await self._cancel_locked(
                    position_id, authorized_user_id=authorized_user_id,
                    reason=reason, state_after=state_after,
                )

    async def replace_position_protection(
        self,
        position_id: str,
        *,
        authorized_user_id: Optional[str] = None,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
    ) -> ProtectionOutcome:
        """Replace protective orders after SL/TP levels move (risk-targets PATCH).

        Only changed legs are replaced; unchanged legs stay untouched.
        """
        async with self._semaphore:
            async with self._lock_for(position_id):
                return await self._replace_locked(
                    position_id, authorized_user_id, new_sl, new_tp
                )

    async def reconcile_once(self, *, authorized_user_id: Optional[str] = None) -> dict[str, Any]:
        """Periodic protective-order reconciliation (crash recovery + broker truth).

        * PROTECTION_PENDING / PROTECTED positions are re-validated against the
          broker (per-position locks prevent cross-worker races).
        * A crashed reference-less PENDING_PLACEMENT row older than the crash
          window is failed closed for manual review (never blind re-poke).
        * A broker-reported terminal leg (FILLED/CANCELLED/REJECTED) updates the
          position state honestly (STOP_TRIGGERED / TARGET_TRIGGERED / FAILED).
        * Orphan protective rows (position gone) are resolved locally only —
          never auto-closed, never fabricated.
        """
        report: dict[str, Any] = {
            "scanned_positions": 0,
            "recovered": [],
            "failed_closed": [],
            "triggered": [],
            "orphans_resolved": 0,
            "closed_leftovers": 0,
            "errors": [],
        }
        async with SessionLocal() as db:
            stmt = select(PositionRecord).where(
                PositionRecord.mode == "LIVE",
                PositionRecord.status == "OPEN",
                PositionRecord.protection_state.in_(
                    (PROTECTION_STATE_PENDING, PROTECTION_STATE_PROTECTED)
                ),
            )
            positions = (await db.execute(stmt)).scalars().all()
            report["scanned_positions"] = len(positions)

            # ── orphan / closed-position sweep ─────────────────────────────────
            # Orphan rows (position deleted) are resolved locally only — never
            # auto-closed at the broker, never fabricated.  Leftover live rows on
            # a CLOSED position are a crash/close race artifact: mark them
            # CANCELLED so a stale live stop never sits against zero inventory.
            orphan_rows = list(
                (
                    await db.execute(
                        select(ProtectiveOrderRecord)
                        .outerjoin(
                            PositionRecord,
                            PositionRecord.id == ProtectiveOrderRecord.position_id,
                        )
                        .where(PositionRecord.id.is_(None))
                    )
                ).scalars().all()
            )
            closed_leftover_rows = list(
                (
                    await db.execute(
                        select(ProtectiveOrderRecord)
                        .join(
                            PositionRecord,
                            PositionRecord.id == ProtectiveOrderRecord.position_id,
                        )
                        .where(
                            PositionRecord.status == "CLOSED",
                            ProtectiveOrderRecord.status.in_(
                                (ROW_PENDING, ROW_PLACED)
                            ),
                        )
                    )
                ).scalars().all()
            )
            if orphan_rows:
                now = _utcnow()
                for r in orphan_rows:
                    r.status = ROW_RESOLVED
                    r.last_error = "orphan protective row (position no longer exists)"
                    r.broker_reported_status = None
                    r.updated_at = now
                report["orphans_resolved"] = len(orphan_rows)
            if closed_leftover_rows:
                now = _utcnow()
                for r in closed_leftover_rows:
                    r.status = ROW_CANCELLED
                    r.last_error = "position closed while protective order remained"
                    r.updated_at = now
                report["closed_leftovers"] = len(closed_leftover_rows)
            if orphan_rows or closed_leftover_rows:
                await db.commit()

            for pos in positions:
                async with self._lock_for(pos.id):
                    try:
                        await self._reconcile_one(db, pos, authorized_user_id, report)
                    except Exception as exc:  # noqa: BLE001 - per-position containment
                        report["errors"].append({"position_id": pos.id, "error": str(exc)})
                        logger.error("[Protection] reconcile failed for %s: %s", pos.id, exc)
            await db.commit()

        # Fresh place passes run outside the scan session to keep broker I/O out
        # of long-held transactions.
        for pos_id in list(report["recovered"]):
            try:
                outcome = await self.ensure_position_protection(
                    pos_id, authorized_user_id=authorized_user_id
                )
                if not outcome.ok or outcome.fail_closed:
                    report["failed_closed"].append(
                        {"position_id": pos_id, "error": outcome.error}
                    )
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"position_id": pos_id, "error": str(exc)})
        return report
# ── internals ────────────────────────────────────────────────────────────

    async def _cancel_locked(
        self, position_id, *, authorized_user_id, reason, state_after
    ) -> ProtectionOutcome:
        async with SessionLocal() as db:
            position = await db.get(PositionRecord, position_id)
            if position is None:
                return ProtectionOutcome(
                    ok=False, state=PROTECTION_STATE_UNPROTECTED,
                    position_id=position_id, error="position not found",
                )
            if position.mode != "LIVE":
                return ProtectionOutcome(
                    ok=True,
                    state=(PROTECTION_STATE_PAPER if position.protection_state == PROTECTION_STATE_PAPER else PROTECTION_STATE_UNPROTECTED),
                    position_id=position_id, mode="PAPER",
                )
            if authorized_user_id and position.user_id != authorized_user_id:
                return ProtectionOutcome(
                    ok=False, state=position.protection_state,
                    position_id=position_id, mode="LIVE",
                    error="not authorized for this position",
                )

            acc, broker = await self._resolve_broker(db, position)
            rows = (
                await db.execute(
                    select(ProtectiveOrderRecord).where(
                        ProtectiveOrderRecord.position_id == position_id
                    )
                )
            ).scalars().all()

            if broker is None:
                # No broker resolution: resolve rows locally — a protection
                # teardown must never fail a position close.
                await self._mark_rows(
                    db, position_id, ROW_RESOLVED,
                    f"no broker for cancel: {reason or 'position close'}",
                )
                await self._set_position_state(
                    db, position, state_after, None,
                    reason or "protection cancelled (no broker)",
                )
                await db.commit()
                return ProtectionOutcome(
                    ok=True, state=state_after, position_id=position_id, mode="LIVE"
                )

            cancel_failed: list[ProtectiveOrderRecord] = []
            for row in rows:
                if row.status not in (ROW_PLACED, ROW_PENDING) or not row.broker_protective_order_id:
                    continue
                ok = await self._broker_cancel(
                    db, broker, row, reason or "protection teardown"
                )
                if not ok:
                    cancel_failed.append(row)

            # A leg whose broker cancel did NOT succeed must not be recorded as
            # locally CANCELLED — the broker order may still be live.  It stays
            # FAILED with its reference + an explicit error so a later pass or
            # the operator can see and retry it (never a fabricated teardown).
            for row in cancel_failed:
                row.status = ROW_FAILED
                row.last_error = (
                    f"cancel failed on {reason or 'protection teardown'} — broker "
                    f"order {row.broker_protective_order_id} may still be live"
                )
                row.updated_at = _utcnow()

            await self._mark_rows(
                db, position_id, ROW_CANCELLED, reason or "cancelled",
                skip_statuses={ROW_FAILED, ROW_COMPLETE},
            )
            await self._set_position_state(
                db, position, state_after, None,
                reason or "protection cancelled",
            )
            await db.commit()
            legs = [
                {"leg": r.leg, "status": r.status, "broker_protective_order_id": r.broker_protective_order_id}
                for r in rows
            ]
            return ProtectionOutcome(
                ok=True, state=state_after, position_id=position_id, mode="LIVE", legs=legs
            )

    async def _resolve_broker(self, db, position: PositionRecord):
        """Resolve (account, adapter) for a LIVE position's own broker account.

        Tenant isolation: the account must be owned by the position owner and
        must be CONNECTED + active; SIMULATED accounts never resolve a broker
        (exchange-level protection is impossible for them).
        """
        if not position.broker_account_id:
            return None, None
        acc = await db.get(BrokerAccountRecord, position.broker_account_id)
        if acc is None:
            return None, None
        if position.user_id and acc.user_id != position.user_id:
            return None, None
        if (acc.broker_name or "").upper() == "SIMULATED":
            return acc, None
        if acc.status != "CONNECTED" or not acc.is_active:
            return acc, None
        import app.brokers
        broker = app.brokers.get_broker_adapter(acc)
        return acc, broker

    # ── internal lifecycle ──────────────────────────────────────────────────
    # Each method below runs under the per-position lock (and the worker
    # semaphore where invoked through a public wrapper).  Methods own
    # short-lived sessions and commit at every durability boundary — in
    # particular the broker reference is persisted in its own commit right
    # after acceptance (the crash-hardening invariant).

    async def _set_position_state(
        self,
        db: AsyncSession,
        position: PositionRecord,
        state: str,
        protected_at: Optional[datetime],
        error: Optional[str],
    ) -> None:
        """Persist the position protection state + honest audit trail."""
        position.protection_state = normalize_protection_state(state)
        if state == PROTECTION_STATE_PROTECTED and protected_at is not None:
            position.protected_at = protected_at
        if error is not None:
            position.protection_error = error[:2000] if error else None
        await db.flush()

    async def _mark_rows(
        self, db: AsyncSession, position_id: str, status: str, reason: str,
        *, skip_statuses: frozenset[str] = frozenset(),
    ) -> int:
        """Bulk-mark every protective row of a position with a status/reason.

        ``skip_statuses`` protect honest terminal states from being overwritten
        (e.g. a row whose broker cancel actually failed, or a COMPLETE leg that
        already reached its terminal fill).
        """
        rows = list(
            (
                await db.execute(
                    select(ProtectiveOrderRecord).where(
                        ProtectiveOrderRecord.position_id == position_id
                    )
                )
            ).scalars().all()
        )
        for row in rows:
            if row.status in skip_statuses:
                continue
            row.status = status
            row.last_error = reason
            row.updated_at = _utcnow()
        return len(rows)

    async def _broker_cancel(
        self,
        db: AsyncSession,
        broker: Any,
        row: ProtectiveOrderRecord,
        reason: str,
    ) -> bool:
        """Best-effort broker-side cancellation of ONE protective leg.

        Returns ``True`` when the broker acknowledged the cancellation (or there
        was nothing to cancel) and ``False`` when the cancel failed.  Never
        raises: a failed cancel is logged and recorded on the row so a later
        pass can retry — silently dropping a live stop against real inventory
        is the one failure this manager MUST NOT hide.  Callers MUST treat a
        ``False`` return as \"the broker order may still be live\" and never
        fabricate a local CANCELLED state or re-place a replacement leg.

        Symbol-context cancellation is preferred when the adapter exposes it
        (Binance's ``cancel_order`` cannot cancel without ``symbol``); the base
        contract's ``cancel_order(broker_order_id)`` is the generic fallback.
        """
        target = row.broker_protective_order_id
        if not target:
            return True
        cancel_with_symbol = getattr(broker, "cancel_order_with_symbol", None)
        try:
            if cancel_with_symbol is not None:
                await cancel_with_symbol(row.symbol, target)
            else:
                await broker.cancel_order(target)
            row.broker_reported_status = "CANCELLED"
            row.last_error = None
            logger.info(
                "[Protection] cancel sent for %s (%s)",
                target, reason,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - best-effort, logged
            row.broker_reported_status = (
                row.broker_reported_status or "UNKNOWN"
            ).upper()
            row.last_error = f"cancel failed ({reason}): {str(exc)[:500]}"
            logger.error(
                "[Protection] cancel FAILED for %s (%s): %s",
                target, reason, exc,
            )
            return False

    async def _replace_locked(
        self,
        position_id: str,
        authorized_user_id: Optional[str],
        new_sl: Optional[float],
        new_tp: Optional[float],
    ) -> ProtectionOutcome:
        """Persist moved SL/TP levels, then re-arm protective orders.

        The risk-targets PATCH commits new levels first; this method is the
        manager-side safety net that persists them from here when invoked with
        explicit values, so the idempotent per-leg path re-plans from the
        columns and cancels + re-places exactly the changed leg(s).
        """
        if new_sl is not None or new_tp is not None:
            async with SessionLocal() as db:
                position = await db.get(PositionRecord, position_id)
                if position is None:
                    return ProtectionOutcome(
                        ok=False, state=PROTECTION_STATE_UNPROTECTED,
                        position_id=position_id, error="position not found",
                    )
                if (position.mode or "PAPER") != "LIVE":
                    return ProtectionOutcome(
                        ok=True, state=PROTECTION_STATE_PAPER,
                        position_id=position_id, mode="PAPER",
                    )
                if authorized_user_id and position.user_id != authorized_user_id:
                    return ProtectionOutcome(
                        ok=False, state=position.protection_state,
                        position_id=position_id, mode="LIVE",
                        error="not authorized for this position",
                    )
                if new_sl is not None:
                    position.stop_loss_price = float(new_sl)
                if new_tp is not None:
                    position.take_profit_price = float(new_tp)
                await db.commit()
        return await self._ensure_locked(
            position_id, authorized_user_id=authorized_user_id, force=False
        )

    async def _ensure_locked(
        self,
        position_id: str,
        *,
        authorized_user_id: Optional[str],
        force: bool,
    ) -> ProtectionOutcome:
        """Core ensure-protection path (position lock held by the caller)."""
        async with SessionLocal() as db:
            position = await db.get(PositionRecord, position_id)
            if position is None:
                return ProtectionOutcome(
                    ok=False, state=PROTECTION_STATE_UNPROTECTED,
                    position_id=position_id, error="position not found",
                )
            if (position.mode or "PAPER") != "LIVE":
                return ProtectionOutcome(
                    ok=True, state=PROTECTION_STATE_PAPER,
                    position_id=position_id, mode="PAPER",
                )
            if authorized_user_id and position.user_id != authorized_user_id:
                return ProtectionOutcome(
                    ok=False, state=position.protection_state,
                    position_id=position_id, mode="LIVE",
                    error="not authorized for this position",
                )
            if position.status != "OPEN":
                return ProtectionOutcome(
                    ok=False, state=position.protection_state,
                    position_id=position_id, mode="LIVE",
                    error=f"position is {position.status}, not OPEN",
                )

            acc, broker = await self._resolve_broker(db, position)
            rows = list(
                (
                    await db.execute(
                        select(ProtectiveOrderRecord).where(
                            ProtectiveOrderRecord.position_id == position_id
                        )
                    )
                ).scalars().all()
            )

            # ── nothing (worth) protecting ─────────────────────────────────────
            if not position.stop_loss_price and not position.take_profit_price:
                complete_legs = {
                    row.leg for row in rows if row.status == ROW_COMPLETE
                }
                for row in rows:
                    if (
                        row.status == ROW_PLACED
                        and row.broker_protective_order_id
                        and broker is not None
                    ):
                        ok = await self._broker_cancel(
                            db, broker, row, "SL/TP removed"
                        )
                        if not ok:
                            row.status = ROW_FAILED
                            row.last_error = (
                                "SL/TP removed but broker cancel failed — broker "
                                f"order {row.broker_protective_order_id} may still be live"
                            )
                            row.updated_at = _utcnow()
                            continue
                    if row.status != ROW_COMPLETE:
                        row.status = ROW_CANCELLED
                        row.last_error = "no SL/TP configured"
                        row.updated_at = _utcnow()
                state = (
                    PROTECTION_STATE_STOP_TRIGGERED
                    if LEG_STOP_LOSS in complete_legs
                    else (
                        PROTECTION_STATE_TARGET_TRIGGERED
                        if LEG_TAKE_PROFIT in complete_legs
                        else PROTECTION_STATE_UNPROTECTED
                    )
                )
                await self._set_position_state(
                    db, position, state, None, "no SL/TP configured"
                )
                await db.commit()
                return ProtectionOutcome(
                    ok=True, state=state, position_id=position_id, mode="LIVE"
                )

            # ── capability gate: fail closed BEFORE any placement ─────────────
            capability = (
                _protection_capability(broker)
                if broker is not None
                else BrokerProtectionCapability(
                    adapter="none", native_sl=False, native_tp=False,
                    bracket=False, replace=False,
                    reason="no resolvable broker account",
                )
            )
            plan, failures = _leg_plan(position=position, capability=capability)
            if failures or not plan:
                await self._set_position_state(
                    db, position, PROTECTION_STATE_FAILED,
                    None, "; ".join(failures) or "nothing to protect",
                )
                await db.commit()
                return ProtectionOutcome(
                    ok=False, state=PROTECTION_STATE_FAILED,
                    position_id=position_id, mode="LIVE",
                    error="; ".join(failures) or "nothing to protect",
                    fail_closed=True,
                )

            # ── arm each planned leg (idempotent, crash-hardened) ─────────────
            await self._set_position_state(
                db, position, PROTECTION_STATE_PENDING, None, None
            )
            await db.commit()
            for entry in plan:
                row = next((r for r in rows if r.leg == entry["leg"]), None)
                try:
                    await self._ensure_one_leg(
                        db, position, broker, entry, existing=row, force=force
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - per-leg containment
                    logger.error(
                        "[Protection] leg %s failed for %s: %s",
                        entry["leg"], position_id, exc,
                    )
                    if row is None:
                        row = ProtectiveOrderRecord(
                            id=str(uuid.uuid4()),
                            position_id=position_id,
                            user_id=position.user_id,
                            broker_account_id=position.broker_account_id,
                            leg=entry["leg"], side=entry["side"],
                            symbol=position.symbol, quantity=position.quantity,
                            order_type=entry["order_type"],
                            trigger_price=entry.get("trigger_price"),
                            limit_price=entry.get("limit_price"),
                            status=ROW_FAILED,
                            last_error=f"per-leg failure: {exc}",
                            created_at=_utcnow(), updated_at=_utcnow(),
                        )
                        db.add(row)
                    else:
                        row.status = ROW_FAILED
                        row.last_error = f"per-leg failure: {exc}"
                        row.updated_at = _utcnow()
            await db.commit()

            # ── settle honest position state from the resulting rows ──────────
            rows = list(
                (
                    await db.execute(
                        select(ProtectiveOrderRecord).where(
                            ProtectiveOrderRecord.position_id == position_id
                        )
                    )
                ).scalars().all()
            )
            state = _derive_position_state(rows, position.protection_state)
            await self._set_position_state(
                db, position, state,
                _utcnow() if state == PROTECTION_STATE_PROTECTED else None,
                None,
            )
            await db.commit()

            legs = _outcome_legs(rows)
            if state == PROTECTION_STATE_PROTECTED:
                return ProtectionOutcome(
                    ok=True, state=state, position_id=position_id, mode="LIVE",
                    legs=legs,
                )
            if state in (
                PROTECTION_STATE_STOP_TRIGGERED, PROTECTION_STATE_TARGET_TRIGGERED,
            ):
                return ProtectionOutcome(
                    ok=True, state=state, position_id=position_id, mode="LIVE",
                    legs=legs,
                )
            if state == PROTECTION_STATE_PENDING:
                return ProtectionOutcome(
                    ok=False, state=state, position_id=position_id, mode="LIVE",
                    legs=legs,
                    error=(
                        "protective placement still in flight — reconciliation "
                        "will finalize it"
                    ),
                )
            return ProtectionOutcome(
                ok=False, state=state, position_id=position_id, mode="LIVE",
                legs=legs,
                error=next(
                    (r.last_error for r in rows if r.status == ROW_FAILED),
                    "protective placement failed",
                ),
                fail_closed=True,
            )
        # -- end of ensure-locked --

    async def _ensure_one_leg(
        self,
        db: AsyncSession,
        position: PositionRecord,
        broker: Any,
        entry: dict[str, Any],
        existing: Optional[ProtectiveOrderRecord],
        force: bool = False,
    ) -> None:
        """Idempotently arm ONE protective leg (position lock held).

        * An unchanged live PLACED leg is a no-op (idempotent retry); ``force``
          re-places it anyway.
        * A level change on a live leg cancels + re-places it (the universal
          replace path — adapters declaring ``capability.replace`` can
          short-circuit later; none do yet).
        * A terminal COMPLETE leg is never re-poked.
        * Placement honors the crash-hardening contract: the PENDING_PLACEMENT
          claim is flushed BEFORE broker I/O, and the broker reference is
          persisted in its own commit IMMEDIATELY after acceptance.
        * A reference-less PENDING claim older than the crash window is failed
          closed for manual review instead of blind re-poke (no false duplicate).
        """
        position_id = position.id
        order_type = entry["order_type"]
        trigger_price = entry.get("trigger_price")
        limit_price = entry.get("limit_price")

        row = existing
        if row is not None and row.status == ROW_COMPLETE:
            return
        if row is not None and row.status in (ROW_PLACED, ROW_PENDING):
            unchanged = (
                (row.order_type or "") == order_type
                and _num(row.trigger_price) == _num(trigger_price)
                and _num(row.limit_price) == _num(limit_price)
            )
            if row.status == ROW_PLACED and row.broker_protective_order_id:
                if unchanged and not force:
                    return
                label = "forced re-arm" if unchanged else "levels changed"
                # Levels moved on a live leg → universal replace.
                ok = await self._broker_cancel(db, broker, row, label)
                if not ok:
                    # Fail CLOSED: never re-place while the old broker order
                    # could still be live (that would duplicate protection on
                    # real inventory).  Keep the old reference visible and mark
                    # the row FAILED for manual review / a later retry.
                    row.status = ROW_FAILED
                    row.last_error = (
                        f"replace blocked ({label}): broker cancel failed for "
                        f"{row.broker_protective_order_id} — new level NOT placed "
                        "to avoid duplicate protection"
                    )
                    row.updated_at = _utcnow()
                    await db.commit()
                    return
                row.status = ROW_PENDING
                row.broker_protective_order_id = None
                row.broker_reported_status = None
                row.updated_at = _utcnow()
                await db.commit()
            elif row.broker_protective_order_id:
                return  # in-flight placement the reconcile loop owns
            else:
                age = _row_age(row)
                if age is not None and age < CRASH_WINDOW_STALE_SECONDS:
                    return  # fresh in-flight claim — never duplicate
                row.status = ROW_FAILED
                row.last_error = (
                    "PENDING_PLACEMENT without broker reference exceeded the "
                    "crash window — manual review (never blind re-poke)"
                )
                row.updated_at = _utcnow()
                await db.commit()
                return

        # ── (re)arm: durable claim BEFORE any broker I/O ─────────────────────
        if row is None:
            row = ProtectiveOrderRecord(
                id=str(uuid.uuid4()),
                position_id=position_id,
                user_id=position.user_id,
                broker_account_id=position.broker_account_id,
                leg=entry["leg"], side=entry["side"],
                symbol=position.symbol, quantity=position.quantity,
                order_type=order_type,
                trigger_price=trigger_price, limit_price=limit_price,
                status=ROW_PENDING,
                attempt_count=1,
                created_at=_utcnow(), updated_at=_utcnow(),
            )
            db.add(row)
        else:
            if row.status == ROW_FAILED and row.broker_protective_order_id:
                # A FAILED row with an unresolved broker order (failed
                # replacement / failed teardown).  Do NOT re-arm while the old
                # order could still be live — attempt a bounded cancel first.
                ok = await self._broker_cancel(
                    db, broker, row, "re-arm after failure"
                )
                if not ok:
                    row.last_error = (
                        "re-arm blocked: previous broker protective order "
                        f"{row.broker_protective_order_id} could not be cancelled "
                        "— not re-placing to avoid duplicate protection"
                    )
                    row.updated_at = _utcnow()
                    await db.commit()
                    return
            row.status = ROW_PENDING
            row.order_type = order_type
            row.trigger_price = trigger_price
            row.limit_price = limit_price
            row.side = entry["side"]
            row.quantity = position.quantity
            row.broker_protective_order_id = None
            row.broker_reported_status = None
            row.last_error = None
            row.attempt_count = (row.attempt_count or 0) + 1
            row.updated_at = _utcnow()
        await db.flush()

        order = _order_request_for(position=position, entry=entry)
        response: Optional[dict[str, Any]] = None
        error_message: Optional[str] = None
        try:
            response = await broker.place_order(order)
            logger.info(
                "[Protection] leg %s placed for %s: %s",
                entry["leg"], position_id, response,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - broker rejection is contained
            error_message = f"broker rejected {entry['leg']}: {exc}"
            logger.error("[Protection] %s", error_message)

        if error_message is None and isinstance(response, dict):
            ref = (
                response.get("broker_protective_order_id")
                or response.get("protective_order_id")
                or response.get("broker_order_id")
                or response.get("order_id")
            )
            if ref:
                # Durability boundary: persist the reference in its own commit
                # immediately after acceptance (crash-window hardening).
                row.broker_protective_order_id = str(ref)
                await db.commit()
                row.status = ROW_PLACED
                row.broker_reported_status = (
                    response.get("status") or "OPEN"
                ).upper()
                row.last_error = None
            else:
                error_message = (
                    "broker accepted the order but returned no protective-order "
                    f"reference: {response!r}"
                )
        elif error_message is None:
            error_message = "broker returned no response"

        if error_message is not None:
            row.status = ROW_FAILED
            row.last_error = error_message[:2000]
        row.updated_at = _utcnow()
        await db.commit()
        # -- end of per-leg (re)arm --

    async def _reconcile_one(
        self,
        db: AsyncSession,
        position: PositionRecord,
        authorized_user_id: Optional[str],
        report: dict[str, Any],
    ) -> None:
        """Reconcile ONE position's protective rows inside the caller's session.

        Broker reads ONLY (``get_order_status``) — never a placement, never a
        fabrication.  An unknown/errored status read leaves the row exactly as
        it was.  A broker-reported terminal fill advances the row to COMPLETE
        and the position honestly to STOP_TRIGGERED / TARGET_TRIGGERED.
        """
        position_id = position.id
        if authorized_user_id and position.user_id != authorized_user_id:
            return
        if (position.mode or "PAPER") != "LIVE":
            return
        configured = bool(position.stop_loss_price or position.take_profit_price)

        acc, broker = await self._resolve_broker(db, position)
        rows = list(
            (
                await db.execute(
                    select(ProtectiveOrderRecord).where(
                        ProtectiveOrderRecord.position_id == position_id
                    )
                )
            ).scalars().all()
        )
        if not rows:
            # A LIVE position stuck in PROTECTION_PENDING with NO durable rows
            # is a crash survivor between the PENDING position-state commit and
            # the first per-leg placement commit (the leg rows were flushed but
            # their transaction rolled back).  Re-arm it idempotently — never
            # leave the ledger claiming an in-flight protection that has no
            # durable claim.
            if position.protection_state == PROTECTION_STATE_PENDING:
                report["recovered"].append(position_id)
            return

        # ── reference-less crash survivors: fail closed for manual review ────
        crash_manual = False
        for row in rows:
            if row.status == ROW_PENDING and not row.broker_protective_order_id:
                age = _row_age(row)
                if age is not None and age >= CRASH_WINDOW_STALE_SECONDS:
                    row.status = ROW_FAILED
                    row.last_error = (
                        "PENDING_PLACEMENT without broker reference exceeded "
                        "the crash window — manual review (never blind re-poke)"
                    )
                    row.updated_at = _utcnow()
                    crash_manual = True
                    report["failed_closed"].append(
                        {"position_id": position_id, "error": row.last_error}
                    )

        # ── broker truth for live references ─────────────────────────────────
        if broker is not None:
            for row in rows:
                if row.status not in (ROW_PLACED, ROW_PENDING):
                    continue
                if not row.broker_protective_order_id:
                    continue
                try:
                    broker_resp = await broker.get_order_status(
                        row.broker_protective_order_id
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - read failure keeps state
                    broker_resp = None
                    logger.warning(
                        "[Protection] status read failed for %s: %s",
                        row.broker_protective_order_id, exc,
                    )
                if broker_resp is None:
                    continue
                raw_status = str(
                    broker_resp.get("status")
                    or broker_resp.get("order_status")
                    or ""
                ).upper()
                row.broker_reported_status = raw_status or None
                row.updated_at = _utcnow()
                if raw_status in _FILLED_STATUSES:
                    row.status = ROW_COMPLETE
                    row.last_error = None
                    logger.info(
                        "[Protection] leg %s TRIGGERED for %s",
                        row.leg, position_id,
                    )
                elif raw_status in _GONE_STATUSES:
                    row.status = (
                        ROW_CANCELLED if "CAN" in raw_status else ROW_FAILED
                    )
                    row.last_error = f"broker reported {raw_status}"
                elif raw_status in _LIVE_OPEN_STATUSES and row.status == ROW_PENDING:
                    # Crash between broker acceptance and the PLACED write: the
                    # broker confirms a live open order, so the row is honestly
                    # promoted to PLACED (broker truth — never a guess).
                    row.status = ROW_PLACED
                    row.last_error = None
                    logger.info(
                        "[Protection] leg %s confirmed live at broker for %s",
                        row.leg, position_id,
                    )

        # ── admit freshly FAILED rows for a bounded placement retry ──────────
        if broker is not None:
            for row in rows:
                if row.status != ROW_FAILED or crash_manual:
                    continue
                if row.trigger_price is None:
                    continue
                if row.broker_protective_order_id:
                    # Unresolved broker order — a clean placement rejection has
                    # no reference; a reference means the old order may still be
                    # live.  Re-poking would risk duplicate protection, so these
                    # wait for explicit manual/API recovery.
                    continue
                cap = _protection_capability(broker)
                plan, _failures = _leg_plan(position=position, capability=cap)
                entry = next((e for e in plan if e["leg"] == row.leg), None)
                if entry is None:
                    continue
                try:
                    await self._ensure_one_leg(
                        db, position, broker, entry, existing=row
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - contained
                    report["errors"].append(
                        {"position_id": position_id, "error": str(exc)}
                    )

        rows = list(
            (
                await db.execute(
                    select(ProtectiveOrderRecord).where(
                        ProtectiveOrderRecord.position_id == position_id
                    )
                )
            ).scalars().all()
        )
        final_state = _derive_position_state(rows, position.protection_state)
        if final_state in (
            PROTECTION_STATE_STOP_TRIGGERED, PROTECTION_STATE_TARGET_TRIGGERED,
        ):
            report["triggered"].append(
                {"position_id": position_id, "state": final_state}
            )
        await self._set_position_state(
            db, position, final_state,
            _utcnow() if final_state == PROTECTION_STATE_PROTECTED else None,
            None,
        )
        # A position that just became FAILED (broker rejection / crash survivor
        # that was NOT manual-review gated) gets one fresh placement attempt per
        # pass — bounded, idempotent, never infinite.
        if (
            final_state == PROTECTION_STATE_FAILED
            and configured
            and not crash_manual
        ):
            report["recovered"].append(position_id)
        # -- end of per-position reconcile --


# ── periodic scheduler ────────────────────────────────────────────────────────


class ProtectiveOrderScheduler:
    """Background loop running bounded protective-order reconcile passes.

    Mirrors the ``BrokerOrderReconciliationScheduler`` lifecycle: an initial
    startup pass picks up crash survivors, then a fixed-interval loop.  start /
    stop are wired from the application lifespan; cancellation on shutdown is
    graceful.
    """

    def __init__(
        self,
        engine: Optional[ProtectiveOrderManager] = None,
        interval_seconds: float = PROTECTION_RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        self.engine = engine or ProtectiveOrderManager()
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "[Protection] Scheduler STARTED (interval=%ss)",
            self.interval_seconds,
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[Protection] Scheduler STOPPED.")

    async def _run_loop(self) -> None:
        try:
            await asyncio.sleep(2)  # brief delay for db startup
            if not self._running:
                return
            summary = await self.engine.reconcile_once()
            logger.info("[Protection] startup pass: %s", summary)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - startup is best-effort
            logger.warning("[Protection] startup pass notice: %s", exc)

        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                if not self._running:
                    break
                summary = await self.engine.reconcile_once()
                logger.info("[Protection] pass complete: %s", summary)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - bounded, error-contained
                logger.error("[Protection] pass failure: %s", exc)
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    break


# Shared process-wide instances (mirror ``order_reconciliation``).
protection_engine = ProtectiveOrderManager()
protection_scheduler = ProtectiveOrderScheduler(protection_engine)

__all__ = [
    "PROTECTION_STATE_UNPROTECTED",
    "PROTECTION_STATE_PAPER",
    "PROTECTION_STATE_PENDING",
    "PROTECTION_STATE_PROTECTED",
    "PROTECTION_STATE_FAILED",
    "PROTECTION_STATE_STOP_TRIGGERED",
    "PROTECTION_STATE_TARGET_TRIGGERED",
    "PROTECTION_STATE_CLOSED",
    "LEG_STOP_LOSS",
    "LEG_TAKE_PROFIT",
    "ProtectionOutcome",
    "ProtectiveOrderManager",
    "ProtectiveOrderScheduler",
    "protection_engine",
    "protection_scheduler",
]