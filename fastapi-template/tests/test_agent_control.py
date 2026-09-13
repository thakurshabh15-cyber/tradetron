"""Phase 1 Step 4 - autonomous-agent control plane: hermetic tests."""
from __future__ import annotations
import json, uuid
from datetime import datetime, timezone
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import app.db.session as db_session
import app.engine.agent_control as ac
import app.engine.agent_runtime as ar
import app.engine.agent_intents as ai
from app.config import settings
from app.core.security import create_access_token, hash_password
from app.db.session import Base
from app.models.agent import AgentRecord, AgentRuntimeConfigRecord, AgentTaskRecord
from app.models.agent_control import (
    AUTONOMY_OBSERVE, AUTONOMY_PAPER,
    DECISION_NEEDS_APPROVAL, DECISION_NO_TRADE, DECISION_TRADE, MODE_PAPER,
    AgentConfigRecord, AgentDecisionRecord,
)
from app.models.audit import AuditLogRecord  # noqa: F401
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import StrategyRecord
from app.models.user import UserRecord
import app.engine.agent_intent_triggers  # noqa: F401
from app.main import app  # noqa: E402
QUOTE_PRICE = 250.0
def _json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)

class ControlEnv:
    def __init__(self, factory) -> None:
        self.factory = factory
        self.service = ac.agent_control_service
        self.feed_price = QUOTE_PRICE
        self.feed_available = True
    def quote(self, symbol: str) -> dict | None:
        if not self.feed_available:
            return None
        return {"symbol": symbol.upper(), "price": self.feed_price, "ltp": self.feed_price,
                "data_status": "LIVE", "is_stale": False, "age_seconds": 0.0}
    def set_feed(self, *, price: float | None = None, available: bool | None = None):
        if price is not None:
            self.feed_price = price
        if available is not None:
            self.feed_available = available
    async def make_user(self, *, role: str = "trader") -> UserRecord:
        user = UserRecord(
            email=f"{uuid.uuid4().hex[:12]}@control.test",
            hashed_password=hash_password("ControlPass123!"),
            full_name="Control Tester", role=role, kyc_status="VERIFIED",
            is_verified=True, is_active=True,
        )
        async with self.factory() as db:
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user
    async def make_broker(self, user_id: str) -> BrokerAccountRecord:
        acc = BrokerAccountRecord(user_id=user_id, broker_name="SIMULATED",
                                  account_name="Agent Control Test")
        acc.set_api_key("test-key")
        async with self.factory() as db:
            db.add(acc)
            await db.commit()
            await db.refresh(acc)
            return acc
    async def make_strategy(self, user_id: str, *, broker_account_id: str | None = None,
                            side: str = "BUY", quantity: int = 10) -> StrategyRecord:
        rec = StrategyRecord(
            user_id=user_id, name=f"strat-{uuid.uuid4().hex[:8]}",
            symbols_json=_json(["RELIANCE"]),
            conditions_json=_json([{"indicator": "PRICE", "operator": "gte", "value": 1}]),
            action_json=_json({"side": side, "quantity": quantity, "order_type": "MARKET",
                               "broker_account_id": broker_account_id or ""}),
            enabled=True, execution_mode="PAPER", broker_account_id=broker_account_id,
        )
        async with self.factory() as db:
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
            return rec
    async def make_config(self, user_id: str, *, name: str = "My Agent",
                          strategy_id: str | None = None, symbols: list[str] | None = None,
                          mode: str = MODE_PAPER, autonomy: int = AUTONOMY_OBSERVE,
                          approval_policy: dict | None = None,
                          risk_policy: dict | None = None) -> dict:
        return await self.service.create_config(
            user_id, name=name, strategy_id=strategy_id, symbols=symbols or ["RELIANCE"],
            execution_mode=mode, autonomy_level=autonomy,
            approval_policy=approval_policy or {"approval_required": True, "window_seconds": 600},
            risk_policy=risk_policy or {"max_position_size": 25, "max_orders_per_minute": 5},
        )
    async def set_config_status(self, config_id: str, status: str) -> None:
        async with self.factory() as db:
            row = await db.get(AgentConfigRecord, config_id)
            assert row is not None
            row.status = status
            await db.commit()
    async def enable_trading_agent(self, *, enabled: bool = True) -> None:
        async with self.factory() as db:
            row = (await db.execute(
                select(AgentRecord).where(AgentRecord.agent_type == "trading_agent")
            )).scalar_one()
            row.enabled = enabled
            row.capabilities_json = _json(["READ", "ANALYZE", "WRITE", "EXECUTE"])
            row.max_autonomy_level = 1
            await db.commit()
    async def set_autonomy(self, *, enabled: bool = True, level: int = 1) -> None:
        await ar.agent_runtime_default().update_runtime_config(
            autonomous_mode_enabled=enabled,
            global_autonomy_level=level,
            updated_by="test",
        )
    async def all_tasks(self, *, task_kind: str | None = None) -> list[dict]:
        stmt = select(AgentTaskRecord).order_by(AgentTaskRecord.created_at)
        if task_kind:
            stmt = stmt.where(AgentTaskRecord.task_kind == task_kind)
        async with self.factory() as db:
            rows = (await db.execute(stmt)).scalars().all()
            return [ar.task_to_dict(r) for r in rows]
    async def all_decisions(self) -> list[dict]:
        async with self.factory() as db:
            rows = (await db.execute(
                select(AgentDecisionRecord).order_by(AgentDecisionRecord.created_at)
            )).scalars().all()
            return [ac.decision_to_dict(r) for r in rows]
    @staticmethod
    def token_for(user: UserRecord) -> str:
        return create_access_token({"sub": user.id})
@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_file = tmp_path / "agent_control_test.db"
    # Isolate the shared rolling-indicator evaluator: every test environment
    # starts with empty price history (a process restart behaves identically).
    ac.reset_shared_evaluator()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.api.agents as agents_api
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(ar, "SessionLocal", factory)
    monkeypatch.setattr(ac, "SessionLocal", factory)
    monkeypatch.setattr(ai, "SessionLocal", factory)
    monkeypatch.setattr(agents_api, "SessionLocal", factory)
    created = await ar.ensure_agent_registry()
    assert "engineering_monitor" in created
    assert "trading_agent" in created
    control_env = ControlEnv(factory)
    await control_env.enable_trading_agent()
    await control_env.set_autonomy(enabled=True, level=1)
    from app.market_data.unified_manager import unified_market_manager
    monkeypatch.setattr(
        unified_market_manager, "get_quote", lambda sym: control_env.quote(sym)
    )
    yield control_env
    await engine.dispose()


@pytest.fixture
async def api_client(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _headers(env: ControlEnv, user: UserRecord) -> dict[str, str]:
    return {"Authorization": f"Bearer {env.token_for(user)}"}


async def _seeded_trader(env):
    user = await env.make_user()
    broker = await env.make_broker(user.id)
    strategy = await env.make_strategy(user.id, broker_account_id=broker.id)
    return user, broker, strategy
async def test_create_config_success_and_duplicate_rejected(env):
    user, broker, strategy = await _seeded_trader(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id)
    assert cfg["user_id"] == user.id
    assert cfg["status"] == "IDLE"
    assert cfg["autonomy_level"] == 0
    assert cfg["execution_mode"] == "PAPER"
    with pytest.raises(ac.AgentControlError) as exc_info:
        await env.make_config(user.id)
    assert exc_info.value.code == ac.ERR_CONFIG_EXISTS


async def test_create_config_validation_bounds(env):
    user = await env.make_user()
    with pytest.raises(ac.AgentControlError) as exc_info:
        await env.service.create_config(user.id, name="x", symbols=[], execution_mode="PAPER",
                                        autonomy_level=0, approval_policy={}, risk_policy={})
    assert exc_info.value.code == ac.ERR_BAD_SYMBOLS
    with pytest.raises(ac.AgentControlError) as exc_info:
        await env.service.create_config(user.id, name="x", symbols=["RELIANCE"],
                                        execution_mode="PAPER", autonomy_level=99,
                                        approval_policy={}, risk_policy={})
    assert exc_info.value.code == ac.ERR_BAD_AUTONOMY


async def test_create_config_live_refused_when_broker_not_live(env, monkeypatch):
    user, broker, strategy = await _seeded_trader(env)
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    with pytest.raises(ac.AgentControlError) as exc_info:
        await env.service.create_config(user.id, name="live", symbols=["RELIANCE"],
                                        execution_mode="LIVE", autonomy_level=2,
                                        approval_policy={"approval_required": False},
                                        risk_policy={"max_position_size": 1, "max_orders_per_minute": 1})
    assert exc_info.value.code == ac.ERR_BAD_MODE


async def test_create_config_strategy_ownership_enforced(env):
    user_a, _, _ = await _seeded_trader(env)
    user_b = await env.make_user()
    strategy_a = await env.make_strategy(user_a.id)
    with pytest.raises(ac.AgentControlError) as exc_info:
        await env.service.create_config(user_b.id, name="steal", strategy_id=strategy_a.id,
                                        symbols=["RELIANCE"], execution_mode="PAPER",
                                        autonomy_level=0, approval_policy={}, risk_policy={})
    assert exc_info.value.code == ac.ERR_STRATEGY_OWNERSHIP


async def test_patch_config_and_running_immutability(env):
    user, _, _ = await _seeded_trader(env)
    cfg = await env.make_config(user.id)
    patched = await env.service.patch_config(cfg["id"], user.id, name="Updated Name")
    assert patched["name"] == "Updated Name"
    await env.service.start(cfg["id"], user.id)
    with pytest.raises(ac.AgentControlError) as exc_info:
        await env.service.patch_config(cfg["id"], user.id, name="Nope")
    assert exc_info.value.code == ac.ERR_ILLEGAL_TRANSITION
async def test_lifecycle_cas_transitions(env):
    user, _, _ = await _seeded_trader(env)
    cfg = await env.make_config(user.id)
    result = await env.service.start(cfg["id"], user.id)
    assert result["ok"] and not result["idempotent"]
    assert result["config"]["status"] == "RUNNING"
    result = await env.service.pause(cfg["id"], user.id)
    assert result["config"]["status"] == "PAUSED"
    result = await env.service.resume(cfg["id"], user.id)
    assert result["config"]["status"] == "RUNNING"
    result = await env.service.stop(cfg["id"], user.id)
    assert result["config"]["status"] == "STOPPED"
    # illegal: pause() from PAUSED
    await env.service.start(cfg["id"], user.id)
    await env.service.pause(cfg["id"], user.id)
    with pytest.raises(ac.AgentControlError) as exc_info:
        await env.service.pause(cfg["id"], user.id)
    assert exc_info.value.code == ac.ERR_ILLEGAL_TRANSITION


async def test_start_is_idempotent(env):
    user, _, _ = await _seeded_trader(env)
    cfg = await env.make_config(user.id)
    await env.service.start(cfg["id"], user.id)
    result = await env.service.start(cfg["id"], user.id)
    assert result["idempotent"] is True
    assert result["config"]["status"] == "RUNNING"


async def test_config_tenant_isolation(env):
    user_a, _, _ = await _seeded_trader(env)
    user_b = await env.make_user()
    cfg_a = await env.make_config(user_a.id)
    with pytest.raises(ac.AgentControlError) as exc_info:
        await env.service.get_config_owned(cfg_a["id"], user_b.id)
    assert exc_info.value.code == ac.ERR_CONFIG_NOT_FOUND
    with pytest.raises(ac.AgentControlError):
        await env.service.start(cfg_a["id"], user_b.id)
    bundle_b = await env.service.bundle(user_b.id)
    assert bundle_b["config"] is None
    assert bundle_b["activity"]["decisions"] == []
    assert bundle_b["activity"]["tasks"] == []
async def test_evaluate_due_enqueues_single_idempotent_task(env):
    user, _, strategy = await _seeded_trader(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id, autonomy=AUTONOMY_PAPER)
    await env.set_config_status(cfg["id"], "RUNNING")
    enqueued = await env.service.evaluate_due()
    assert enqueued == 1
    tasks = await env.all_tasks(task_kind="evaluate_market")
    assert len(tasks) == 1
    assert tasks[0]["input"]["config_id"] == cfg["id"]
    enqueued2 = await env.service.evaluate_due()
    assert enqueued2 == 0
    assert len(await env.all_tasks(task_kind="evaluate_market")) == 1


async def test_evaluate_due_ignores_non_running_config(env):
    user, _, _ = await _seeded_trader(env)
    await env.make_config(user.id)
    enqueued = await env.service.evaluate_due()
    assert enqueued == 0
    assert len(await env.all_tasks(task_kind="evaluate_market")) == 0


async def test_evaluate_due_survives_dispatch_failure(env):
    user_a, _, strat_a = await _seeded_trader(env)
    user_b, _, strat_b = await _seeded_trader(env)
    cfg_a = await env.make_config(user_a.id, strategy_id=strat_a.id, autonomy=AUTONOMY_PAPER)
    await env.set_config_status(cfg_a["id"], "RUNNING")
    cfg_b = await env.make_config(user_b.id, strategy_id=strat_b.id, autonomy=AUTONOMY_PAPER)
    await env.set_config_status(cfg_b["id"], "RUNNING")
    await env.enable_trading_agent(enabled=False)
    enqueued = await env.service.evaluate_due()
    assert enqueued == 0


async def test_run_evaluation_no_quote_no_trade(env):
    user, _, strategy = await _seeded_trader(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id, autonomy=AUTONOMY_PAPER)
    await env.set_config_status(cfg["id"], "RUNNING")
    env.set_feed(available=False)
    decisions = await env.service.run_evaluation(
        config_id=cfg["id"], user_id=user.id, strategy_id=strategy.id,
        symbols=["RELIANCE"], autonomy_level=AUTONOMY_PAPER,
        approval_policy={"approval_required": False, "window_seconds": 600},
        risk_policy={"max_position_size": 25, "max_orders_per_minute": 5},
        requested_mode=MODE_PAPER, task_id="task-no-quote",
    )
    assert len(decisions) == 1
    assert decisions[0]["decision"] == DECISION_NO_TRADE
    assert "no market quote" in decisions[0]["reason"]


async def test_run_evaluation_observe_only_no_trade(env):
    user, _, strategy = await _seeded_trader(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id, autonomy=AUTONOMY_OBSERVE)
    await env.set_config_status(cfg["id"], "RUNNING")
    decisions = await env.service.run_evaluation(
        config_id=cfg["id"], user_id=user.id, strategy_id=strategy.id,
        symbols=["RELIANCE"], autonomy_level=AUTONOMY_OBSERVE,
        approval_policy={"approval_required": False, "window_seconds": 600},
        risk_policy={"max_position_size": 25, "max_orders_per_minute": 5},
        requested_mode=MODE_PAPER, task_id="task-observe",
    )
    assert len(decisions) == 1
    assert decisions[0]["decision"] == DECISION_NO_TRADE
    assert "observe-only" in decisions[0]["reason"]
async def test_run_evaluation_trade_enqueues_governed_task(env):
    user, broker, strategy = await _seeded_trader(env)
    cfg = await env.make_config(
        user.id, strategy_id=strategy.id, autonomy=AUTONOMY_PAPER,
        approval_policy={"approval_required": False, "window_seconds": 600},
    )
    await env.set_config_status(cfg["id"], "RUNNING")
    decisions = await env.service.run_evaluation(
        config_id=cfg["id"], user_id=user.id, strategy_id=strategy.id,
        symbols=["RELIANCE"], autonomy_level=AUTONOMY_PAPER,
        approval_policy={"approval_required": False, "window_seconds": 600},
        risk_policy={"max_position_size": 25, "max_orders_per_minute": 5},
        requested_mode=MODE_PAPER, task_id="task-trade",
    )
    d = decisions[0]
    assert d["decision"] == DECISION_TRADE
    assert d["side"] == "BUY"
    assert d["quantity"] == 10
    assert d["execution_result"] == "SENT"
    exec_tasks = [t for t in await env.all_tasks(task_kind="execute_trade")
                  if t["requested_by"] == user.id]
    assert len(exec_tasks) == 1
    assert exec_tasks[0]["created_by"] == "agent"
    assert exec_tasks[0]["requires_approval"] is False


async def test_run_evaluation_needs_approval_enqueues_approval_task(env):
    user, _, strategy = await _seeded_trader(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id, autonomy=AUTONOMY_PAPER)
    await env.set_config_status(cfg["id"], "RUNNING")
    decisions = await env.service.run_evaluation(
        config_id=cfg["id"], user_id=user.id, strategy_id=strategy.id,
        symbols=["RELIANCE"], autonomy_level=AUTONOMY_PAPER,
        approval_policy={"approval_required": True, "window_seconds": 600},
        risk_policy={"max_position_size": 25, "max_orders_per_minute": 5},
        requested_mode=MODE_PAPER, task_id="task-approval",
    )
    assert decisions[0]["decision"] == DECISION_NEEDS_APPROVAL
    assert decisions[0]["approval_result"] == "REQUIRED"
    exec_tasks = [t for t in await env.all_tasks(task_kind="execute_trade")
                  if t["requested_by"] == user.id]
    assert len(exec_tasks) == 1
    assert exec_tasks[0]["requires_approval"] is True


async def test_run_evaluation_config_not_running_no_trade(env):
    user, _, strategy = await _seeded_trader(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id, autonomy=AUTONOMY_PAPER)
    decisions = await env.service.run_evaluation(
        config_id=cfg["id"], user_id=user.id, strategy_id=strategy.id,
        symbols=["RELIANCE"], autonomy_level=AUTONOMY_PAPER,
        approval_policy={"approval_required": False, "window_seconds": 600},
        risk_policy={"max_position_size": 25, "max_orders_per_minute": 5},
        requested_mode=MODE_PAPER, task_id="task-idle",
    )
    assert decisions[0]["decision"] == DECISION_NO_TRADE
    assert "IDLE" in decisions[0]["reason"]
async def test_approve_task_owner_only(env):
    user_a, _, _ = await _seeded_trader(env)
    user_b = await env.make_user()
    task_dict, _ = await ar.agent_runtime_default().create_task(
        agent_type="trading_agent", task_kind="execute_trade",
        input_payload={"symbol": "RELIANCE"},
        created_by="user", requires_approval=True, requested_by=user_a.id,
    )
    task_id = task_dict["id"]
    with pytest.raises(ac.AgentControlError) as exc_info:
        await env.service.approve_task(task_id, user_b.id)
    assert exc_info.value.code == ac.ERR_TASK_NOT_APPROVABLE
    result = await env.service.approve_task(task_id, user_a.id)
    assert result["approved_by"] == user_a.id


async def test_link_back_sweep_records_intent_and_order(env):
    user, _, _ = await _seeded_trader(env)
    cfg = await env.make_config(user.id)
    await env.set_config_status(cfg["id"], "RUNNING")
    now = datetime.now(timezone.utc)
    decision_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    async with env.factory() as db:
        db.add(AgentTaskRecord(
            id=task_id, agent_type="trading_agent", task_kind="execute_trade",
            input_json="{}", status="SUCCEEDED", attempts=1, max_attempts=1,
            timeout_seconds=60.0, created_by="agent", requested_by=user.id,
            output_json='{"intent_id":"intent-1","order_id":"order-1"}',
            created_at=now, started_at=now, completed_at=now,
        ))
        db.add(AgentDecisionRecord(
            id=decision_id, agent_config_id=cfg["id"], user_id=user.id,
            task_id=task_id, decision=DECISION_TRADE, symbol="RELIANCE",
            mode=MODE_PAPER, execution_result="SENT",
            created_at=now, updated_at=now,
        ))
        await db.commit()
    updated = await env.service.link_back_sweep()
    assert updated == 1
    d = next(x for x in await env.all_decisions() if x["decision_id"] == decision_id)
    assert d["intent_id"] == "intent-1"
    assert d["order_id"] == "order-1"
    assert d["execution_result"] == "SUCCEEDED"


async def test_bundle_returns_each_task_once(env):
    user, _, _ = await _seeded_trader(env)
    cfg = await env.make_config(user.id)
    for i in range(3):
        await ar.agent_runtime_default().create_task(
            agent_type="trading_agent", task_kind="evaluate_market",
            input_payload={"config_id": cfg["id"], "slot": 999999 + i},
            created_by="user", requested_by=user.id,
        )
    bundle = await env.service.bundle(user.id)
    ids = [t["id"] for t in bundle["activity"]["tasks"]]
    assert len(ids) == 3
    assert len(set(ids)) == 3


async def test_scheduler_start_stop_idempotent(env):
    sched = ac.AgentEvaluationScheduler(service=env.service)
    sched.start()
    assert sched.is_running()
    sched.start()
    assert sched.is_running()
    sched.stop()
    assert not sched.is_running()
    sched.stop()
    assert not sched.is_running()
async def test_api_console_requires_auth(env, api_client):
    res = await api_client.get("/api/agent/console")
    assert res.status_code == 401


async def test_api_config_crud_lifecycle(env, api_client):
    user, broker, strategy = await _seeded_trader(env)
    headers = await _headers(env, user)
    res = await api_client.post(
        "/api/agent/config",
        json={"name": "Test Agent", "symbols": ["RELIANCE"],
              "execution_mode": "PAPER", "autonomy_level": 0,
              "strategy_id": strategy.id},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    cfg = res.json()
    assert cfg["status"] == "IDLE"
    res = await api_client.post(
        "/api/agent/config", json={"name": "Dup", "symbols": ["RELIANCE"]},
        headers=headers,
    )
    assert res.status_code == 409
    res = await api_client.get("/api/agent/console", headers=headers)
    assert res.status_code == 200
    assert res.json()["config"]["id"] == cfg["id"]
    res = await api_client.post("/api/agent/config/start", headers=headers)
    assert res.status_code == 200
    assert res.json()["config"]["status"] == "RUNNING"
    res = await api_client.patch(
        "/api/agent/config", json={"name": "Nope"}, headers=headers
    )
    assert res.status_code == 409
    res = await api_client.post("/api/agent/config/stop", headers=headers)
    assert res.status_code == 200
    assert res.json()["config"]["status"] == "STOPPED"
    user_b = await env.make_user()
    headers_b = await _headers(env, user_b)
    res = await api_client.post("/api/agent/config/start", headers=headers_b)
    assert res.status_code == 404


async def test_api_live_refused_400(env, api_client, monkeypatch):
    user, _, _ = await _seeded_trader(env)
    headers = await _headers(env, user)
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    res = await api_client.post(
        "/api/agent/config",
        json={"name": "Live Agent", "symbols": ["RELIANCE"], "execution_mode": "LIVE"},
        headers=headers,
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == ac.ERR_BAD_MODE


async def test_api_cross_tenant_patch_404(env, api_client):
    user_a, _, _ = await _seeded_trader(env)
    await env.make_config(user_a.id)
    user_b = await env.make_user()
    headers_b = await _headers(env, user_b)
    res = await api_client.patch(
        "/api/agent/config", json={"name": "Stolen"}, headers=headers_b
    )
    assert res.status_code == 404