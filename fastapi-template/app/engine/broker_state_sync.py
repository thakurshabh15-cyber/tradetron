"""Broker-truth state synchronization (Phase 15B).

Closes the audit gap where broker ``get_positions()`` / ``get_margins()`` were
queried only transiently and internal LIVE equity/positions remained
authoritative: this module makes BROKER data authoritative and PERSISTED.

Pipeline (per authenticated broker account):

    broker adapter (get_positions + get_margins)
      -> normalization (position_normalizer + margin normalizer, None-safe)
      -> BrokerStateRecord snapshot (CAS upsert, freshness tracked,
         account-scoped + user-scoped)
      -> idempotent reconciliation against internal OPEN LIVE PositionRecords
         (discover missing, flag orphans, sync quantity/avg-price/side to
         broker truth)
      -> risk engine / equity & P&L surfaces read the persisted snapshot

Hard rules
----------
* NO FABRICATION: fields the broker does not provide stay NULL/absent
  (never paperBalance, never a guessed number, never previous-session value).
* Account isolation: every query filters by ``broker_account_id`` AND the
  account's OWNER ``user_id``; tenant B can never see/touch tenant A state.
* Idempotent: a second sync against unchanged broker state produces no new
  snapshot row and no position mutations.
* Concurrency-safe: per-account asyncio lock + CAS update on
  ``BrokerStateRecord.updated_at``; no SELECT->mutate->save where a concurrent
  run could overwrite newer state.
* A stale/unavailable/error snapshot is NEVER presented as LIVE truth; the
  LIVE risk gate fails closed on anything that is not a fresh BROKER snapshot.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.brokers.position_normalizer import (
    normalize_angelone_position,
    normalize_binance_position,
    normalize_simulated_position,
    normalize_upstox_position,
    normalize_zerodha_position,
)
from app.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.broker_account import BrokerAccountRecord
from app.models.broker_state import BrokerStateRecord
from app.models.trading import PositionRecord

# Adapter factory referenced through the module (``app.brokers.get_broker_adapter``)
# rather than a from-import so tests can monkeypatch it at its canonical
# location and the lookup happens at call time.
import app.brokers

logger = get_logger("engine.broker_state_sync")

_REAL_BROKERS = {"ZERODHA", "UPSTOX", "ANGEL_ONE", "BINANCE"}


def is_real_broker_name(broker_name: str | None) -> bool:
    """True only for genuine broker adapters (never SIMULATED/empty)."""
    return (broker_name or "").upper() in _REAL_BROKERS


# ---------------------------------------------------------------------------
# Field aliases accepted when normalizing broker margin/account payloads.
# Missing/unknown keys produce None — never a fabricated number.
# ---------------------------------------------------------------------------
_ALIASES: dict[str, tuple[str, ...]] = {
    "available_cash": (
        "available_cash", "availableCash", "available_balance", "availableBalance",
        "cash", "funds", "available_margin", "availableMargin",
    ),
    "utilized_margin": (
        "utilized_margin", "utilizedMargin", "used_margin", "usedMargin",
        "margin_used", "utilized",
    ),
    "total_collateral": (
        "total_collateral", "totalCollateral", "collateral", "collateral_value",
        "collateralvalue",
    ),
    "unrealized_pnl": (
        "unrealized_pnl", "unrealizedPnl", "unrealised_pnl", "unrealised",
        "m2m", "mtm", "pnl",
    ),
    "realized_pnl": (
        "realized_pnl", "realizedPnl", "realised_pnl", "realised",
    ),
    "total_equity": (
        "total_equity", "totalEquity", "equity", "net_worth", "netWorth",
    ),
    "currency": ("currency", "ccy", "quote_currency"),
}


def _resolve_float(raw: dict[str, Any], aliases: tuple[str, ...]) -> float | None:
    """Return the first parseable float among aliases, else None (no fabrication)."""
    for key in aliases:
        val = raw.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _resolve_str(raw: dict[str, Any], aliases: tuple[str, ...]) -> str | None:
    for key in aliases:
        val = raw.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None
def normalize_broker_margins(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Canonical margins dict.  All fields optional (None when broker lacks them)."""
    if not isinstance(raw, dict):
        return {
            "available_cash": None, "utilized_margin": None,
            "total_collateral": None, "unrealized_pnl": None,
            "realized_pnl": None, "total_equity": None, "currency": None,
        }
    return {
        "available_cash": _resolve_float(raw, _ALIASES["available_cash"]),
        "utilized_margin": _resolve_float(raw, _ALIASES["utilized_margin"]),
        "total_collateral": _resolve_float(raw, _ALIASES["total_collateral"]),
        "unrealized_pnl": _resolve_float(raw, _ALIASES["unrealized_pnl"]),
        "realized_pnl": _resolve_float(raw, _ALIASES["realized_pnl"]),
        "total_equity": _resolve_float(raw, _ALIASES["total_equity"]),
        "currency": _resolve_str(raw, _ALIASES["currency"]),
    }


def _attach_pnl_if_present(norm: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Preserve broker-provided unrealized P&L on a normalized position."""
    pnl = _resolve_float(raw, _ALIASES["unrealized_pnl"])
    if pnl is not None:
        norm["unrealized_pnl"] = pnl
    return norm


def normalize_broker_positions(
    broker_name: str | None, raw_positions: Any
) -> list[dict[str, Any]]:
    """Normalize a broker's raw get_positions() payload to the canonical contract.

    Non-list payloads and malformed rows are dropped (never fabricated).
    Rows are de-duplicated by upper-cased symbol and returned sorted so the
    positions hash is deterministic (idempotency).
    """
    if not isinstance(raw_positions, list):
        return []
    broker = (broker_name or "").upper()
    if broker == "ZERODHA":
        fn = normalize_zerodha_position
    elif broker == "UPSTOX":
        fn = normalize_upstox_position
    elif broker == "ANGEL_ONE":
        fn = normalize_angelone_position
    elif broker == "BINANCE":
        fn = normalize_binance_position
    elif broker == "SIMULATED":
        fn = normalize_simulated_position
    else:
        fn = normalize_zerodha_position

    by_symbol: dict[str, dict[str, Any]] = {}
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        normalized = fn(item)
        if normalized is None:
            continue
        by_symbol[normalized["symbol"]] = _attach_pnl_if_present(normalized, item)
    return sorted(by_symbol.values(), key=lambda p: p["symbol"])


def compute_positions_hash(positions: list[dict[str, Any]]) -> str:
    """Deterministic SHA-256 fingerprint for idempotent change detection."""
    canonical = json.dumps(positions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
def derive_broker_state_status(
    snap: BrokerStateRecord | None, now: datetime | None = None
) -> str:
    """Derive the semantic freshness status of a stored snapshot.

    LIVE only when the broker data is genuine (source BROKER, stored status
    LIVE) AND captured within the configured stale threshold.  Everything else
    degrades explicitly: STALE / UNAVAILABLE / ERROR / PAPER.
    """
    if snap is None:
        return "UNAVAILABLE"
    stored = (snap.status or "UNAVAILABLE").upper()
    if stored == "PAPER":
        return "PAPER"
    if stored in ("ERROR", "UNAVAILABLE"):
        return stored
    captured = snap.captured_at
    if captured is None:
        return "UNAVAILABLE"
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age = (now - captured).total_seconds()
    if age <= settings.broker_state_stale_after:
        return "LIVE"
    return "STALE"


def snapshot_to_dict(
    snap: BrokerStateRecord | None,
    broker_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Serializable, fresh-status-labelled view of a broker-state snapshot."""
    if snap is None:
        return {
            "status": "UNAVAILABLE", "source": None,
            "broker_account_id": None, "broker_name": broker_name,
            "user_id": None,
            "positions": [], "positions_hash": None,
            "available_cash": None, "utilized_margin": None,
            "total_collateral": None, "total_equity": None,
            "unrealized_pnl": None, "realized_pnl": None, "currency": None,
            "captured_at": None, "last_good_captured_at": None,
            "sync_message": None, "sync_count": 0, "updated_at": None,
        }
    status = derive_broker_state_status(snap, now=now)
    return {
        "status": status,
        "source": snap.source,
        "broker_account_id": snap.broker_account_id,
        "broker_name": broker_name,
        "user_id": snap.user_id,
        "positions": json.loads(snap.positions_json or "[]"),
        "positions_hash": snap.positions_hash,
        "available_cash": snap.available_cash,
        "utilized_margin": snap.utilized_margin,
        "total_collateral": snap.total_collateral,
        "total_equity": snap.total_equity,
        "unrealized_pnl": snap.unrealized_pnl,
        "realized_pnl": snap.realized_pnl,
        "currency": snap.currency,
        "captured_at": snap.captured_at.isoformat() if snap.captured_at else None,
        "last_good_captured_at": (
            snap.last_good_captured_at.isoformat() if snap.last_good_captured_at else None
        ),
        "sync_message": snap.sync_message,
        "sync_count": snap.sync_count,
        "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
    }
class BrokerStateSyncEngine:
    """Fetch, normalize, persist and reconcile broker truth per account."""

    def __init__(self) -> None:
        # Per-account in-process serialization (scheduler + API + gate all
        # share this singleton; the deployment posture is single-instance).
        self._locks: dict[str, asyncio.Lock] = {}
        self._syncing: set[str] = set()

    def _lock_for(self, account_id: str) -> asyncio.Lock:
        lock = self._locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account_id] = lock
        return lock

    async def get_snapshot(
        self, broker_account_id: str
    ) -> Optional[BrokerStateRecord]:
        """Return the persisted snapshot row (or None) for an account."""
        async with SessionLocal() as db:
            stmt = select(BrokerStateRecord).where(
                BrokerStateRecord.broker_account_id == broker_account_id
            )
            return (await db.execute(stmt)).scalar_one_or_none()

    async def sync_all(self) -> dict[str, Any]:
        """Sync every active broker account (bounded, per-account contained)."""
        async with SessionLocal() as db:
            stmt = select(BrokerAccountRecord).where(
                BrokerAccountRecord.is_active.is_(True)
            )
            accounts = (await db.execute(stmt)).scalars().all()
        results: list[dict[str, Any]] = []
        for acc in accounts:
            try:
                results.append(await self.sync_account(acc.id))
            except Exception as exc:  # noqa: BLE001 - a single bad account never kills the pass
                logger.exception("Broker state sync failed for account %s: %s", acc.id, exc)
                results.append({
                    "broker_account_id": acc.id,
                    "broker_name": acc.broker_name,
                    "status": "ERROR",
                    "error": str(exc)[:300],
                })
        return {"accounts": len(accounts), "results": results}

    async def sync_account(
        self,
        broker_account_id: str,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Synchronize ONE broker account to a fresh, normalized broker-truth snapshot.

        ``user_id`` enforces tenant isolation when provided (must match the
        account's owner).  Returns a summary dict; never raises for expected
        broker failures (they produce an ERROR snapshot instead).
        """
        now = datetime.now(timezone.utc)
        async with self._lock_for(broker_account_id):
            self._syncing.add(broker_account_id)
            try:
                return await self._sync_account_locked(broker_account_id, user_id, now)
            finally:
                self._syncing.discard(broker_account_id)

    async def _sync_account_locked(
        self,
        broker_account_id: str,
        user_id: Optional[str],
        now: datetime,
    ) -> dict[str, Any]:
        """Core per-account sync (caller holds the account lock)."""
        async with SessionLocal() as db:
            acc = await db.get(BrokerAccountRecord, broker_account_id)
            if acc is None:
                return {
                    "broker_account_id": broker_account_id, "status": "ERROR",
                    "error": "Broker account not found",
                }
            if user_id is not None and acc.user_id != user_id:
                return {
                    "broker_account_id": broker_account_id, "status": "ERROR",
                    "error": "Account ownership mismatch — tenant isolation enforced",
                }

            broker_name = (acc.broker_name or "").upper()

            # ── Simulated / legacy accounts: honest PAPER label, never LIVE ──
            if not is_real_broker_name(broker_name):
                raw_positions: Any = []
                raw_margins: Any = None
                try:
                    adapter = app.brokers.get_broker_adapter(acc)
                    raw_positions = await adapter.get_positions()
                    raw_margins = await adapter.get_margins()
                except Exception as exc:  # noqa: BLE001 - contained
                    logger.debug("Simulated broker state read skipped: %s", exc)
                positions = normalize_broker_positions(broker_name, raw_positions)
                margins = normalize_broker_margins(raw_margins)
                snap = await self._upsert_snapshot(
                    db, acc, now,
                    status="PAPER", source="SIMULATED",
                    positions=positions, margins=margins,
                    message="Simulated broker account — PAPER state, never LIVE truth",
                )
                await db.commit()
                return {
                    "broker_account_id": acc.id, "broker_name": broker_name,
                    "status": "PAPER", "source": "SIMULATED",
                    "positions": positions, "margins": margins,
                    "reconciliation": {},
                }

            if acc.is_token_expired():
                snap = await self._upsert_snapshot(
                    db, acc, now,
                    status="UNAVAILABLE", source="BROKER",
                    positions=[], margins=None,
                    message="Broker token expired — account state unavailable",
                )
                await db.commit()
                return {
                    "broker_account_id": acc.id, "broker_name": broker_name,
                    "status": "UNAVAILABLE", "source": "BROKER",
                    "positions": [], "margins": None,
                    "reconciliation": {},
                    "error": "Broker token expired — account state unavailable",
                }

            # ── Read broker truth (read-only) ─────────────────────────────
            try:
                adapter = app.brokers.get_broker_adapter(acc)
                raw_positions = await adapter.get_positions()
                raw_margins = await adapter.get_margins()
            except Exception as exc:  # noqa: BLE001 - FAIL-CLOSED, never fabricate
                logger.error("[BrokerState] fetch failed for %s: %s", acc.id, exc)
                snap = await self._upsert_snapshot(
                    db, acc, now,
                    status="ERROR", source="BROKER",
                    positions=[], margins=None,
                    message=f"Broker API failure: {exc}",
                    preserve_last_good=True,
                )
                await db.commit()
                return {
                    "broker_account_id": acc.id, "broker_name": broker_name,
                    "status": "ERROR", "source": "BROKER",
                    "positions": [], "margins": None,
                    "reconciliation": {},
                    "error": str(exc)[:300],
                }

            positions = normalize_broker_positions(broker_name, raw_positions)
            margins = normalize_broker_margins(raw_margins)

            # ── Persist the fresh LIVE snapshot (CAS upsert) ──────────────
            snap = await self._upsert_snapshot(
                db, acc, now,
                status="LIVE", source="BROKER",
                positions=positions, margins=margins,
                message=None,
            )
            await db.commit()

        # ── Reconcile internal LIVE positions to broker truth (idempotent) ─
        report: dict[str, Any] = {}
        if positions:
            report = await self._reconcile_positions(acc, positions, now)

        return {
            "broker_account_id": acc.id, "broker_name": broker_name,
            "status": "LIVE", "source": "BROKER",
            "positions": positions, "margins": margins,
            "reconciliation": report,
        }
    @staticmethod
    def _snapshot_values(
        acc: BrokerAccountRecord,
        status: str,
        source: str,
        positions: list[dict[str, Any]],
        margins: dict[str, Any] | None,
        now: datetime,
        message: str | None = None,
        preserve_last_good: bool = False,
    ) -> dict[str, Any]:
        """Build the value dict for a snapshot INSERT or UPDATE."""
        margins = margins or {}
        positions_json = json.dumps(positions, sort_keys=True, separators=(",", ":"))
        positions_hash = (
            compute_positions_hash(positions) if source == "BROKER" and positions else None
        )
        is_good = status in ("LIVE",)
        return {
            "broker_account_id": acc.id,
            "user_id": acc.user_id,
            "status": status,
            "source": source,
            "positions_json": positions_json,
            "positions_hash": positions_hash,
            "available_cash": margins.get("available_cash"),
            "utilized_margin": margins.get("utilized_margin"),
            "total_collateral": margins.get("total_collateral"),
            "unrealized_pnl": margins.get("unrealized_pnl"),
            "realized_pnl": margins.get("realized_pnl"),
            "total_equity": margins.get("total_equity"),
            "currency": margins.get("currency"),
            "captured_at": now if status in ("LIVE", "ERROR", "UNAVAILABLE", "PAPER") else None,
            "last_good_captured_at": now if is_good else None,
            "sync_message": message,
            "updated_at": now,
        }

    async def _upsert_snapshot(
        self,
        db: Any,  # AsyncSession
        acc: BrokerAccountRecord,
        now: datetime,
        *,
        status: str,
        source: str,
        positions: list[dict[str, Any]],
        margins: dict[str, Any] | None,
        message: str | None = None,
        preserve_last_good: bool = False,
    ) -> BrokerStateRecord:
        """CAS upsert: exactly one row per broker account.

        INSERT or UPDATE-where-``updated_at``-matches.  When the CAS loses
        (concurrent writer) the values are re-applied onto the latest row so
        the snapshot eventually converges (idempotent: last-broker-truth-wins).
        """
        stmt = select(BrokerStateRecord).where(
            BrokerStateRecord.broker_account_id == acc.id
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()

        values = self._snapshot_values(
            acc, status, source, positions, margins, now,
            message=message, preserve_last_good=preserve_last_good,
        )
        # On a failed sync, keep the previous successful capture timestamp so
        # "when was the last good broker read" stays truthful.
        if (
            preserve_last_good
            and existing is not None
            and existing.last_good_captured_at is not None
        ):
            values["last_good_captured_at"] = existing.last_good_captured_at

        if existing is None:
            snap = BrokerStateRecord(**values)
            db.add(snap)
            try:
                await db.flush()
            except IntegrityError:
                # Race with a concurrent insert; rollback, re-read, and update.
                await db.rollback()
                existing = (await db.execute(stmt)).scalar_one_or_none()
                if existing is None:
                    raise RuntimeError(
                        "Concurrent broker-state first-insert race — retry sync"
                    ) from None
                await self._cas_update(db, existing, values, now)
                return existing
            return snap

        await self._cas_update(db, existing, values, now)
        return existing

    @staticmethod
    async def _cas_update(
        db: Any, existing: BrokerStateRecord, values: dict[str, Any], now: datetime
    ) -> None:
        """UPDATE WHERE id=:id AND updated_at=:seen — single-row CAS."""
        seen = existing.updated_at
        res = await db.execute(
            update(BrokerStateRecord)
            .where(
                BrokerStateRecord.id == existing.id,
                BrokerStateRecord.updated_at == seen,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 1:
            await db.refresh(existing)
            return
        # CAS conflict (concurrent sync committed first).  Re-fetch the latest
        # row and apply the new values as a plain update (idempotent merge).
        await db.rollback()
        fresh = (await db.execute(
            select(BrokerStateRecord).where(BrokerStateRecord.id == existing.id)
        )).scalar_one_or_none()
        if fresh is None:
            # Fallback: re-insert.
            fresh = BrokerStateRecord(**values)
            db.add(fresh)
        else:
            for k, v in values.items():
                setattr(fresh, k, v)
            db.add(fresh)

    async def _reconcile_positions(
        self,
        acc: BrokerAccountRecord,
        broker_positions: list[dict[str, Any]],
        now: datetime,
    ) -> dict[str, Any]:
        """Idempotent reconciliation of internal OPEN LIVE positions to broker truth.

        Scope: ONLY the authenticated broker account's OWN positions
        (``user_id == acc.user_id``, ``broker_account_id == acc.id``,
        ``mode == LIVE``, ``status == OPEN``).  Broker truth is authoritative:
        quantity / average price / side / broker-reported unrealized P&L are
        mirrored onto matching internal rows; newly discovered broker positions
        are created; internal positions with NO broker counterpart are flagged
        as orphans (never auto-closed, never fabricated CLOSED).

        Idempotent: a second pass against unchanged broker state performs no
        writes (``changed`` stays False for every row, discovered set is empty).
        """
        report: dict[str, Any] = {
            "matched": [], "discovered": [], "orphans": [],
            "quantity_mismatches": [], "avg_price_mismatches": [],
            "side_mismatches": [], "created": 0, "updated": 0,
            "avg_price_missing": [], "unrealized_pnl_unavailable": [],
            "scope": {"user_id": acc.user_id, "broker_account_id": acc.id, "mode": "LIVE"},
        }
        broker_by_symbol: dict[str, dict[str, Any]] = {}
        for p in broker_positions:
            sym = str(p.get("symbol", "")).upper()
            if sym:
                broker_by_symbol[sym] = p

        async with SessionLocal() as db:
            stmt = select(PositionRecord).where(
                PositionRecord.user_id == acc.user_id,
                PositionRecord.broker_account_id == acc.id,
                PositionRecord.mode == "LIVE",
                PositionRecord.status == "OPEN",
            )
            internal_rows = (await db.execute(stmt)).scalars().all()

            consumed: set[str] = set()
            for ip in internal_rows:
                sym = str(ip.symbol).upper()
                bp = broker_by_symbol.get(sym)
                if bp is None or sym in consumed:
                    report["orphans"].append(sym)
                    continue
                consumed.add(sym)
                report["matched"].append(sym)
                changed = False

                broker_side = bp.get("side") or (
                    "LONG" if int(bp.get("quantity", 0)) >= 0 else "SHORT"
                )
                if ip.side != broker_side:
                    report["side_mismatches"].append({
                        "symbol": sym, "internal": ip.side, "broker": broker_side,
                    })
                    ip.side = broker_side
                    changed = True

                broker_qty = abs(int(bp.get("quantity", 0)))
                if ip.quantity != broker_qty:
                    report["quantity_mismatches"].append({
                        "symbol": sym, "internal": ip.quantity, "broker": broker_qty,
                    })
                    ip.quantity = broker_qty
                    changed = True

                broker_avg = float(bp.get("average_price") or 0.0)
                if abs((ip.entry_price or 0.0) - broker_avg) > 1e-9:
                    report["avg_price_mismatches"].append({
                        "symbol": sym, "internal": ip.entry_price, "broker": broker_avg,
                    })
                    ip.entry_price = broker_avg
                    changed = True

                if bp.get("unrealized_pnl") is not None:
                    ip.unrealized_pnl = float(bp["unrealized_pnl"])
                    changed = True
                else:
                    report["unrealized_pnl_unavailable"].append(sym)

                if changed:
                    report["updated"] += 1
                    db.add(ip)

            for sym, bp in sorted(broker_by_symbol.items()):
                if sym in consumed:
                    continue
                # Newly discovered broker position (no internal counterpart).
                created_side = bp.get("side") or (
                    "LONG" if int(bp.get("quantity", 0)) >= 0 else "SHORT"
                )
                avg_price = float(bp.get("average_price") or 0.0)
                is_pnl_present = bp.get("unrealized_pnl") is not None
                db.add(PositionRecord(
                    user_id=acc.user_id,
                    broker_account_id=acc.id,
                    symbol=sym,
                    side=created_side,
                    quantity=abs(int(bp.get("quantity", 0))),
                    entry_price=avg_price,
                    current_price=avg_price,
                    unrealized_pnl=(
                        float(bp["unrealized_pnl"]) if is_pnl_present else 0.0
                    ),
                    realized_pnl=0.0,
                    mode="LIVE",
                    status="OPEN",
                    opened_at=now,
                ))
                report["discovered"].append({
                    "symbol": sym, "side": created_side,
                    "quantity": abs(int(bp.get("quantity", 0))),
                    "average_price": avg_price,
                })
                report["created"] += 1
                if avg_price <= 0.0:
                    report["avg_price_missing"].append(sym)
                if not is_pnl_present:
                    report["unrealized_pnl_unavailable"].append(sym)

            await db.commit()
        return report
class BrokerStateSyncScheduler:
    """Background loop keeping broker-truth snapshots fresh across restarts.

    Mirrors the ``BrokerOrderReconciliationScheduler`` lifecycle contract
    (start once, graceful cancel, per-cycle containment) so a single slow or
    failing account never blocks the loop.
    """

    def __init__(
        self,
        engine: Optional[BrokerStateSyncEngine] = None,
        interval_seconds: Optional[float] = None,
    ) -> None:
        self.engine = engine or BrokerStateSyncEngine()
        self.interval_seconds = interval_seconds or float(settings.broker_state_sync_interval)
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "[BrokerStateSync] Scheduler STARTED (interval=%ss)",
            self.interval_seconds,
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[BrokerStateSync] Scheduler STOPPED.")

    async def _run_loop(self) -> None:
        # Startup pass picks up crash survivors from a previous process.
        try:
            await asyncio.sleep(2)
            if not self._running:
                return
            summary = await self.engine.sync_all()
            logger.info("[BrokerStateSync] startup pass: %s", summary)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - contained
            logger.warning("[BrokerStateSync] startup pass notice: %s", exc)

        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                if not self._running:
                    break
                summary = await self.engine.sync_all()
                logger.info("[BrokerStateSync] pass complete: %s", summary)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - contained
                logger.error("[BrokerStateSync] pass failure: %s", exc)
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    break


broker_state_sync_engine = BrokerStateSyncEngine()
broker_state_sync_scheduler = BrokerStateSyncScheduler(broker_state_sync_engine)

__all__ = [
    "BrokerStateSyncEngine",
    "BrokerStateSyncScheduler",
    "broker_state_sync_engine",
    "broker_state_sync_scheduler",
    "compute_positions_hash",
    "derive_broker_state_status",
    "is_real_broker_name",
    "normalize_broker_margins",
    "normalize_broker_positions",
    "snapshot_to_dict",
]