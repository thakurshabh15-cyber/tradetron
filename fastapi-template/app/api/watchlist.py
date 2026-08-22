"""Watchlist and Price Alert CRUD API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_db
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
async def get_watchlist(db: AsyncSession = Depends(get_db)):
    """Retrieve all symbols in the active watchlist."""
    stmt = select(WatchlistRecord).order_by(WatchlistRecord.created_at.desc())
    res = await db.execute(stmt)
    records = res.scalars().all()

    # If DB is empty, initialize default seed records
    if not records:
        for sym in _DEFAULT_SEEDS:
            rec = WatchlistRecord(symbol=sym, notes="Core watch asset")
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
):
    """Add a symbol to the active watchlist."""
    sym_clean = req.symbol.strip().upper()

    # Check for duplicates
    stmt = select(WatchlistRecord).where(WatchlistRecord.symbol == sym_clean)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{sym_clean} is already in the watchlist",
        )

    record = WatchlistRecord(symbol=sym_clean, notes=req.notes)
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
):
    """Remove a symbol from the watchlist by symbol name or ID."""
    clean_target = symbol_or_id.strip()

    stmt = select(WatchlistRecord).where(
        (WatchlistRecord.id == clean_target) | (WatchlistRecord.symbol == clean_target.upper())
    )
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
async def list_alerts(db: AsyncSession = Depends(get_db)):
    """List all registered price alerts."""
    stmt = select(PriceAlertRecord).order_by(PriceAlertRecord.created_at.desc())
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
):
    """Create a new price alert (e.g. NVDA CROSSES ABOVE $135.00)."""
    sym_clean = req.symbol.strip().upper()
    cond_clean = req.condition.strip().upper()
    if cond_clean not in ("ABOVE", "BELOW"):
        raise HTTPException(status_code=400, detail="Condition must be 'ABOVE' or 'BELOW'")

    alert = PriceAlertRecord(
        symbol=sym_clean,
        condition=cond_clean,
        target_price=req.target_price,
        is_active=True,
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
):
    """Delete a price alert."""
    stmt = select(PriceAlertRecord).where(PriceAlertRecord.id == alert_id)
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
):
    """Toggle price alert active state."""
    stmt = select(PriceAlertRecord).where(PriceAlertRecord.id == alert_id)
    res = await db.execute(stmt)
    alert = res.scalar_one_or_none()

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    alert.is_active = not alert.is_active
    await db.commit()
    await db.refresh(alert)
    return {"success": True, "alert_id": alert.id, "is_active": alert.is_active}
