"""Phase 17 RED regression — strategy deployments MUST carry an owner.

Defect (P1, cross-tenant operational): ``app.models.marketplace.StrategyDeploymentRecord``
has NO owner/user column.  ``POST /api/strategies/{id}/deploy`` therefore creates a
deployment row that cannot be attributed to the deploying user:

  * admin/copy ``StrategyDeploymentRecord`` surfaces (e.g. the admin
    ``/strategies/oversight``) cannot be user-scoped — the surface cannot tell
    WHICH user deployed a strategy;
  * a per-user halt (``kill_switch_user``) cannot issue a scoped deployment
    pause because the rows carry no owner identity (Phase 15 recorded this same
    limitation as the reason deployments could not be user-scoped).

Corrected contract pinned here (Phase 17):
  * a deployment created via ``POST /api/strategies/{id}/deploy`` MUST persist
    ``owner_user_id == <authenticated user id>`` (the caller derived from the
    bearer token — never a client-supplied field);
  * the admin ``/strategies/oversight`` response MUST expose the deployment
    owner so oversight is tenant-accountable;
  * a deployment row MUST be queryable/filterable by owner (the exact
    tenant-scoping primitive a per-user halt / oversight uses).
"""
from __future__ import annotations

import uuid as _uuid

import pytest

from app.db.session import init_db


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
                full_name="Phase17 Owner Tester",
                role=role,
                is_active=True,
            )
        )
        await session.commit()
    return uid

async def _seed_owned_strategy(user_id: str, name: str) -> str:
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
                enabled=False,
                execution_mode="PAPER",
            )
        )
        await session.commit()
    return sid


async def _deployment_owner(did: str):
    """Return the persisted owner_user_id for a deployment, or None if the
    column is absent (Phase 15/16 schema)."""
    from app.db.session import SessionLocal
    from app.models.marketplace import StrategyDeploymentRecord

    async with SessionLocal() as session:
        row = await session.get(StrategyDeploymentRecord, did)
        if row is None:
            return None
        return getattr(row, "owner_user_id", None)


@pytest.mark.asyncio
async def test_deploy_strategy_persists_owner_user_id():
    """A deployment created via POST /deploy MUST be attributed to the caller."""
    await init_db()

    owner_id = await _seed_user(f"owner_{_uuid.uuid4().hex[:6]}@tradetron.io")
    strat_id = await _seed_owned_strategy(owner_id, "Owner Pinned Algo")

    from httpx import ASGITransport, AsyncClient

    from app.core.security import create_access_token
    from app.main import app

    headers = {
        "Authorization": f"Bearer {create_access_token({'sub': owner_id, 'email': 'o@x.io', 'role': 'trader'})}"
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/strategies/{strat_id}/deploy",
            json={
                "execution_mode": "PAPER",
                "broker_name": "Simulated",
                "multiplier": 1.0,
                "capital_allocated": 10000.0,
            },
            headers=headers,
        )
        assert res.status_code == 200, res.text
        deployment_id = res.json()["deployment_id"]

    # THE INVARIANT — the deployment row is attributed to the caller.
    persisted_owner = await _deployment_owner(deployment_id)
    assert persisted_owner is not None, (
        "Phase 15/16 DEFECT: StrategyDeploymentRecord has no owner_user_id, "
        "so the deployment could not be tenant-scoped."
    )
    assert persisted_owner == owner_id, (
        f"deployment owner {persisted_owner!r} != caller {owner_id!r}"
    )


@pytest.mark.asyncio
async def test_admin_oversight_exposes_deployment_owner():
    """Admin /strategies/oversight MUST return the deployment owner so the
    surface is tenant-accountable."""
    await init_db()

    owner_id = await _seed_user(f"os_owner_{_uuid.uuid4().hex[:6]}@tradetron.io")
    admin_id = await _seed_user(f"os_admin_{_uuid.uuid4().hex[:6]}@tradetron.io", role="admin")
    strat_id = await _seed_owned_strategy(owner_id, "Oversight Algo")

    from httpx import ASGITransport, AsyncClient

    from app.core.security import create_access_token
    from app.main import app

    owner_headers = {
        "Authorization": f"Bearer {create_access_token({'sub': owner_id, 'email': 'o2@x.io', 'role': 'trader'})}"
    }
    admin_headers = {
        "Authorization": f"Bearer {create_access_token({'sub': admin_id, 'email': 'a@x.io', 'role': 'admin'})}"
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        deploy_res = await client.post(
            f"/api/strategies/{strat_id}/deploy",
            json={
                "execution_mode": "PAPER",
                "broker_name": "Simulated",
                "multiplier": 1.0,
                "capital_allocated": 25000.0,
            },
            headers=owner_headers,
        )
        assert deploy_res.status_code == 200, deploy_res.text
        deployment_id = deploy_res.json()["deployment_id"]

        oversight_res = await client.get("/api/admin/strategies/oversight", headers=admin_headers)
        assert oversight_res.status_code == 200, oversight_res.text

    deployments = oversight_res.json()
    matching = [d for d in deployments if d.get("deployment_id") == deployment_id]
    assert matching, "the deployed strategy was not present in admin oversight"

    # THE INVARIANT — the oversight record identifies the owner.
    owner_field = matching[0].get("owner_user_id")
    assert owner_field is not None, (
        "admin oversight does not expose the deployment owner — cannot be tenant-scoped"
    )
    assert owner_field == owner_id, (
        f"oversight owner {owner_field!r} != {owner_id!r}"
    )

