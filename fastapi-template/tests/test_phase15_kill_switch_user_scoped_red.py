"""Phase 15 RED regression — admin user kill-switch MUST be user-scoped.

Defect (P1, cross-tenant operational): ``app/api/admin.py``
``POST /api/admin/kill-switch/user/{user_id}`` is documented and broadcast as
"Emergency halt for a single user's live strategies", but its UPDATE swept
EVERY ``RUNNING`` ``StrategyDeploymentRecord`` platform-wide:

    update(StrategyDeploymentRecord).where(status == "RUNNING")
        .values(status="PAUSED_ADMIN_HALT")

Those deployment rows carry NO owner column, so the "user" kill-switch:
  (a) paused OTHER users' strategies platform-wide (cross-tenant mutation),
  (b) never actually halted the TARGET user's engine dispatch (deployments
      are not executed by the engine; ``StrategyRecord.enabled`` is).

Corrected contract pinned here:
  * the target user's enabled ``StrategyRecord`` rows are disabled
    (the engine-honored halt, same lever as ``pause_strategy``);
  * ANOTHER user's enabled strategies stay enabled;
  * no platform-wide deployment sweep occurs.
"""

from __future__ import annotations

import uuid as _uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import init_db
from app.main import app


async def _seed_user(email: str, role: str = "trader") -> str:
    from app.core.security import hash_password
    from app.db.session import SessionLocal
    from app.models.user import UserRecord

    uid = str(_uuid.uuid4())
    async with SessionLocal() as session:
        session.add(
            UserRecord(
                id=uid,
                email=email,
                hashed_password=hash_password("SecurePassword123!"),
                full_name="Phase15 KillSwitch Tester",
                role=role,
                is_active=True,
            )
        )
        await session.commit()
    return uid


async def _seed_enabled_strategy(user_id: str, name: str) -> str:
    import json

    from app.db.session import SessionLocal
    from app.models.trading import StrategyRecord

    sid = str(_uuid.uuid4())
    async with SessionLocal() as session:
        session.add(
            StrategyRecord(
                id=sid,
                user_id=user_id,
                name=name,
                symbols_json=json.dumps(["RELIANCE"]),
                conditions_json=json.dumps(
                    [{"indicator": "PRICE", "operator": "gte", "value": 100.0}]
                ),
                action_json=json.dumps({"side": "BUY", "quantity": 1, "order_type": "MARKET"}),
                enabled=True,
                execution_mode="PAPER",
            )
        )
        await session.commit()
    return sid


async def _seed_running_deployment(strategy_name: str) -> str:
    from app.db.session import SessionLocal
    from app.models.marketplace import StrategyDeploymentRecord

    did = str(_uuid.uuid4())
    async with SessionLocal() as session:
        session.add(
            StrategyDeploymentRecord(
                id=did,
                strategy_name=strategy_name,
                execution_mode="LIVE",
                broker_name="ZERODHA",
                multiplier=1.0,
                capital_allocated=50000.0,
                status="RUNNING",
                realized_pnl=0.0,
            )
        )
        await session.commit()
    return did


async def _fetch_strategy(sid: str):
    from app.db.session import SessionLocal
    from app.models.trading import StrategyRecord

    async with SessionLocal() as session:
        return await session.get(StrategyRecord, sid)


async def _fetch_deployment(did: str):
    from app.db.session import SessionLocal
    from app.models.marketplace import StrategyDeploymentRecord

    async with SessionLocal() as session:
        return await session.get(StrategyDeploymentRecord, did)


@pytest.mark.asyncio
async def test_user_kill_switch_halts_only_target_user():
    """Halting user A disables A's strategies ONLY: B stays enabled, and no
    platform-wide deployment sweep occurs."""
    await init_db()

    user_a = await _seed_user(f"ks_a_{_uuid.uuid4().hex[:6]}@tradetron.io")
    user_b = await _seed_user(f"ks_b_{_uuid.uuid4().hex[:6]}@tradetron.io")
    admin_id = await _seed_user(f"ks_admin_{_uuid.uuid4().hex[:6]}@tradetron.io", role="admin")

    strat_a = await _seed_enabled_strategy(user_a, "User A Algo")
    strat_b = await _seed_enabled_strategy(user_b, "User B Algo")
    dep_any = await _seed_running_deployment("Any User's Deployed Algo")

    from app.core.security import create_access_token

    admin_headers = {
        "Authorization": f"Bearer {create_access_token({'sub': admin_id, 'email': 'adm@x.io', 'role': 'admin'})}"
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/admin/kill-switch/user/{user_a}",
            json={"reason": "Max leverage breach"},
            headers=admin_headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "HALTED"

    strat_a_final = await _fetch_strategy(strat_a)
    strat_b_final = await _fetch_strategy(strat_b)
    dep_final = await _fetch_deployment(dep_any)

    assert strat_a_final is not None and strat_a_final.enabled is False, (
        "the target user's strategy was NOT halted"
    )
    # THE INVARIANT — other users are unaffected:
    assert strat_b_final is not None and strat_b_final.enabled is True, (
        "Phase15 DEFECT: user kill-switch disabled ANOTHER user's strategy"
    )
    # No platform-wide deployment sweep (deployments have no owner; pausing
    # them all on a per-user halt is the cross-tenant leak being pinned).
    assert dep_final is not None and dep_final.status == "RUNNING", (
        "Phase15 DEFECT: user kill-switch swept a RUNNING deployment platform-wide"
    )