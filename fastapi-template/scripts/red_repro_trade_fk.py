"""RED reproduction: trades.order_id FK violation on FK-enforced databases.

The production Postgres enforces ``trades.order_id -> orders.id``. The API
order paths populate ``trades.order_id`` with the DISPLAY string (``ORD_...``,
``EXIT_...``, ``CPY_ORD_...``) which matches NO row in ``orders.id`` (UUID).
SQLite does not enforce FKs by default, so the 635-test suite never catches it;
Postgres raises ForeignKeyViolation -> HTTP 500 -> stranded PENDING claim.

This script proves the violation deterministically using an FK-enforced SQLite
database (PRAGMA foreign_keys=ON — the closest local equivalent to Postgres).
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

from app.db.session import Base
from app.models.user import UserRecord
from app.models.trading import OrderRecord, TradeRecord

# Load the FULL model registry exactly like the app does, so the FK graph
# (strategies -> broker_accounts, trades -> orders, etc.) resolves before any
# create_all runs.
import app.models.broker_account  # noqa: F401
import app.models.billing  # noqa: F401
import app.models.copy_trading  # noqa: F401
import app.models.audit  # noqa: F401
import app.models.watchlist  # noqa: F401
import app.models.visual_strategy  # noqa: F401
import app.models.marketplace  # noqa: F401
import app.models.notification  # noqa: F401
import app.models.alerts  # noqa: F401


def _fk_enforcing_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, connection_record):  # noqa: ANN001, ANN002
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


async def main():
    engine = _fk_enforcing_engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as db:
        user = UserRecord(
            id=str(uuid.uuid4()),
            email="red.repro@tradethrone.test",
            hashed_password="x",
            full_name="RED Repro",
            is_active=True,
        )
        db.add(user)
        await db.commit()

        # 1. A real OrderRecord row (UUID pk) exists.
        order = OrderRecord(
            id=str(uuid.uuid4()),
            user_id=user.id,
            broker_order_id="ORD_999_aaaa",  # display string lives HERE
            symbol="RELIANCE",
            side="BUY",
            quantity=1,
            status="FILLED",
            mode="PAPER",
        )
        db.add(order)
        await db.commit()

        # 2. BUGGY insert: TradeRecord.order_id = the DISPLAY string.
        print("Inserting TradeRecord with DISPLAY string order_id ...")
        try:
            bad_trade = TradeRecord(
                id=str(uuid.uuid4()),
                order_id="ORD_999_aaaa",  # ← the bug: not a valid orders.id
                symbol="RELIANCE",
                side="BUY",
                quantity=1,
                price=100.0,
                mode="PAPER",
                user_id=user.id,
            )
            db.add(bad_trade)
            await db.commit()
            print("RESULT: INSERT SUCCEEDED (no FK enforcement in effect!)")
        except IntegrityError as exc:
            await db.rollback()
            print(f"RESULT: ForeignKeyViolation reproduced: "
                  f"{type(exc.orig).__name__}: {exc.orig}")
            # 3. Correct insert: TradeRecord.order_id = the order UUID.
            print("Inserting TradeRecord with correct UUID order_id ...")
            good_trade = TradeRecord(
                id=str(uuid.uuid4()),
                order_id=order.id,  # the fix: reference the actual orders.id
                symbol="RELIANCE",
                side="BUY",
                quantity=1,
                price=100.0,
                mode="PAPER",
                user_id=user.id,
            )
            db.add(good_trade)
            await db.commit()
            print("RESULT: correct UUID insert SUCCEEDED")
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))