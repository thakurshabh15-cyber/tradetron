"""Phase 1 Step 2 - autonomous-agent runtime: end-to-end tests.

Every test runs against a throwaway SQLite file (never the dev ``trading.db``).
The ``env`` fixture builds a fresh engine, ``create_all()`` s the schema,
wires the temp session factory into every ``SessionLocal`` consumer
(``app.db.session``, ``app.engine.agent_runtime``, ``app.api.agents``),
provisions the code-declared agent registry, and yields helpers.

Coverage (fail-closed contract):
  dispatch gates .... unknown/unsupported/disabled agent, capability
                      intersection, invalid creator/timeout/max_attempts
  claim/CAS ......... attempts consumed at claim, no double-claim,
                      stale-CAS rowcount == 0, non-RUNNING skip
  approval .......... required/expired/revoked/not-applicable
  autonomy .......... kill-switch, global level 0, ceiling exceeded, allowed,
                      human creators unaffected
  retry/backoff ..... retryable requeue + exponential backoff, eventual
                      success, max-attempts exhaustion, non-retryable
                      termination, generic-exception mapping, timeout
  stale workers ..... stale RUNNING requeued / FAILED when attempts consumed,
                      fresh heartbeat untouched
  idempotency ....... dedupe on ``idempotency_key``
  HTTP envelope ..... admin-gated auth (401/403/200), error mapping,
                      approval flow, run/config/registry/list, scheduling
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.session as db_session
import app.engine.agent_runtime as ar
from app.config import settings
from app.core.security import create_access_token, hash_password
from app.db.session import Base
from app.models.agent import AgentRecord, AgentTaskRecord
from app.models.user import UserRecord

# Register every table the built-in engineering-monitor handler queries with
# the shared metadata so the hermetic create_all() is complete.
import app.models.audit  # noqa: F401
import app.models.broker_account  # noqa: F401
import app.models.broker_state  # noqa: F401
import app.models.protective_order  # noqa: F401
import app.models.trading  # noqa: F401
# NOTE: keep this import last - the ``import app.models.*`` lines rebind the
# name ``app`` to the package module and would clobber the FastAPI instance.
from app.main import app
# ----- Test-only agent + handlers (registered once, guarded) --------
PROBE_AGENT = "probe_agent"

async def _ok_handler(ctx: ar.AgentContext) -> dict:
    return {"ok": True, "attempt": ctx.attempt, "created_by": ctx.created_by}

async def _flaky_handler(ctx: ar.AgentContext) -> dict:
    raise ar.AgentTaskFailure("FLAKY", "transient blip", retryable=True)

async def _try_twice_handler(ctx: ar.AgentContext) -> dict:
    if ctx.attempt < 2:
        raise ar.AgentTaskFailure("WARMUP", "cold start", retryable=True)
    return {"ready": True, "attempt": ctx.attempt}

async def _fatal_handler(ctx: ar.AgentContext) -> dict:
    raise ar.AgentTaskFailure("FATAL", "permanent failure", retryable=False)

async def _explode_handler(ctx: ar.AgentContext) -> dict:
    raise RuntimeError("kaboom")

async def _analyze_handler(ctx: ar.AgentContext) -> dict:
    return {"analyzed": True, "attempt": ctx.attempt}

async def _slow_handler(ctx: ar.AgentContext) -> dict:
    await asyncio.sleep(5.0)
    return {"slept": True}


def _ensure_probe_agent() -> None:
    """Register the test-only agent definition and handlers exactly once."""
    if ar.agent_definition(PROBE_AGENT) is None:
        ar.register_agent(
            ar.AgentDefinition(
                agent_type=PROBE_AGENT,
                name="Probe Agent",
                description="test-only deterministic agent",
                capabilities=(ar.CAP_READ, ar.CAP_ANALYZE),
                readonly=True,
                max_autonomy_level=1,
                enabled_by_default=False,
            )
        )
    if ar.handler_spec(PROBE_AGENT, "ok") is not None:
        return

    ar.register_handler(
        agent_type=PROBE_AGENT, task_kind="ok", required_capability=ar.CAP_READ
    )(_ok_handler)
    ar.register_handler(
        agent_type=PROBE_AGENT, task_kind="flaky", required_capability=ar.CAP_READ
    )(_flaky_handler)
    ar.register_handler(
        agent_type=PROBE_AGENT, task_kind="try_twice", required_capability=ar.CAP_READ
    )(_try_twice_handler)
    ar.register_handler(
        agent_type=PROBE_AGENT, task_kind="fatal", required_capability=ar.CAP_READ
    )(_fatal_handler)
    ar.register_handler(
        agent_type=PROBE_AGENT, task_kind="explode", required_capability=ar.CAP_READ
    )(_explode_handler)
    ar.register_handler(
        agent_type=PROBE_AGENT, task_kind="analyze", required_capability=ar.CAP_ANALYZE
    )(_analyze_handler)
    ar.register_handler(
        agent_type=PROBE_AGENT, task_kind="slow", required_capability=ar.CAP_READ
    )(_slow_handler)


_ensure_probe_agent()
# ----- Hermetic database + runtime helpers ----------------------------
class AgentEnv:
    """Convenience facade over one throwaway runtime database."""

    def __init__(self, factory, runtime) -> None:
        self.factory = factory
        self.runtime = runtime

    async def enable_agent(
        self,
        agent_type: str = "engineering_monitor",
        *,
        enabled: bool = True,
        capabilities: list[str] | None = None,
        max_autonomy_level: int | None = None,
    ) -> None:
        """Flip the registry row; the DB row may only TIGHTEN the code set."""
        async with self.factory() as db:
            agent = (
                await db.execute(
                    select(AgentRecord).where(AgentRecord.agent_type == agent_type)
                )
            ).scalar_one()
            agent.enabled = enabled
            if capabilities is not None:
                agent.capabilities_json = json.dumps(list(capabilities))
            if max_autonomy_level is not None:
                agent.max_autonomy_level = max_autonomy_level
            await db.commit()

    async def set_autonomy(self, *, enabled: bool = True, level: int = 1) -> None:
        await self.runtime.update_runtime_config(
            autonomous_mode_enabled=enabled,
            global_autonomy_level=level,
            updated_by="test",
        )

    async def dict_of(self, task_id: str) -> dict:
        async with self.factory() as db:
            row = await db.get(AgentTaskRecord, task_id)
            assert row is not None, f"task {task_id} not found"
            return ar.task_to_dict(row)

    async def all_tasks(self) -> list[dict]:
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(AgentTaskRecord).order_by(AgentTaskRecord.created_at)
                )
            ).scalars().all()
            return [ar.task_to_dict(r) for r in rows]

    async def make_user(
        self, *, role: str = "trader", is_active: bool = True
    ) -> UserRecord:
        user = UserRecord(
            email=f"{uuid.uuid4().hex[:12]}@probe.test",
            hashed_password=hash_password("ProbePass123!"),
            full_name="Probe User",
            role=role,
            kyc_status="VERIFIED",
            is_verified=True,
            is_active=is_active,
        )
        async with self.factory() as db:
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    @staticmethod
    def token_for(user: UserRecord) -> str:
        return create_access_token({"sub": user.id})


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """Hermetic SQLite DB wired into every runtime/API ``SessionLocal``."""
    db_file = tmp_path / "agent_test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    import app.api.agents as agents_api

    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(ar, "SessionLocal", factory)
    monkeypatch.setattr(agents_api, "SessionLocal", factory)

    created = await ar.ensure_agent_registry()
    assert "engineering_monitor" in created, "registry provisioning failed"
    assert PROBE_AGENT in created, "probe agent not registered"

    ag_env = AgentEnv(factory, ar.AgentRuntime())
    ag_env.engine = engine  # type: ignore[attr-defined]
    yield ag_env
    await engine.dispose()


@pytest.fixture
async def api_client(env):
    """Async HTTP client against the real FastAPI app, temp-DB backed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _admin_headers(env: AgentEnv) -> tuple[dict[str, str], UserRecord]:
    admin = await env.make_user(role="admin")
    return {"Authorization": f"Bearer {env.token_for(admin)}"}, admin


async def _trader_headers(env: AgentEnv) -> dict[str, str]:
    trader = await env.make_user(role="trader")
    return {"Authorization": f"Bearer {env.token_for(trader)}"}
# ----- Dispatch-time gates --------------------------------------------
async def test_dispatch_unknown_agent_rejected(env):
    with pytest.raises(ar.AgentDispatchError) as exc_info:
        await env.runtime.create_task(agent_type="ghost", task_kind="haunt")
    assert exc_info.value.code == ar.ERR_AGENT_NOT_REGISTERED


async def test_dispatch_unsupported_task_kind_rejected(env):
    with pytest.raises(ar.AgentDispatchError) as exc_info:
        await env.runtime.create_task(
            agent_type="engineering_monitor", task_kind="fly_the_plane"
        )
    assert exc_info.value.code == ar.ERR_UNSUPPORTED_TASK_KIND


async def test_dispatch_requires_enabled_registry_row(env):
    # row provisioned enabled=False -> fail-closed at dispatch
    with pytest.raises(ar.AgentDispatchError) as exc_info:
        await env.runtime.create_task(
            agent_type="engineering_monitor", task_kind="system_health_report"
        )
    assert exc_info.value.code == ar.ERR_AGENT_DISABLED


async def test_dispatch_capability_intersection_tightens(env):
    # full code + DB caps: ANALYZE dispatch passes
    await env.enable_agent(PROBE_AGENT)
    task, created = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="analyze"
    )
    assert created is True
    assert task["task_kind"] == "analyze"
    # operator narrows ONLY the DB row: ANALYZE now intersection-excluded
    await env.enable_agent(PROBE_AGENT, capabilities=[ar.CAP_READ])
    with pytest.raises(ar.AgentDispatchError) as exc_info:
        await env.runtime.create_task(agent_type=PROBE_AGENT, task_kind="analyze")
    assert exc_info.value.code == ar.ERR_CAPABILITY_DENIED


@pytest.mark.parametrize(
    "kwargs,expected_code",
    [
        ({"created_by": "robot"}, ar.ERR_INVALID_CREATOR),
        ({"timeout_seconds": -1}, ar.ERR_INVALID_TIMEOUT),
        ({"max_attempts": 0}, ar.ERR_INVALID_MAX_ATTEMPTS),
        ({"max_attempts": 11}, ar.ERR_INVALID_MAX_ATTEMPTS),
        ({"idempotency_key": "x" * 200}, ar.ERR_INVALID_IDEMPOTENCY_KEY),
    ],
)
async def test_dispatch_parameter_gates(env, kwargs, expected_code):
    await env.enable_agent(PROBE_AGENT)
    with pytest.raises(ar.AgentDispatchError) as exc_info:
        await env.runtime.create_task(
            agent_type=PROBE_AGENT, task_kind="ok", **kwargs
        )
    assert exc_info.value.code == expected_code


# ----- Durable claiming / CAS ------------------------------------------
async def test_claim_consumes_attempts_and_marks_running(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(agent_type=PROBE_AGENT, task_kind="ok")
    claimed = await env.runtime._claim_batch()
    assert claimed == [task["id"]]
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_RUNNING
    assert row["attempts"] == 1
    assert row["started_at"] is not None
    # RUNNING rows are never claimed twice
    assert await env.runtime._claim_batch() == []


async def test_cas_rejects_stale_attempt_count(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(agent_type=PROBE_AGENT, task_kind="ok")
    async with env.factory() as db:
        result = await db.execute(
            update(AgentTaskRecord)
            .where(
                AgentTaskRecord.id == task["id"],
                AgentTaskRecord.status == ar.STATUS_PENDING,
                AgentTaskRecord.attempts == 1,  # stale - actual value is 0
            )
            .values(status=ar.STATUS_RUNNING, attempts=2)
        )
        assert result.rowcount == 0
    assert (await env.dict_of(task["id"]))["status"] == ar.STATUS_PENDING


async def test_execute_task_skips_non_running(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(agent_type=PROBE_AGENT, task_kind="ok")
    outcome = await env.runtime.execute_task(task["id"])
    assert outcome["outcome"] == "not_running"
    outcome = await env.runtime.execute_task("does-not-exist")
    assert outcome["outcome"] == "missing"
# ----- Human approval gating -------------------------------------------
async def test_approval_required_blocks_claim_until_approved(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="ok", requires_approval=True
    )
    summary = await env.runtime.run_once()
    assert summary["claimed"] == 0, "unapproved approval-required task claimed"
    assert (await env.dict_of(task["id"]))["status"] == ar.STATUS_PENDING

    approved = await env.runtime.approve_task(task["id"], approved_by="user-1")
    assert approved["approved_by"] == "user-1"
    assert approved["approved_at"] is not None
    assert approved["approval_expires_at"] is not None

    summary = await env.runtime.run_once()
    assert summary["succeeded"] == 1
    assert (await env.dict_of(task["id"]))["status"] == ar.STATUS_SUCCEEDED


async def test_approval_expiry_blocks_claim_and_fails_closed(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="ok", requires_approval=True
    )
    await env.runtime.approve_task(task["id"], approved_by="user-1")
    # let the approval window lapse
    async with env.factory() as db:
        row = await db.get(AgentTaskRecord, task["id"])
        row.approval_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    # claim filter refuses expired approvals
    assert await env.runtime._claim_batch() == []
    # simulate a racing claim (RUNNING): execute re-checks the gate and FAILS
    async with env.factory() as db:
        await db.execute(
            update(AgentTaskRecord)
            .where(AgentTaskRecord.id == task["id"])
            .values(
                status=ar.STATUS_RUNNING,
                attempts=1,
                started_at=datetime.now(timezone.utc),
                heartbeat_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    outcome = await env.runtime.execute_task(task["id"])
    assert outcome["outcome"] == "failed"
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_FAILED
    assert row["error"]["code"] == ar.ERR_APPROVAL_EXPIRED


async def test_approval_revocation_blocks_claim(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="ok", requires_approval=True
    )
    await env.runtime.approve_task(task["id"], approved_by="user-1")
    revoked = await env.runtime.revoke_approval(task["id"], revoked_by="user-1")
    assert revoked["approved_by"] is None
    assert revoked["approved_at"] is None
    assert revoked["approval_expires_at"] is None
    assert await env.runtime._claim_batch() == []


async def test_approval_not_applicable_and_missing(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(agent_type=PROBE_AGENT, task_kind="ok")
    with pytest.raises(ar.AgentDispatchError) as exc_info:
        await env.runtime.approve_task(task["id"], approved_by="user-1")
    assert exc_info.value.code == ar.ERR_APPROVAL_NOT_APPLICABLE
    with pytest.raises(ar.AgentDispatchError) as exc_info:
        await env.runtime.approve_task("missing-task", approved_by="user-1")
    assert exc_info.value.code == ar.ERR_INVALID_INPUT
# ----- Autonomy gate ---------------------------------------------------
async def test_autonomy_kill_switch_blocks_agent_created_task(env):
    await env.enable_agent("engineering_monitor")  # autonomous mode stays OFF
    task, _ = await env.runtime.create_task(
        agent_type="engineering_monitor",
        task_kind="system_health_report",
        created_by=ar.CREATOR_AGENT,
    )
    summary = await env.runtime.run_once()
    assert summary["failed"] == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_FAILED
    assert row["error"]["code"] == ar.ERR_AUTONOMOUS_MODE_DISABLED


async def test_autonomy_global_level_zero_fails_closed(env):
    await env.enable_agent("engineering_monitor")
    await env.set_autonomy(enabled=True, level=0)
    task, _ = await env.runtime.create_task(
        agent_type="engineering_monitor",
        task_kind="system_health_report",
        created_by=ar.CREATOR_SYSTEM,
    )
    summary = await env.runtime.run_once()
    assert summary["failed"] == 1
    row = await env.dict_of(task["id"])
    assert row["error"]["code"] == ar.ERR_AUTONOMY_LEVEL_EXCEEDED


async def test_autonomy_ceiling_exceeded_fails_closed(env):
    # DB row lowers the autonomy envelope below the code ceiling
    await env.enable_agent("engineering_monitor", max_autonomy_level=0)
    await env.set_autonomy(enabled=True, level=1)
    task, _ = await env.runtime.create_task(
        agent_type="engineering_monitor",
        task_kind="system_health_report",
        created_by=ar.CREATOR_AGENT,
    )
    summary = await env.runtime.run_once()
    assert summary["failed"] == 1
    row = await env.dict_of(task["id"])
    assert row["error"]["code"] == ar.ERR_AUTONOMY_LEVEL_EXCEEDED


async def test_autonomy_within_ceiling_executes(env):
    await env.enable_agent("engineering_monitor")  # DB ceiling 1 == code 1
    await env.set_autonomy(enabled=True, level=1)
    task, _ = await env.runtime.create_task(
        agent_type="engineering_monitor",
        task_kind="system_health_report",
        created_by=ar.CREATOR_AGENT,
    )
    summary = await env.runtime.run_once()
    assert summary["succeeded"] == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_SUCCEEDED
    assert isinstance(row["output"], dict)
    assert row["output"]["autonomy_granted"] is True


async def test_human_created_task_ignores_autonomy_gate(env):
    await env.enable_agent("engineering_monitor")  # autonomous mode OFF
    task, _ = await env.runtime.create_task(
        agent_type="engineering_monitor",
        task_kind="system_health_report",
        created_by=ar.CREATOR_USER,
    )
    summary = await env.runtime.run_once()
    assert summary["succeeded"] == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_SUCCEEDED
    assert row["output"]["autonomy_granted"] is False
# ----- Retry / backoff / timeout ---------------------------------------
async def test_retryable_failure_requeues_with_backoff_then_exhausts(env, monkeypatch):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="flaky", max_attempts=3
    )
    before = datetime.now(timezone.utc)
    summary = await env.runtime.run_once()
    assert summary["claimed"] == 1
    assert summary["requeued"] == 1

    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_PENDING
    assert row["attempts"] == 1
    assert row["error"]["code"] == "FLAKY"
    assert row["error"]["retryable"] is True
    assert row["completed_at"] is None
    # exponential backoff, attempt 1 -> base * 2^0 == base seconds
    gap = datetime.fromisoformat(row["next_retry_at"]) - before
    assert timedelta(seconds=4) <= gap <= timedelta(seconds=6)

    # zero the backoff BASE so further retries reschedule immediately; backdate
    # the already-scheduled +5s retry window so the task is claimable right now
    monkeypatch.setattr(settings, "agent_retry_backoff_seconds", 0.0)
    async with env.factory() as db:
        r = await db.get(AgentTaskRecord, task["id"])
        r.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    s2 = await env.runtime.run_once()
    assert s2["requeued"] == 1 and (await env.dict_of(task["id"]))["attempts"] == 2
    s3 = await env.runtime.run_once()
    assert s3["failed"] == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_FAILED
    assert row["attempts"] == 3
    assert row["error"]["code"] == "FLAKY"
    assert row["completed_at"] is not None


async def test_retry_eventually_succeeds(env, monkeypatch):
    monkeypatch.setattr(settings, "agent_retry_backoff_seconds", 0.0)
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="try_twice"
    )
    s1 = await env.runtime.run_once()
    assert s1["requeued"] == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_PENDING and row["attempts"] == 1
    s2 = await env.runtime.run_once()
    assert s2["succeeded"] == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_SUCCEEDED
    assert row["attempts"] == 2
    assert row["output"] == {"ready": True, "attempt": 2}


async def test_non_retryable_failure_terminates(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(agent_type=PROBE_AGENT, task_kind="fatal")
    summary = await env.runtime.run_once()
    assert summary["failed"] == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_FAILED
    assert row["error"]["code"] == "FATAL"
    assert row["error"]["retryable"] is False
    assert row["completed_at"] is not None


async def test_generic_exception_maps_to_retryable_handler_failed(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="explode"
    )
    summary = await env.runtime.run_once()
    assert summary["requeued"] == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_PENDING
    assert row["error"]["code"] == ar.ERR_HANDLER_FAILED
    assert row["error"]["retryable"] is True
    assert "kaboom" in row["error"]["message"]


async def test_handler_timeout_requeues(env, monkeypatch):
    monkeypatch.setattr(settings, "agent_task_min_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "agent_retry_backoff_seconds", 0.0)
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="slow", timeout_seconds=0.1
    )
    assert task["timeout_seconds"] == 0.1
    summary = await env.runtime.run_once()
    assert summary["requeued"] == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_PENDING
    assert row["error"]["code"] == ar.ERR_TIMEOUT
    assert row["error"]["retryable"] is True
# ----- Stale-worker recovery -------------------------------------------
async def test_stale_running_requeued_with_workerlost(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(agent_type=PROBE_AGENT, task_kind="ok")
    assert await env.runtime._claim_batch() == [task["id"]]  # simulate a crash
    async with env.factory() as db:
        row = await db.get(AgentTaskRecord, task["id"])
        assert row is not None
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        row.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await db.commit()
    assert await env.runtime._recover_stale_running() == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_PENDING
    assert row["attempts"] == 1  # crashed attempt was consumed
    assert row["error"]["code"] == ar.ERR_WORKER_LOST
    # the recovered task is immediately claimable and completes
    summary = await env.runtime.run_once()
    assert summary["succeeded"] == 1


async def test_stale_running_attempts_exhausted_fails(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="ok", max_attempts=1
    )
    await env.runtime._claim_batch()  # attempts 0 -> 1 == max
    async with env.factory() as db:
        row = await db.get(AgentTaskRecord, task["id"])
        assert row is not None
        row.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await db.commit()
    assert await env.runtime._recover_stale_running() == 1
    row = await env.dict_of(task["id"])
    assert row["status"] == ar.STATUS_FAILED
    assert row["error"]["code"] == ar.ERR_WORKER_LOST
    assert row["attempts"] == 1
    assert row["completed_at"] is not None


async def test_fresh_running_untouched(env):
    await env.enable_agent(PROBE_AGENT)
    task, _ = await env.runtime.create_task(agent_type=PROBE_AGENT, task_kind="ok")
    await env.runtime._claim_batch()
    assert await env.runtime._recover_stale_running() == 0
    assert (await env.dict_of(task["id"]))["status"] == ar.STATUS_RUNNING


# ----- Idempotent dispatch ---------------------------------------------
async def test_idempotent_dispatch_dedupes(env):
    await env.enable_agent(PROBE_AGENT)
    first, created1 = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="ok", idempotency_key="dispatch-1"
    )
    second, created2 = await env.runtime.create_task(
        agent_type=PROBE_AGENT, task_kind="ok", idempotency_key="dispatch-1"
    )
    assert created1 is True
    assert created2 is False
    assert second["id"] == first["id"]
    assert len(await env.all_tasks()) == 1


# ----- Scheduler wiring -------------------------------------------------
async def test_scheduler_start_stop_is_idempotent(env):
    sched = ar.AgentRuntimeScheduler(interval_seconds=0.05)
    sched.start()
    try:
        assert sched.is_running() is True
        sched.start()  # idempotent restart is a no-op
        assert sched.is_running() is True
    finally:
        sched.stop()
        if sched._task is not None:
            try:
                await sched._task
            except asyncio.CancelledError:
                pass
    assert sched.is_running() is False
# ----- HTTP API: admin gating ------------------------------------------
async def test_agents_api_requires_admin(env, api_client):
    # anonymous -> 401
    resp = await api_client.get("/api/agents/tasks")
    assert resp.status_code == 401
    # regular trader -> 403 (get_current_admin_user)
    resp = await api_client.get(
        "/api/agents/tasks", headers=await _trader_headers(env)
    )
    assert resp.status_code == 403
    assert "Administrative privileges required" in resp.json()["detail"]
    # trader cannot dispatch either
    resp = await api_client.post(
        "/api/agents/tasks",
        json={"agent_type": PROBE_AGENT, "task_kind": "ok"},
        headers=await _trader_headers(env),
    )
    assert resp.status_code == 403
    # admin -> 200
    headers, _ = await _admin_headers(env)
    resp = await api_client.get("/api/agents/tasks", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"tasks": [], "count": 0}


async def test_agents_api_deactivated_admin_rejected(env, api_client):
    deactivated = await env.make_user(role="admin", is_active=False)
    resp = await api_client.get(
        "/api/agents/config",
        headers={"Authorization": f"Bearer {env.token_for(deactivated)}"},
    )
    assert resp.status_code == 401  # get_current_user refuses inactive accounts


# ----- HTTP API: dispatch error mapping ---------------------------------
async def test_agents_api_dispatch_error_mapping(env, api_client):
    headers, _ = await _admin_headers(env)
    # disabled agent -> 403
    resp = await api_client.post(
        "/api/agents/tasks",
        json={
            "agent_type": "engineering_monitor",
            "task_kind": "system_health_report",
        },
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == ar.ERR_AGENT_DISABLED
    # unknown agent -> 404
    resp = await api_client.post(
        "/api/agents/tasks",
        json={"agent_type": "ghost", "task_kind": "haunt"},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == ar.ERR_AGENT_NOT_REGISTERED
    # unsupported task kind -> 404
    resp = await api_client.post(
        "/api/agents/tasks",
        json={"agent_type": "engineering_monitor", "task_kind": "fly"},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == ar.ERR_UNSUPPORTED_TASK_KIND
    # invalid creator -> 400
    resp = await api_client.post(
        "/api/agents/tasks",
        json={
            "agent_type": "engineering_monitor",
            "task_kind": "system_health_report",
            "created_by": "robot",
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == ar.ERR_INVALID_CREATOR

    # capability intersection -> 403 (DB row may only tighten)
    await env.enable_agent(PROBE_AGENT, capabilities=[ar.CAP_READ])
    resp = await api_client.post(
        "/api/agents/tasks",
        json={"agent_type": PROBE_AGENT, "task_kind": "analyze"},
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == ar.ERR_CAPABILITY_DENIED

    # dispatch a valid task, then approve a non-approval task -> 409
    resp = await api_client.post(
        "/api/agents/tasks",
        json={"agent_type": PROBE_AGENT, "task_kind": "ok"},
        headers=headers,
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]
    resp = await api_client.post(f"/api/agents/tasks/{tid}/approve", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == ar.ERR_APPROVAL_NOT_APPLICABLE
    # approving a missing task -> 404 NOT_FOUND
    resp = await api_client.post(
        "/api/agents/tasks/does-not-exist/approve", headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NOT_FOUND"
# ----- HTTP API: full task lifecycle -----------------------------------
async def test_agents_api_approval_flow(env, api_client):
    await env.enable_agent(PROBE_AGENT)
    headers, admin = await _admin_headers(env)
    resp = await api_client.post(
        "/api/agents/tasks",
        json={
            "agent_type": PROBE_AGENT,
            "task_kind": "ok",
            "requires_approval": True,
        },
        headers=headers,
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]
    # approval required -> a scheduler pass claims nothing
    run = await api_client.post("/api/agents/run", headers=headers)
    assert run.status_code == 200
    assert run.json()["claimed"] == 0
    # approve
    resp = await api_client.post(f"/api/agents/tasks/{tid}/approve", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["approved_by"] == str(admin.id)
    # now it executes
    run = await api_client.post("/api/agents/run", headers=headers)
    assert run.status_code == 200
    assert run.json()["succeeded"] == 1
    # single-task read reflects the terminal state
    resp = await api_client.get(f"/api/agents/tasks/{tid}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == ar.STATUS_SUCCEEDED
    assert resp.json()["output"]["ok"] is True
    # revoke on a COMPLETED task clears the stamp (revoke is blocked only for
    # in-flight RUNNING work, not terminal history; approve is the closed gate)
    resp = await api_client.post(
        f"/api/agents/tasks/{tid}/revoke-approval", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["approved_by"] is None


async def test_agents_api_idempotent_dispatch(env, api_client):
    await env.enable_agent(PROBE_AGENT)
    headers, _ = await _admin_headers(env)
    payload = {
        "agent_type": PROBE_AGENT,
        "task_kind": "ok",
        "idempotency_key": "http-k-1",
    }
    r1 = await api_client.post("/api/agents/tasks", json=payload, headers=headers)
    assert r1.status_code == 201 and r1.json()["created"] is True
    r2 = await api_client.post("/api/agents/tasks", json=payload, headers=headers)
    assert r2.status_code == 201 and r2.json()["created"] is False
    assert r2.json()["id"] == r1.json()["id"]
    listing = await api_client.get(
        "/api/agents/tasks?status=PENDING", headers=headers
    )
    assert listing.status_code == 200
    assert listing.json()["count"] == 1


async def test_agents_api_autonomous_dispatch_gated_at_run(env, api_client):
    await env.enable_agent("engineering_monitor")
    headers, _ = await _admin_headers(env)
    payload = {
        "agent_type": "engineering_monitor",
        "task_kind": "system_health_report",
        "created_by": ar.CREATOR_AGENT,
    }
    # dispatch is legal; the autonomy kill-switch trips at execution
    resp = await api_client.post("/api/agents/tasks", json=payload, headers=headers)
    assert resp.status_code == 201
    run = await api_client.post("/api/agents/run", headers=headers)
    assert run.status_code == 200 and run.json()["failed"] == 1
    # flip the kill-switch on with a compatible ceiling -> now executes
    patch = await api_client.patch(
        "/api/agents/config",
        json={"autonomous_mode_enabled": True, "global_autonomy_level": 1},
        headers=headers,
    )
    assert patch.status_code == 200
    assert patch.json()["autonomous_mode_enabled"] is True
    resp = await api_client.post("/api/agents/tasks", json=payload, headers=headers)
    assert resp.status_code == 201
    run = await api_client.post("/api/agents/run", headers=headers)
    assert run.status_code == 200 and run.json()["succeeded"] == 1


async def test_agents_api_inventory_config_and_registry_sync(env, api_client):
    headers, _ = await _admin_headers(env)
    inventory = await api_client.get("/api/agents", headers=headers)
    assert inventory.status_code == 200
    assert inventory.json()["total"] >= 2
    agents = {a["agent_type"] for a in inventory.json()["agents"]}
    assert "engineering_monitor" in agents and PROBE_AGENT in agents

    cfg = await api_client.get("/api/agents/config", headers=headers)
    assert cfg.status_code == 200
    assert cfg.json()["autonomous_mode_enabled"] is False

    sync = await api_client.post("/api/agents/registry/sync", headers=headers)
    assert sync.status_code == 200
    assert sync.json()["created"] == []  # already provisioned -> idempotent

    run = await api_client.post("/api/agents/run", headers=headers)
    assert run.status_code == 200
    assert set(run.json()) == {
        "recovered",
        "claimed",
        "succeeded",
        "failed",
        "requeued",
        "skipped",
    }
