"""Step 9 regression — copy-trading resume must preserve a LIVE broker linkage.

Bug (Step 9 audit, P2): ``POST /api/copy-trading/join`` resuming a STOPPED
subscription used ``existing.mode = req.mode`` and
``existing.broker_account_id = req.broker_account_id if req.mode == "LIVE"`` —
when the caller omits ``mode`` on resume, the row's mode flipped to ``None``
and the previously-attached LIVE broker account was silently detached.
``update_following_settings`` (PATCH) already resolves ``(req.mode or
follower.mode)``; the resume path now mirrors it.

Contract verified here:
  * resume without ``mode`` keeps ``mode == "LIVE"`` and the broker linkage;
  * resume with an explicit ``mode != LIVE`` detaches the broker;
  * a STOPPED->ACTIVE resume keeps the same follower row.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.broker_account import BrokerAccountRecord
from app.models.copy_trading import CopyFollowerRecord
from app.models.user import UserRecord


@pytest.fixture(autouse=True)
async def setup_database():
    """Ensure DB schema is initialized before test execution."""
    await init_db()


def _token(uid: str) -> str:
    return create_access_token({"sub": uid, "email": f"{uid}@s9.test", "role": "trader"})


async def _seed_users():
    master_id, follower_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with SessionLocal() as db:
        db.add(UserRecord(
            id=master_id, email=f"{master_id}@s9.test",
            hashed_password=hash_password("Passw0rd!"), full_name="Master",
            role="trader", is_active=True, is_verified=True,
        ))
        db.add(UserRecord(
            id=follower_id, email=f"{follower_id}@s9.test",
            hashed_password=hash_password("Passw0rd!"), full_name="Follower",
            role="trader", is_active=True, is_verified=True,
            paper_balance=1_000_000.0,
        ))
        db.add(BrokerAccountRecord(
            id=str(uuid.uuid4()), user_id=follower_id, broker_name="ZERODHA",
            account_name="Follower LIVE", client_id="ZK0001",
            status="CONNECTED", is_active=True, api_key_encrypted="",
        ))
        await db.commit()
    return master_id, follower_id



async def _get_connected_broker_id(follower_id: str) -> str:
    async with SessionLocal() as db:
        return (
            await db.execute(
                select(BrokerAccountRecord).where(
                    BrokerAccountRecord.user_id == follower_id,
                    BrokerAccountRecord.status == "CONNECTED",
                )
            )
        ).scalar_one().id


@pytest.mark.asyncio
async def test_resume_without_mode_keeps_live_broker_linkage():
    client = TestClient(app)
    master_id, follower_id = await _seed_users()
    master_tok, follower_tok = _token(master_id), _token(follower_id)

    grp = client.post(
        "/api/copy-trading/groups",
        headers={"Authorization": f"Bearer {master_tok}"},
        json={"name": "Step9 Alpha", "is_public": True},
    )
    assert grp.status_code == 200
    group_id = grp.json()["group"]["id"]

    broker_id = await _get_connected_broker_id(follower_id)

    join = client.post(
        "/api/copy-trading/join",
        headers={"Authorization": f"Bearer {follower_tok}"},
        json={"group_id": group_id, "mode": "LIVE", "broker_account_id": broker_id},
    )
    assert join.status_code == 200, join.text
    follower_row_id = join.json()["follower_id"]

    stop = client.patch(
        f"/api/copy-trading/following/{follower_row_id}",
        headers={"Authorization": f"Bearer {follower_tok}"},
        json={"status": "STOPPED"},
    )
    assert stop.status_code == 200, stop.text

    # Resume WITHOUT sending mode — LIVE mode + broker linkage must survive
    # (defect: previously reset to None and detached the broker).
    resumed = client.post(
        "/api/copy-trading/join",
        headers={"Authorization": f"Bearer {follower_tok}"},
        json={"group_id": group_id},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["follower_id"] == follower_row_id

    async with SessionLocal() as db:
        row = await db.get(CopyFollowerRecord, follower_row_id)
        assert row is not None
        assert row.status == "ACTIVE"
        assert row.mode == "LIVE", f"resume dropped mode to {row.mode!r}"
        assert row.broker_account_id == broker_id, (
            "resume without mode silently detached the LIVE broker linkage"
        )


@pytest.mark.asyncio
async def test_resume_switching_to_paper_detaches_broker():
    client = TestClient(app)
    master_id, follower_id = await _seed_users()
    master_tok, follower_tok = _token(master_id), _token(follower_id)

    grp = client.post(
        "/api/copy-trading/groups",
        headers={"Authorization": f"Bearer {master_tok}"},
        json={"name": "Step9 Beta", "is_public": True},
    )
    assert grp.status_code == 200
    group_id = grp.json()["group"]["id"]

    broker_id = await _get_connected_broker_id(follower_id)

    join = client.post(
        "/api/copy-trading/join",
        headers={"Authorization": f"Bearer {follower_tok}"},
        json={"group_id": group_id, "mode": "LIVE", "broker_account_id": broker_id},
    )
    assert join.status_code == 200, join.text
    follower_row_id = join.json()["follower_id"]

    stop = client.patch(
        f"/api/copy-trading/following/{follower_row_id}",
        headers={"Authorization": f"Bearer {follower_tok}"},
        json={"status": "STOPPED"},
    )
    assert stop.status_code == 200

    # Explicit downgrade to PAPER on resume MUST detach the broker.
    resumed = client.post(
        "/api/copy-trading/join",
        headers={"Authorization": f"Bearer {follower_tok}"},
        json={"group_id": group_id, "mode": "PAPER"},
    )
    assert resumed.status_code == 200, resumed.text

    async with SessionLocal() as db:
        row = await db.get(CopyFollowerRecord, follower_row_id)
        assert row is not None
        assert row.mode == "PAPER"
        assert row.broker_account_id is None
    return master_id, follower_id