"""Database handler for trade audit logging."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal
from app.models.audit import TradeAuditRecord


async def save_audit_log(
    provider: str,
    payload: dict[str, Any],
    execution: dict[str, Any]
) -> None:
    """
    Save a trade execution audit record to the database.
    
    Args:
        provider: Webhook provider name (e.g., "tradetron")
        payload: Validated webhook payload containing signal, symbol, action, quantity, price
        execution: Execution result from place_tradetron_order containing order_id, status, symbol
    """
    async with SessionLocal() as session:
        try:
            audit_record = TradeAuditRecord(
                timestamp=datetime.utcnow(),
                provider=provider,
                symbol=payload.get("symbol", ""),
                action=payload.get("action", ""),
                quantity=int(payload.get("quantity", 0)),
                status=execution.get("status", "UNKNOWN"),
                order_id=execution.get("order_id", ""),
                signal=payload.get("signal"),
                price=payload.get("price"),
            )
            session.add(audit_record)
            await session.commit()
        except Exception as e:
            await session.rollback()
            # Log error but don't raise - audit logging shouldn't break the main flow
            print(f"Failed to save audit log: {e}")