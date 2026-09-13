"""Phase 1 Step 6+/Step 10 â€” deterministic autonomous-loop end-to-end tests.

Drives the FULL production PIPELINE through the existing services â€” no fake
parallel pipeline:

  fresh market event (unified cache)
    â†’ bounded scheduler slot enqueues ONE idempotent evaluate_market task
    â†’ AgentRuntime worker claims it
    â†’ deterministic decision row (feed-freshness + loop-safety guards)
    â†’ TRADE enqueues ONE idempotent execute_trade task (created_by=agent)
    â†’ AgentRuntime worker claims it (autonomy/approval gates re-checked)
    â†’ AgentIntentTriggerBridge â†’ AgentTradingService gates â†’ durable intent
    â†’ PAPER broker fill â†’ order + position + engine-simulated SL/TP
    â†’ link-back sweep records intent/order ids on the decision
    â†’ the agent observes the resulting state through the durable ledger

Failure/duplicate/restart contracts are asserted alongside:

  * duplicate same-slot re-delivery converges on the SAME task (no double work)
  * a later slot with the same matching market state is NO_TRADE while the
    position stays OPEN (loop-safety guard) â€” no duplicate order
  * replaying an executed intent is idempotent â€” no second broker dispatch
  * restart/recovery preserves durable state and re-enqueues converge
  * autonomous-mode-off fails closed at claim time (no intent, no order)
  * STALE/UNAVAILABLE feed produces an honest NO_TRADE decision record
  * indicator history persists across slots (SMA fires on slot 2)
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.session as db_session
import app.engine.agent_control as ac
import app.engine.agent_intents as ai
import app.engine.agent_runtime as ar
import app.engine.durable_claims as dc
import app.engine.agent_intent_triggers  # noqa: F401  (registers execute_trade handler)
from app.core.security import create_access_token, hash_password
from app.db.session import Base
from app.models.agent import AgentRecord, AgentTaskRecord
from app.models.agent_control import (
    AUTONOMY_PAPER,
    DECISION_NO_TRADE,
    DECISION_TRADE,
    MODE_PAPER,
    AgentConfigRecord,
    AgentDecisionRecord,
)
from app.models.agent_intent import TradingIntentRecord
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, StrategyRecord
from app.models.user import UserRecord

# Register every table the shared metadata must hold (hermetic create_all()).
import app.models.audit  # noqa: F401
import app.models.broker_state  # noqa: F401
import app.models.protective_order  # noqa: F401

FEED_PRICE = 250.0
FILL_PRICE = 251.5


def _json(obj) -> str:  # noqa: ANN001
    return json.dumps(obj, separators=(",", ":"), default=str)


class DeterministicBroker:
    """Instant-fill broker adapter (the ONLY broker the pipeline may reach)."""

    def __init__(self) -> None:
        self.placed: list = []
        self.fill_price = FILL_PRICE

    async def place_order(self, order):  # noqa: ANN001, ANN201
        self.placed.append(order)
        return {
            "broker_order_id": f"E2E-{len(self.placed):04d}",
            "filled_price": self.fill_price,
        }


class E2EEnv:
    """Facade over one hermetic autonomous-loop database."""

    def __init__(self, factory, broker: DeterministicBroker) -> None:
        self.factory = factory
        self.broker = broker
        self.feed_price = FEED_PRICE
        self.feed_status = "LIVE"
        self.feed_age = 0.0
        self.feed_stale = False
        self.service = ac.agent_control_service
        self.trading_service = ai.AgentTradingService()
        self.latest_user_id: str = ""
        self.latest_strategy_id: str = ""

    def set_feed(
        self,
        *,
        status: str | None = None,
        price: float | None = None,
        age: float | None = None,
        stale: bool | None = None,
    ) -> None:
        if status is not None:
            self.feed_status = status
        if price is not None:
            self.feed_price = price
        if age is not None:
            self.feed_age = age
        if stale is not None:
            self.feed_stale = stale

    def quote(self, symbol: str) -> dict:
        return {
            "symbol": symbol.upper(),
            "price": self.feed_price,
            "ltp": self.feed_price,
            "data_status": self.feed_status,
            "is_stale": self.feed_stale,
            "age_seconds": self.feed_age,
        }

    async def make_user(self, *, role: str = "trader") -> UserRecord:
        user = UserRecord(
            email=f"{uuid.uuid4().hex[:12]}@e2e.test",
            hashed_password=hash_password("E2EPass123!"),
            full_name="Autonomous E2E Tester",
            role=role,
            kyc_status="VERIFIED",
            is_verified=True,
            is_active=True,
            paper_balance=1_000_000.0,
        )
        async with self.factory() as db:
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    async def make_broker(self, user_id: str) -> BrokerAccountRecord:
        acc = BrokerAccountRecord(
            user_id=user_id, broker_name="SIMULATED", account_name="Autonomous E2E"
        )
        acc.set_api_key("test-key")
        async with self.factory() as db:
            db.add(acc)
            await db.commit()
            await db.refresh(acc)
            return acc

    async def make_strategy(
        self,
        user_id: str,
        *,
        broker_account_id: str,
        conditions: list[dict] | None = None,
        side: str = "BUY",
        quantity: int = 10,
    ) -> StrategyRecord:
        rec = StrategyRecord(
            user_id=user_id,
            name=f"e2e-strat-{uuid.uuid4().hex[:8]}",
            symbols_json=_json(["RELIANCE"]),
            conditions_json=_json(
                conditions
                or [{"indicator": "PRICE", "operator": "gte", "value": 1}]
            ),
            action_json=_json({
                "side": side,
                "quantity": quantity,
                "order_type": "MARKET",
                "broker_account_id": broker_account_id,
                "stop_loss": 245.0,
                "take_profit": 260.0,
            }),
            enabled=True,
        )
        async with self.factory() as db:
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
            return rec

    async def make_config(
        self,
        user_id: str,
        *,
        strategy_id: str | None = None,
        autonomy: int = AUTONOMY_PAPER,
        approval_policy: dict | None = None,
        risk_policy: dict | None = None,
    ) -> dict:
        return await self.service.create_config(
            user_id,
            name="Autonomous E2E Agent",
            strategy_id=strategy_id,
            symbols=["RELIANCE"],
            execution_mode=MODE_PAPER,
            autonomy_level=autonomy,
            approval_policy=approval_policy
            or {"approval_required": False, "window_seconds": 600},
            risk_policy=risk_policy
            or {"max_position_size": 25, "max_orders_per_minute": 5, "max_open_positions": 1},
        )

    async def set_config_status(self, config_id: str, status: str) -> None:
        async with self.factory() as db:
            row = await db.get(AgentConfigRecord, config_id)
            assert row is not None
            row.status = status
            await db.commit()

    async def enable_trading_agent(self, *, enabled: bool = True) -> None:
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(AgentRecord).where(AgentRecord.agent_type == "trading_agent")
                )
            ).scalar_one()
            row.enabled = enabled
            row.capabilities_json = _json(["READ", "ANALYZE", "WRITE", "EXECUTE"])
            row.max_autonomy_level = 1
            await db.commit()

    async def set_autonomy(self, *, enabled: bool = True, level: int = 2) -> None:
        await ar.agent_runtime_default().update_runtime_config(
            autonomous_mode_enabled=enabled,
            global_autonomy_level=level,
            updated_by="test",
        )

    async def track_owner(self, user_id: str, strategy_id: str) -> None:
        """Bind the tenant context used by ``evaluate_slot``."""
        self.latest_user_id = user_id
        self.latest_strategy_id = strategy_id

    async def evaluate_slot(self, config_id: str, task_id: str) -> list[dict]:
        return await self.service.run_evaluation(
            config_id=config_id,
            user_id=self.latest_user_id,
            strategy_id=self.latest_strategy_id,
            symbols=["RELIANCE"],
            autonomy_level=AUTONOMY_PAPER,
            approval_policy={"approval_required": False, "window_seconds": 600},
            risk_policy={"max_position_size": 25, "max_orders_per_minute": 5, "max_open_positions": 1},
            requested_mode=MODE_PAPER,
            task_id=task_id,
        )

    async def tasks_of(self, task_kind: str) -> list[dict]:
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(AgentTaskRecord)
                    .where(AgentTaskRecord.task_kind == task_kind)
                    .order_by(AgentTaskRecord.created_at, AgentTaskRecord.id)
                )
            ).scalars().all()
            return [ar.task_to_dict(r) for r in rows]

    async def task_count(self, task_kind: str) -> int:
        return len(await self.tasks_of(task_kind))

    async def all_decisions(self) -> list[dict]:
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(AgentDecisionRecord).order_by(AgentDecisionRecord.created_at)
                )
            ).scalars().all()
            return [ac.decision_to_dict(r) for r in rows]

    async def intent_record(self, intent_id: str) -> TradingIntentRecord:
        async with self.factory() as db:
            row = await db.get(TradingIntentRecord, intent_id)
            assert row is not None
            return row

    async def order_count(self, user_id: str) -> int:
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(OrderRecord).where(OrderRecord.user_id == user_id)
                )
            ).scalars().all()
            return len(rows)

    @staticmethod
    def token_for(user: UserRecord) -> str:
        return create_access_token({"sub": user.id})


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """Hermetic SQLite DB wired into every service ``SessionLocal``."""
    db_file = tmp_path / "autonomous_e2e.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Isolate the shared rolling-indicator evaluator per test environment.
    ac.reset_shared_evaluator()
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(ar, "SessionLocal", factory)
    monkeypatch.setattr(ac, "SessionLocal", factory)
    monkeypatch.setattr(ai, "SessionLocal", factory)
    monkeypatch.setattr(dc, "SessionLocal", factory)

    created = await ar.ensure_agent_registry()
    assert "engineering_monitor" in created
    assert "trading_agent" in created

    e2e_env = E2EEnv(factory, DeterministicBroker())
    await e2e_env.enable_trading_agent()
    await e2e_env.set_autonomy(enabled=True, level=2)

    # Deterministic risk source (never carry kill-switch state across tests).
    ai.set_engine_risk_getter(lambda: ai.RiskManager())
    monkeypatch.setattr(ai, "_fallback_risk_manager", ai.RiskManager())
    monkeypatch.setattr(
        "app.brokers.get_broker_adapter",
        lambda *a, **k: e2e_env.broker,
    )
    from app.market_data.unified_manager import unified_market_manager

    monkeypatch.setattr(
        unified_market_manager, "get_quote", lambda sym: e2e_env.quote(sym)
    )
    yield e2e_env
    await engine.dispose()


async def _seeded(env: E2EEnv, **strategy_kwargs):
    user = await env.make_user()
    broker = await env.make_broker(user.id)
    strategy = await env.make_strategy(
        user.id, broker_account_id=str(broker.id), **strategy_kwargs
    )
    await env.track_owner(user.id, strategy.id)
    return user, broker, strategy


async def _run_full_loop(env: E2EEnv, cfg: dict):
    """evaluate_due â†’ runtime eval pass â†’ runtime execute pass â†’ link-back."""
    assert await env.service.evaluate_due() == 1
    first = await ar.agent_runtime_default().run_once()
    assert first["succeeded"] == 1
    second = await ar.agent_runtime_default().run_once()
    assert second["succeeded"] == 1
    await env.service.link_back_sweep()
    return (await env.all_decisions())[0]


# â”€â”€ Step 10: the one deterministic end-to-end autonomous PAPER loop â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.asyncio
async def test_paper_autonomous_full_loop(env: E2EEnv):
    user, _, strategy = await _seeded(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id)
    await env.set_config_status(cfg["id"], "RUNNING")

    # 1-2. Fresh market event is present (env.quote LIVE); the bounded slot
    #      enqueues exactly ONE idempotent evaluation task.  A duplicate slot
    #      re-delivery converges â€” never a second task.
    assert await env.task_count("evaluate_market") == 0
    assert await env.service.evaluate_due() == 1
    assert await env.task_count("evaluate_market") == 1
    assert await env.service.evaluate_due() == 0
    assert await env.task_count("evaluate_market") == 1

    # 3-5. Worker claims the evaluation task â†’ deterministic decision â†’
    #      governed execute_trade task (created_by=agent).
    first = await ar.agent_runtime_default().run_once()
    assert first["succeeded"] == 1
    decisions = await env.all_decisions()
    assert len(decisions) == 1
    assert decisions[0]["decision"] == DECISION_TRADE
    assert decisions[0]["execution_result"] == "SENT"
    assert await env.task_count("execute_trade") == 1

    # 6-9. Worker claims the execute_trade task; the runtime autonomy gate,
    #      the trigger bridge and every AgentTradingService gate pass; the
    #      PAPER order fills through the deterministic broker.
    second = await ar.agent_runtime_default().run_once()
    assert second["succeeded"] == 1
    exec_tasks = await env.tasks_of("execute_trade")
    assert len(exec_tasks) == 1
    assert exec_tasks[0]["status"] == "SUCCEEDED"
    assert exec_tasks[0]["output"]["intent_id"]
    assert exec_tasks[0]["output"]["order_id"]

    # 13. Link-back sweep records the terminal ids on the decision row.
    updated = await env.service.link_back_sweep()
    assert updated == 1
    decision = (await env.all_decisions())[0]
    assert decision["intent_id"] == exec_tasks[0]["output"]["intent_id"]
    assert decision["order_id"] == exec_tasks[0]["output"]["order_id"]
    assert decision["execution_result"] == "SUCCEEDED"

    # 10-12. Fill â†’ position â†’ engine-simulated protective SL/TP (PAPER).
    intent = await env.intent_record(decision["intent_id"])
    assert intent.status == "EXECUTED"
    async with env.factory() as db:
        order = await db.get(OrderRecord, intent.order_id)
        pos = await db.get(PositionRecord, intent.position_id)
    assert order is not None and order.status == "FILLED"
    assert order.agent_intent_id == intent.id
    assert pos is not None and pos.status == "OPEN"
    assert pos.mode == "PAPER"
    assert float(pos.entry_price) == FILL_PRICE
    assert float(pos.stop_loss_price) == 245.0
    assert float(pos.take_profit_price) == 260.0
    assert pos.protection_state == "PAPER"
    assert len(env.broker.placed) == 1

    # 14. The agent observes the resulting state through the same durable rows.
    bundle = await env.service.bundle(user.id)
    assert bundle["activity"]["intents"] and bundle["activity"]["positions"]
    assert all(p["status"] == "OPEN" for p in bundle["activity"]["positions"])


# â”€â”€ Step 5/10: duplicate events and replay must never produce a 2nd order â”€â”€

@pytest.mark.asyncio
async def test_duplicate_market_event_does_not_duplicate_order(env: E2EEnv):
    user, _, strategy = await _seeded(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id)
    await env.set_config_status(cfg["id"], "RUNNING")

    decision = await _run_full_loop(env, cfg)
    intent_id = decision["intent_id"]
    assert await env.order_count(user.id) == 1

    # Same-slot market re-delivery: idempotency key converges â€” 0 new tasks.
    assert await env.service.evaluate_due() == 0
    assert await env.task_count("evaluate_market") == 1
    assert await env.task_count("execute_trade") == 1

    # A later slot with the SAME matching market state while the position is
    # OPEN: the loop-safety guard records an honest NO_TRADE â€” no 2nd order.
    later = await env.evaluate_slot(cfg["id"], task_id="task-later-slot")
    assert later[0]["decision"] == DECISION_NO_TRADE
    assert later[0]["risk_result"] == "OPEN_POSITION_LIMIT"
    assert await env.task_count("execute_trade") == 1

    # Replaying the executed intent is idempotent â€” no second broker dispatch.
    replay = await env.trading_service.execute_intent(intent_id)
    assert replay["ok"] is True and replay["idempotent"] is True
    assert await env.order_count(user.id) == 1
    assert len(env.broker.placed) == 1


# â”€â”€ Step 10/16: restart + recovery preserves durable state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.asyncio
async def test_restart_recovery_preserves_state(env: E2EEnv):
    user, _, strategy = await _seeded(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id)
    await env.set_config_status(cfg["id"], "RUNNING")

    decision = await _run_full_loop(env, cfg)
    decision_id = decision["decision_id"]
    intent_id = decision["intent_id"]
    assert await env.order_count(user.id) == 1

    # Durable rows survive: decision + evaluated task + executed task + intent.
    assert len(await env.all_decisions()) == 1
    assert await env.task_count("evaluate_market") == 1
    assert await env.task_count("execute_trade") == 1
    intent = await env.intent_record(intent_id)
    assert intent.status == "EXECUTED"

    # A restart spins a FRESH runtime consumer: it sees the same durable queue
    # and claims nothing (everything terminal) â€” no duplicate work on restart.
    fresh_runtime = ar.AgentRuntime()
    summary = await fresh_runtime.run_once()
    assert summary["claimed"] == 0 and summary["succeeded"] == 0

    # Re-enqueuing with the SAME slot idempotency keys converges on the same
    # durable tasks (created=False) â€” a recovered loop can never double-run.
    assert await env.service.evaluate_due() == 0
    _task, created = await ar.agent_runtime_default().create_task(
        agent_type="trading_agent",
        task_kind="execute_trade",
        input_payload={},
        created_by="agent",
        requires_approval=False,
        idempotency_key=f"cfg:{cfg['id']}:dec:{decision_id}",
        requested_by=user.id,
    )
    assert created is False

    # Exactly one order ever reached the broker.
    assert await env.order_count(user.id) == 1
    assert len(env.broker.placed) == 1


# â”€â”€ Step 4: autonomous mode OFF fails closed at claim time â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.asyncio
async def test_autonomous_mode_off_blocks_execution_fail_closed(env: E2EEnv):
    user, _, strategy = await _seeded(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id)
    await env.set_config_status(cfg["id"], "RUNNING")
    await env.set_autonomy(enabled=False, level=2)

    # Evaluation is still permitted (user-created task) â†’ the decision is
    # recorded durably (safe observed state), and the governed execute_trade
    # task is enqueued but MUST fail at the runtime autonomy gate.
    assert await env.service.evaluate_due() == 1
    first = await ar.agent_runtime_default().run_once()
    assert first["succeeded"] == 1
    assert (await env.all_decisions())[0]["decision"] == DECISION_TRADE
    assert await env.task_count("execute_trade") == 1

    second = await ar.agent_runtime_default().run_once()
    assert second["failed"] == 1
    exec_tasks = await env.tasks_of("execute_trade")
    assert exec_tasks[0]["status"] == "FAILED"
    assert exec_tasks[0]["error"]["code"] == "AUTONOMOUS_MODE_DISABLED"
    assert await env.order_count(user.id) == 0
    assert len(env.broker.placed) == 0


# â”€â”€ Step 11: stale / unavailable feed â†’ honest NO_TRADE decision â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.asyncio
async def test_stale_and_unavailable_feed_decisions_fail_closed(env: E2EEnv):
    user, _, strategy = await _seeded(env)
    cfg = await env.make_config(user.id, strategy_id=strategy.id)
    await env.set_config_status(cfg["id"], "RUNNING")

    env.set_feed(status="STALE", stale=True, age=120.0)
    stale = await env.evaluate_slot(cfg["id"], task_id="task-stale")
    assert stale[0]["decision"] == DECISION_NO_TRADE
    assert stale[0]["risk_result"] == "FEED_STALE"
    assert "STALE" in stale[0]["reason"]
    assert await env.task_count("execute_trade") == 0

    env.set_feed(status="UNAVAILABLE", age=None, stale=False)
    unavailable = await env.evaluate_slot(cfg["id"], task_id="task-unavailable")
    assert unavailable[0]["decision"] == DECISION_NO_TRADE
    assert unavailable[0]["risk_result"] == "FEED_UNAVAILABLE"


# â”€â”€ Gap-fix: rolling indicator history persists across slots â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.asyncio
async def test_indicator_history_persists_across_slots(env: E2EEnv):
    user, broker, strategy = await _seeded(
        env,
        conditions=[{"indicator": "SMA", "operator": "gte", "value": 150, "period": 2}],
    )
    cfg = await env.make_config(user.id, strategy_id=strategy.id)
    await env.set_config_status(cfg["id"], "RUNNING")

    # Slot 1: history [100] â†’ SMA(2) is not computable yet â†’ NO_TRADE.
    env.set_feed(price=100.0)
    slot1 = await env.evaluate_slot(cfg["id"], task_id="slot-1")
    assert slot1[0]["decision"] == DECISION_NO_TRADE
    assert await env.task_count("execute_trade") == 0

    # Slot 2: history [100, 200] â†’ SMA(2)=150 gte 150 â†’ TRADE (the guarded
    # decision fires because the evaluator kept its rolling history).
    env.set_feed(price=200.0)
    slot2 = await env.evaluate_slot(cfg["id"], task_id="slot-2")
    assert slot2[0]["decision"] == DECISION_TRADE
    assert await env.task_count("execute_trade") == 1

