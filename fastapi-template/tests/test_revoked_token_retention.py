"""Regression: revoked refresh-token table stays storage-bounded.

Every logout and refresh-token rotation inserts a revocation row; without
retention they accumulate forever.  Expired revocations are provably dead (the
same JWT ``exp`` the refresh endpoint already rejects), so pruning them is
purely a storage win and can never re-enable a spent credential.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.maintenance import prune_expired_revoked_tokens
from app.models.user import RevokedTokenRecord


@pytest.mark.asyncio
async def test_prune_removes_only_expired_rows():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(RevokedTokenRecord.metadata.create_all)

    now = datetime.now(timezone.utc)
    async with factory() as db:
        db.add(
            RevokedTokenRecord(
                token_hash="expired-hash-1",
                user_id="u1",
                expires_at=now - timedelta(seconds=30),
            )
        )
        db.add(
            RevokedTokenRecord(
                token_hash="expired-hash-2",
                user_id="u2",
                expires_at=now - timedelta(hours=1),
            )
        )
        db.add(
            RevokedTokenRecord(
                token_hash="still-valid-3",
                user_id="u3",
                expires_at=now + timedelta(days=1),
            )
        )
        await db.commit()

    async with factory() as db:
        deleted = await prune_expired_revoked_tokens(db)
        assert deleted == 2

    async with factory() as db:
        remaining = (await db.execute(select(func.count()).select_from(RevokedTokenRecord))).scalar()
        assert remaining == 1
        leftover = (
            await db.execute(
                select(RevokedTokenRecord).where(RevokedTokenRecord.token_hash == "still-valid-3")
            )
        ).scalar_one_or_none()
        assert leftover is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_prune_is_noop_when_no_grants_have_expired():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(RevokedTokenRecord.metadata.create_all)

    now = datetime.now(timezone.utc)
    async with factory() as db:
        db.add(
            RevokedTokenRecord(
                token_hash="valid-hash",
                user_id="u1",
                expires_at=now + timedelta(days=7),
            )
        )
        await db.commit()

    async with factory() as db:
        deleted = await prune_expired_revoked_tokens(db)
        assert deleted == 0

    await engine.dispose()