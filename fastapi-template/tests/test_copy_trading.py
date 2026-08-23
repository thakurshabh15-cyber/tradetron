"""Comprehensive Test Suite for Copy Trading & Master-Slave Trade Fan-Out Engine."""

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import SessionLocal, init_db
from app.core.security import create_access_token, hash_password
from app.models.user import UserRecord
from app.models.copy_trading import CopyGroupRecord, CopyFollowerRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.engine.copy_trading import copy_trading_engine
import uuid


@pytest.fixture(autouse=True)
async def setup_database():
    """Ensure DB schema is initialized before test execution."""
    await init_db()


@pytest.mark.asyncio
async def test_copy_trading_full_lifecycle():
    client = TestClient(app)

    async with SessionLocal() as db:
        # Create Master User
        master_id = str(uuid.uuid4())
        master_email = f"master_{master_id[:8]}@tradetron.io"
        master = UserRecord(
            id=master_id,
            email=master_email,
            hashed_password=hash_password("MasterPass123!"),
            full_name="Master Alpha Trader",
            role="trader",
            is_active=True,
            is_verified=True,
        )
        db.add(master)

        # Create Follower User
        follower_id = str(uuid.uuid4())
        follower_email = f"follower_{follower_id[:8]}@tradetron.io"
        follower = UserRecord(
            id=follower_id,
            email=follower_email,
            hashed_password=hash_password("FollowerPass123!"),
            full_name="Follower Trader",
            role="trader",
            is_active=True,
            is_verified=True,
            paper_balance=1000000.0,
        )
        db.add(follower)
        await db.commit()

    master_token = create_access_token({"sub": master_id, "email": master_email, "role": "trader"})
    follower_token = create_access_token({"sub": follower_id, "email": follower_email, "role": "trader"})

    # 1. Master creates Copy Group
    create_res = client.post(
        "/api/copy-trading/groups",
        headers={"Authorization": f"Bearer {master_token}"},
        json={
            "name": "NIFTY Momentum Alpha",
            "description": "Sub-millisecond momentum breakout strategy",
            "profit_share_pct": 20.0,
            "min_capital": 15000.0,
            "is_public": True,
        },
    )
    assert create_res.status_code == 200
    group_data = create_res.json()["group"]
    group_id = group_data["id"]
    invite_code = group_data["invite_code"]
    assert invite_code.startswith("CPY-")

    # 2. Verify Master groups listing
    mine_res = client.get(
        "/api/copy-trading/groups/mine",
        headers={"Authorization": f"Bearer {master_token}"},
    )
    assert mine_res.status_code == 200
    my_groups = mine_res.json()
    assert any(g["id"] == group_id for g in my_groups)

    # 3. Explore public groups
    explore_res = client.get("/api/copy-trading/explore")
    assert explore_res.status_code == 200
    assert len(explore_res.json()) >= 1

    # 4. Follower joins group via invite code with 2.0x lot multiplier
    join_res = client.post(
        "/api/copy-trading/join",
        headers={"Authorization": f"Bearer {follower_token}"},
        json={
            "invite_code": invite_code,
            "multiplier": 2.0,
            "max_allocation": 100000.0,
            "mode": "PAPER",
        },
    )
    assert join_res.status_code == 200
    follower_sub_id = join_res.json()["follower_id"]

    # 5. Verify follower subscription listing
    following_res = client.get(
        "/api/copy-trading/following",
        headers={"Authorization": f"Bearer {follower_token}"},
    )
    assert following_res.status_code == 200
    following_list = following_res.json()
    assert len(following_list) == 1
    assert following_list[0]["multiplier"] == 2.0
    assert following_list[0]["group_id"] == group_id

    # 6. Master places a 10-lot manual order -> Fan-out engine mirrors 20 lots (2.0x) to Follower
    order_res = client.post(
        "/api/trades/order",
        headers={"Authorization": f"Bearer {master_token}"},
        json={
            "symbol": "RELIANCE",
            "side": "BUY",
            "quantity": 10,
            "order_type": "MARKET",
            "mode": "PAPER",
        },
    )
    assert order_res.status_code == 200
    order_data = order_res.json()
    assert order_data["quantity"] == 10
    master_pos_id = order_data["position_id"]

    # Check that Follower has a mirrored position with quantity == 20
    async with SessionLocal() as db:
        follower_pos = (
            await db.execute(
                pytest.importorskip("sqlalchemy").select(PositionRecord).where(
                    PositionRecord.user_id == follower_id,
                    PositionRecord.symbol == "RELIANCE",
                    PositionRecord.status == "OPEN",
                )
            )
        ).scalar_one_or_none()
        assert follower_pos is not None
        assert follower_pos.quantity == 20  # 10 lots * 2.0x multiplier = 20 lots

    # 7. Master closes position -> Follower position is concurrently closed
    close_res = client.post(
        f"/api/trades/positions/{master_pos_id}/close",
        headers={"Authorization": f"Bearer {master_token}"},
    )
    assert close_res.status_code == 200

    # 8. Follower updates settings to 1.5x multiplier and PAUSED status
    patch_res = client.patch(
        f"/api/copy-trading/following/{follower_sub_id}",
        headers={"Authorization": f"Bearer {follower_token}"},
        json={"multiplier": 1.5, "status": "PAUSED"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["multiplier"] == 1.5
    assert patch_res.json()["status"] == "PAUSED"

    # 9. Follower leaves group
    leave_res = client.delete(
        f"/api/copy-trading/following/{follower_sub_id}",
        headers={"Authorization": f"Bearer {follower_token}"},
    )
    assert leave_res.status_code == 200
