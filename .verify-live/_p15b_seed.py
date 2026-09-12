"""Phase 15B local E2E fixture — seeds a broker account + snapshot for UI verification.

SYNTHETIC LOCAL TEST FIXTURE ONLY: creates a ZERODHA broker account record and a
persisted broker_state snapshot directly in the LOCAL dev SQLite (trading.db) so
the fresh/stale/error rendering paths of the browser UI can be exercised.  These
are UI-rendering fixtures — NOT real broker credentials and NOT live market data.
"""
import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.db.session import SessionLocal
from app.models.broker_account import BrokerAccountRecord
from app.models.broker_state import BrokerStateRecord
from app.models.user import UserRecord  # noqa: F401  (register `users` so sort_tables can resolve broker_accounts.user_id FK)

FIXTURE_POSITIONS = [{"symbol": "RELIANCE", "quantity": 25, "side": "LONG",
                      "average_price": 2480.5, "product": "MIS"}]


async def main() -> None:
    user_id = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "live"
    async with SessionLocal() as db:
        acc = (await db.execute(
            select(BrokerAccountRecord).where(BrokerAccountRecord.user_id == user_id)
        )).scalar_one_or_none()
        if acc is None:
            acc = BrokerAccountRecord(
                id=str(uuid.uuid4()),
                user_id=user_id,
                broker_name="ZERODHA",
                account_name="P15B Fixture",
                status="CONNECTED",
                is_active=True,
                client_id="P15BFIX",
                api_key_encrypted="p15b_fixture_key",
                api_secret_encrypted="p15b_fixture_secret",
                token_expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).replace(tzinfo=None),
            )
            # Synthetic access token so the account is NOT token-expired — this lets
            # sync/render flows reach the real-broker adapter path, where BROKER_MODE
            # (not 'live') must hard-block and produce ERROR/fail-closed instead of
            # fabricating values.  Still a fake fixture string, never a real credential.
            acc.set_access_token("p15b_fixture_access_token")
            db.add(acc)
            await db.flush()

        snap = (await db.execute(
            select(BrokerStateRecord).where(BrokerStateRecord.broker_account_id == acc.id)
        )).scalar_one_or_none()
        if snap is None:
            snap = BrokerStateRecord(broker_account_id=acc.id, user_id=user_id, sync_count=0)
            db.add(snap)

        now = datetime.now(timezone.utc)
        if mode == "error":
            snap.status = "ERROR"
            snap.source = "BROKER"
            snap.positions_json = "[]"
            snap.positions_hash = None
            snap.available_cash = None
            snap.utilized_margin = None
            snap.total_collateral = None
            snap.total_equity = None
            snap.unrealized_pnl = None
            snap.realized_pnl = None
            snap.currency = "INR"
            snap.captured_at = None
            snap.sync_message = "Broker API failure (simulated-mode fixture)"
        else:
            captured = now
            if mode == "stale":
                captured = now - timedelta(seconds=settings.broker_state_stale_after + 60)
            snap.status = "LIVE"
            snap.source = "BROKER"
            snap.positions_json = json.dumps(FIXTURE_POSITIONS, sort_keys=True, separators=(",", ":"))
            snap.positions_hash = None
            snap.available_cash = 75000.0
            snap.utilized_margin = 23000.0
            snap.total_collateral = 98000.0
            snap.total_equity = 98000.5
            snap.unrealized_pnl = 3200.0
            snap.realized_pnl = None
            snap.currency = "INR"
            snap.captured_at = captured
            snap.last_good_captured_at = captured
            snap.sync_message = None
        snap.sync_count = (snap.sync_count or 0) + 1
        await db.commit()
        print(json.dumps({"broker_account_id": acc.id, "mode": mode}))


if __name__ == "__main__":
    asyncio.run(main())