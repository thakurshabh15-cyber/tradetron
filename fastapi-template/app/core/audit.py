"""Audit logger utility to record immutable audit events without blocking primary execution."""

from __future__ import annotations

import json
from typing import Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.audit import AuditLogRecord

logger = get_logger("audit.trail")


async def log_audit_event(
    db: AsyncSession,
    action: str,
    resource_type: str,
    user_id: Optional[str] = None,
    resource_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    status: str = "SUCCESS",
    details: Optional[dict[str, Any]] = None,
) -> AuditLogRecord:
    """Record an audit trail event directly into the audit_logs table."""
    details_str = json.dumps(details or {}, separators=(",", ":"))
    record = AuditLogRecord(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else None,
        ip_address=ip_address,
        status=status,
        details_json=details_str,
    )
    db.add(record)
    try:
        await db.commit()
        await db.refresh(record)
    except Exception as exc:
        logger.error("Failed to commit audit record: %s", exc)
        await db.rollback()

    logger.info("AUDIT: [%s] by %s on %s:%s -> %s", action, user_id or "ANONYMOUS", resource_type, resource_id, status)
    return record
