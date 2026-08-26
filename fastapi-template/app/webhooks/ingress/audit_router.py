"""Audit logs router - provides access to trade audit history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List

from app.db.session import get_db, engine, Base
from app.models.audit import TradeAuditRecord
from app.core.logging import get_logger

logger = get_logger("webhook.audit")

router = APIRouter(prefix="/webhooks/audit", tags=["audit"])


@router.get(
    "/logs",
    summary="Get trade audit logs",
    description="Retrieve trade execution audit logs with optional filtering by provider",
)
async def get_audit_logs(
    limit: int = Query(50, ge=1, le=500, description="Maximum number of records to return"),
    provider: Optional[str] = Query(None, description="Filter by provider name (e.g., tradethrone)"),
    session: AsyncSession = Depends(get_db),
) -> List[dict]:
    """
    Fetch trade audit logs from the database.
    
    Args:
        limit: Maximum number of records (default: 50, max: 500)
        provider: Optional filter by provider name
    
    Returns:
        List of audit log entries ordered by timestamp descending
    """
    # Ensure tables exist on the current engine instance before querying
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    try:
        stmt = select(TradeAuditRecord).order_by(desc(TradeAuditRecord.timestamp)).limit(limit)
        
        if provider:
            stmt = stmt.where(TradeAuditRecord.provider == provider.lower())
        
        result = await session.execute(stmt)
        records = result.scalars().all()
        
        audit_logs = []
        for record in records:
            audit_logs.append({
                "timestamp": record.timestamp.isoformat() if record.timestamp else None,
                "provider": record.provider,
                "symbol": record.symbol,
                "action": record.action,
                "quantity": record.quantity,
                "status": record.status,
                "order_id": record.order_id,
                "price": record.price,
            })
        
        logger.debug("Fetched %d audit log records", len(audit_logs))
        return audit_logs
    
    except OperationalError as exc:
        # Table doesn't exist or other operational error - return empty list gracefully
        logger.warning("Database operational error fetching audit logs (table may not exist yet): %s", exc)
        return []
    except Exception as exc:
        # Any other unexpected error - log and return empty list to avoid 500
        logger.error("Unexpected error fetching audit logs: %s", exc, exc_info=True)
        return []