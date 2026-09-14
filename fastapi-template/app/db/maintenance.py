"""Small, safety-only database maintenance helpers.

Nothing here deletes trading / audit truth: only provably-dead operational
rows (e.g. revoked refresh tokens whose JWT ``exp`` has already passed) are
purged, so storage use stays bounded on long-running deployments without any
risk of re-enabling a spent credential.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import RevokedTokenRecord


async def prune_expired_revoked_tokens(
    db: AsyncSession, *, now: datetime | None = None
) -> int:
    """Delete revoked-token rows whose ``expires_at`` is already in the past.

    Correctness argument: a refresh token with an expired JWT ``exp`` can never
    authenticate again (the expiry check in ``refresh_token`` rejects it), so
    removing its revocation marker cannot unlock anything.  This bounds a table
    that otherwise grows with every logout and every refresh-token rotation for
    the lifetime of the process.

    Returns the number of rows deleted.  Fail-open-by-design: exceptions are
    swallowed by the caller so security-critical flows (logout/rotation) never
    break because of a maintenance failure.
    """
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        delete(RevokedTokenRecord).where(RevokedTokenRecord.expires_at < now)
    )
    if result.rowcount:
        await db.commit()
    return int(result.rowcount or 0)