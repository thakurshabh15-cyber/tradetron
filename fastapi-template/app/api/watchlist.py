"""Watchlist and Price Alert CRUD API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_optional_current_user
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.user import UserRecord
from app.models.watchlist import PriceAlertRecord, WatchlistRecord

logger = get_logger("api.watchlist")
router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddWatchlistRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=30)
    notes: str | None = None


class CreateAlertRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=30)
    condition: str = Field("ABOVE", description="ABOVE or BELOW")
    target_price: float = Field(..., gt=0)


# Default seeded watchlists for immediate usability
_DEFAULT_SEEDS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]


@router.get("")
async def get_watchlist(
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_optional_current_user),
):
    """Retrieve the caller's OWN watchlist symbols.

    Tenant isolation policy (mirrors the dashboard/summary split):

    * **Authenticated** — strictly scoped to the server-derived ``user.id``
      from the bearer token; another tenant's rows and the anonymous demo
      rows are never returned.
    * **Anonymous** — returns only the ``user_id IS NULL`` demo rows (the
      seeded guest watchlist used by the landing/watchlist page). A
      client-supplied ``user_id`` is never consulted.
    """
    stmt = select(WatchlistRecord).order_by(WatchlistRecord.created_at.desc())
    if user is not None:
        stmt = stmt.where(WatchlistRecord.user_id == user.id)
    else:
        stmt = stmt.where(WatchlistRecord.user_id.is_(None))
    res = await db.execute(stmt)
    records = res.scalars().all()

    # Seed the demo dataset only when no demo rows exist yet (anonymous view).
    if user is None and not records:
        for sym in _DEFAULT_SEEDS:
            rec = WatchlistRecord(symbol=sym, notes="Core watch asset", user_id=None)
            db.add(rec)
        await db.commit()
        res = await db.execute(stmt)
        records = res.scalars().all()

    return [
        {
            "id": r.id,
            "symbol": r.symbol,
            "notes": r.notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in records
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_to_watchlist(
    req: AddWatchlistRequest,
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_optional_current_user),
):
    """Add a symbol to the caller's own watchlist namespace.

    Authenticated callers get the row stamped with the server-derived
    ``user.id``; anonymous callers operate on the ``user_id IS NULL`` demo
    namespace only. Duplicate checks are namespace-scoped, so tenants never
    collide with one another or with the demo dataset.
    """
    sym_clean = req.symbol.strip().upper()

    # Check for duplicates within the caller's own namespace only.
    dup_stmt = select(WatchlistRecord).where(WatchlistRecord.symbol == sym_clean)
    if user is None:
        dup_stmt = dup_stmt.where(WatchlistRecord.user_id.is_(None))
    else:
        dup_stmt = dup_stmt.where(WatchlistRecord.user_id == user.id)
    dup_res = await db.execute(dup_stmt)
    if dup_res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{sym_clean} is already in the watchlist",
        )

    record = WatchlistRecord(symbol=sym_clean, notes=req.notes, user_id=user.id if user else None)
    db.add(record)
    await db.commit()
    await db.refresh(record)

    logger.info("Added %s to watchlist (%s)", record.symbol, record.id)
    return {
        "id": record.id,
        "symbol": record.symbol,
        "notes": record.notes,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


@router.delete("/{symbol_or_id}")
async def remove_from_watchlist(
    symbol_or_id: str,
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_optional_current_user),
):
    """Remove a symbol from the caller's own watchlist namespace.

    The target row must belong to the caller's namespace: authenticated
    callers can only delete rows stamped with their server-derived
    ``user.id``; anonymous callers can only delete ``user_id IS NULL`` demo
    rows. Anything else is reported as not-found so the endpoint never
    reveals (or mutates) another tenant's data.
    """
    clean_target = symbol_or_id.strip()

    stmt = select(WatchlistRecord).where(
        (WatchlistRecord.id == clean_target) | (WatchlistRecord.symbol == clean_target.upper())
    )
    if user is None:
        stmt = stmt.where(WatchlistRecord.user_id.is_(None))
    else:
        stmt = stmt.where(WatchlistRecord.user_id == user.id)
    res = await db.execute(stmt)
    record = res.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=404, detail="Watchlist item not found")

    await db.delete(record)
    await db.commit()
    logger.info("Removed %s from watchlist", clean_target)
    return {"success": True, "deleted": record.symbol}


# ── PRICE ALERTS ─────────────────────────────────────────────────────────────
@router.get("/alerts/list")
async def list_alerts(
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_optional_current_user),
):
    """List the caller's OWN price alerts.

    Authenticated — strictly tenant-scoped to the server-derived ``user.id``.
    Anonymous — only the ``user_id IS NULL`` demo alerts (guest visitors).
    """
    stmt = select(PriceAlertRecord).order_by(PriceAlertRecord.created_at.desc())
    if user is None:
        stmt = stmt.where(PriceAlertRecord.user_id.is_(None))
    else:
        stmt = stmt.where(PriceAlertRecord.user_id == user.id)
    res = await db.execute(stmt)
    alerts = res.scalars().all()

    return [
        {
            "id": a.id,
            "symbol": a.symbol,
            "condition": a.condition,
            "target_price": a.target_price,
            "is_active": a.is_active,
            "is_triggered": a.is_triggered,
            "triggered_at": a.triggered_at.isoformat() if a.triggered_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in alerts
    ]


@router.post("/alerts", status_code=status.HTTP_201_CREATED)
async def create_price_alert(
    req: CreateAlertRequest,
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_optional_current_user),
):
    """Create a new price alert (e.g. NVDA CROSSES ABOVE $135.00).

    Authenticated callers get the alert stamped with the server-derived
    ``user.id``; anonymous callers create ``user_id IS NULL`` demo alerts.
    """
    sym_clean = req.symbol.strip().upper()
    cond_clean = req.condition.strip().upper()
    if cond_clean not in ("ABOVE", "BELOW"):
        raise HTTPException(status_code=400, detail="Condition must be 'ABOVE' or 'BELOW'")

    alert = PriceAlertRecord(
        symbol=sym_clean,
        condition=cond_clean,
        target_price=req.target_price,
        is_active=True,
        user_id=user.id if user else None,
    )
    db.add(alert)
    await db.commit()
    await db.refresh(alert)

    logger.info(
        "Created price alert: %s %s %.2f (%s)",
        alert.symbol,
        alert.condition,
        alert.target_price,
        alert.id,
    )
    return {
        "id": alert.id,
        "symbol": alert.symbol,
        "condition": alert.condition,
        "target_price": alert.target_price,
        "is_active": alert.is_active,
        "is_triggered": alert.is_triggered,
    }


@router.delete("/alerts/{alert_id}")
async def delete_price_alert(
    alert_id: str,
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_optional_current_user),
):
    """Delete the caller's OWN price alert.

    The target alert must belong to the caller's namespace (server-derived
    ``user.id`` for authenticated callers, ``user_id IS NULL`` demo alerts for
    anonymous callers); anything else is reported as not-found.
    """
    stmt = select(PriceAlertRecord).where(PriceAlertRecord.id == alert_id)
    if user is None:
        stmt = stmt.where(PriceAlertRecord.user_id.is_(None))
    else:
        stmt = stmt.where(PriceAlertRecord.user_id == user.id)
    res = await db.execute(stmt)
    alert = res.scalar_one_or_none()

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    await db.delete(alert)
    await db.commit()
    return {"success": True, "deleted_id": alert_id}


@router.patch("/alerts/{alert_id}/toggle")
async def toggle_price_alert(
    alert_id: str,
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_optional_current_user),
):
    """Toggle the caller's OWN price alert active state.

    The target alert must belong to the caller's namespace (server-derived
    ``user.id`` for authenticated callers, ``user_id IS NULL`` demo alerts for
    anonymous callers); anything else is reported as not-found.
    """
    stmt = select(PriceAlertRecord).where(PriceAlertRecord.id == alert_id)
    if user is None:
        stmt = stmt.where(PriceAlertRecord.user_id.is_(None))
    else:
        stmt = stmt.where(PriceAlertRecord.user_id == user.id)
    res = await db.execute(stmt)
    alert = res.scalar_one_or_none()

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    alert.is_active = not alert.is_active
    await db.commit()
    await db.refresh(alert)
    return {"success": True, "alert_id": alert.id, "is_active": alert.is_active}
