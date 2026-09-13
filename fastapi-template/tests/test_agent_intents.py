"""Phase 1 Step 3 — governed agent trading intents: end-to-end tests.

Every test runs against a throwaway SQLite file (never the dev ``trading.db``).
The ``env`` fixture builds a fresh engine, ``create_all()`` s the schema,
wires the temp session factory into every ``SessionLocal`` consumer
(``app.db.session``, ``app.engine.agent_intents``,
``app.engine.durable_claims``), installs a
deterministic fake broker + quote feed, and yields helpers.

Coverage (fail-closed governed-pipeline contract):
  decision contract .. NO_TRADE / REJECTED / invalid→FAILED are journaled
                        with intent_id=None; TRADE creates a durable intent
  schema gates ....... quantity cap, unknown/cross-tenant broker
  feed freshness ..... STALE feed ⇒ NO_TRADE (fail closed, never a guess)
  LIVE fail-closed ... requested_mode=LIVE while BROKER_MODE=simulated
                        ⇒ REJECTED (never a silent PAPER fallback)
  execution CAS ...... CREATED→SENT single-winner; re-entry is idempotent and
                        never double-dispatches
  approval ........... NEEDS_APPROVAL without approval ⇒ REJECTED at execute;
                        approved ⇒ EXECUTED
  risk gate .......... engine RiskManager consulted at evaluate AND execute
  broker failure ..... dispatch failure ⇒ claim REJECTED + intent FAILED
  lifecycle .......... PAPER TRADE ⇒ order + position + PAPER protection;
                        close ⇒ CAS + owner-scoped paper P&L + intent CLOSED;
                        replay never double-credits
  HTTP envelope ..... admin-gated auth (401/403/200)
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.session as db_session
import app.engine.agent_intents as ai
import app.engine.durable_claims as dc
from app.core.security import create_access_token, hash_password
from app.db.session import Base
from app.models.agent_intent import TradingIntentRecord
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord
from app.models.user import UserRecord

# Register every table the shared metadata must hold (hermetic create_all()).
import app.models.audit  # noqa: F401
import app.models.broker_state  # noqa: F401
import app.models.protective_order  # noqa: F401
# NOTE: keep this import last - ``import app.models.*`` rebinds ``app``.
from app.main import app  # noqa: E402

FEED_PRICE = 250.0
FILL_PRICE = 251.5


class FakeBroker:
    """Deterministic broker adapter: instant fill, optional failure."""

    def __init__(self) -> None:
        self.placed: list = []
        self.fill_price = FILL_PRICE
        self.fail_next = False

    async def place_order(self, order):  # noqa: ANN001, ANN201
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated broker outage")
        self.placed.append(order)
        return {
            "broker_order_id": f"FAKE-{len(self.placed):04d}",
            "filled_price": self.fill_price,
        }


class _FailingRisk:
    """Risk stub that blocks everything (kill-switch on)."""

    def check(self, order):  # noqa: ANN001, ANN201
        return False, "kill switch on"


class IntentEnv:
    """Facade over one throwaway governed-pipeline database."""

    def __init__(self, factory, broker: FakeBroker) -> None:
        self.factory = factory
        self.broker = broker
        self.feed_price = FEED_PRICE
        self.feed_status = "LIVE"
        self.feed_age = 0.0
        self.feed_stale = False
        self.service = ai.AgentTradingService()

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
            "data_status": self.feed_status,
            "is_stale": self.feed_stale,
            "age_seconds": self.feed_age,
        }

    async def make_user(
        self, *, role: str = "trader", balance: float = 1_000_000.0
    ) -> UserRecord:
        user = UserRecord(
            email=f"{uuid.uuid4().hex[:12]}@intent.test",
            hashed_password=hash_password("IntentPass123!"),
            full_name="Intent Tester",
            role=role,
            kyc_status="VERIFIED",
            is_verified=True,
            is_active=True,
            paper_balance=balance,
        )
        async with self.factory() as db:
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    async def make_broker(
        self, user_id: str, *, broker_name: str = "SIMULATED"
    ) -> BrokerAccountRecord:
        acc = BrokerAccountRecord(
            user_id=user_id,
            broker_name=broker_name,
            account_name="Agent Intent Test",
        )
        acc.set_api_key("test-key")
        async with self.factory() as db:
            db.add(acc)
            await db.commit()
            await db.refresh(acc)
            return acc

    async def evaluate(
        self,
        user_id: str,
        broker_account_id: str,
        *,
        decision: str = ai.DECISION_TRADE,
        symbol: str = "RELIANCE",
        side: str = "BUY",
        quantity: int = 10,
        order_type: str = "MARKET",
        limit_price: float | None = None,
        trigger_price: float | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        requested_mode: str = "PAPER",
        approval_required: bool = False,
        agent_task_id: str | None = None,
    ) -> dict:
        return await self.service.evaluate(
            agent_task_id=agent_task_id or f"task_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            broker_account_id=broker_account_id,
            strategy_id=None,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            trigger_price=trigger_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            confidence=0.8,
            reason="test intent",
            requested_mode=requested_mode,
            decision=decision,
            approval_required=approval_required,
        )

    async def get_intent(self, intent_id: str) -> dict:
        return await self.service.get_intent(intent_id)

    async def grant_approval(self, intent_id: str, approver_id: str) -> None:
        from datetime import datetime, timezone

        async with self.factory() as db:
            row = await db.get(TradingIntentRecord, intent_id)
            assert row is not None
            row.approved_by = approver_id
            row.approved_at = datetime.now(timezone.utc)
            await db.commit()

    async def order_count_for(self, intent_id: str) -> int:
        key = ai.intent_order_key(intent_id)
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(OrderRecord).where(OrderRecord.client_order_id == key)
                )
            ).scalars().all()
            return len(rows)

    async def balance(self, user_id: str) -> float:
        async with self.factory() as db:
            user = await db.get(UserRecord, user_id)
            return float(user.paper_balance) if user else -1.0

    @staticmethod
    def token_for(user: UserRecord) -> str:
        return create_access_token({"sub": user.id})


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """Hermetic SQLite DB wired into every service/API ``SessionLocal``."""
    db_file = tmp_path / "intent_test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(ai, "SessionLocal", factory)
    monkeypatch.setattr(dc, "SessionLocal", factory)
    # Clean risk source per environment (never carry kill-switch state).
    ai.set_engine_risk_getter(lambda: ai.RiskManager())
    monkeypatch.setattr(ai, "_fallback_risk_manager", ai.RiskManager())
    broker = FakeBroker()
    monkeypatch.setattr(
        "app.brokers.get_broker_adapter",
        lambda *a, **k: broker,
    )
    from app.market_data.unified_manager import unified_market_manager

    intent_env = IntentEnv(factory, broker)
    monkeypatch.setattr(
        unified_market_manager, "get_quote", lambda sym: intent_env.quote(sym)
    )
    yield intent_env
    await engine.dispose()


@pytest.fixture
async def api_client(env):
    """Async HTTP client against the real FastAPI app, temp-DB backed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── Helpers ─────────────────────────────────────────────────────────────

async def _seed_paper_trader(
    env: IntentEnv,
) -> tuple[UserRecord, BrokerAccountRecord]:
    user = await env.make_user()
    broker_acc = await env.make_broker(user.id)
    return user, broker_acc


async def _audit_actions(env: IntentEnv) -> list[str]:
    from app.models.audit import AuditLogRecord

    async with env.factory() as db:
        rows = (
            await db.execute(
                select(AuditLogRecord).order_by(AuditLogRecord.created_at)
            )
        ).scalars().all()
        return [r.action for r in rows]


async def _order_status_by_key(env: IntentEnv, intent_id: str) -> str | None:
    key = ai.intent_order_key(intent_id)
    async with env.factory() as db:
        row = (
            await db.execute(
                select(OrderRecord).where(OrderRecord.client_order_id == key)
            )
        ).scalar_one_or_none()
        return row.status if row else None


async def _open_position(env: IntentEnv, user_id: str, broker_account_id: str) -> str:
    """Evaluate a TRADE intent and execute it; returns intent_id."""
    result = await env.evaluate(user_id, broker_account_id)
    intent_id = result["intent_id"]
    assert intent_id is not None
    outcome = await env.service.execute_intent(intent_id)
    assert outcome["ok"] is True, outcome
    return intent_id


# ── Decision contract + gates ───────────────────────────────────────────

async def test_evaluate_no_trade_decision_journaled(env):
    user, broker_acc = await _seed_paper_trader(env)
    out = await env.evaluate(user.id, broker_acc.id, decision=ai.DECISION_NO_TRADE)
    assert out["decision"] == "NO_TRADE"
    assert out["intent_id"] is None
    assert "agent.intent.decision_no_trade" in await _audit_actions(env)
    assert env.broker.placed == []


async def test_evaluate_rejected_decision_journaled(env):
    user, broker_acc = await _seed_paper_trader(env)
    out = await env.evaluate(user.id, broker_acc.id, decision=ai.DECISION_REJECTED)
    assert out["decision"] == "REJECTED"
    assert out["intent_id"] is None
    assert "agent.intent.decision_rejected" in await _audit_actions(env)
    assert env.broker.placed == []


async def test_evaluate_invalid_decision_fails_closed(env):
    user, broker_acc = await _seed_paper_trader(env)
    out = await env.evaluate(user.id, broker_acc.id, decision="MAYBE")
    assert out["decision"] == "FAILED"
    assert out["intent_id"] is None
    actions = await _audit_actions(env)
    assert any("decision_failed" in a for a in actions)
    assert env.broker.placed == []


async def test_evaluate_stale_feed_no_trade(env):
    user, broker_acc = await _seed_paper_trader(env)
    env.set_feed(status="STALE", stale=True, age=120)
    out = await env.evaluate(user.id, broker_acc.id, decision=ai.DECISION_TRADE)
    assert out["decision"] == "NO_TRADE"
    assert out["intent_id"] is None
    assert "STALE" in out["reason"]
    assert env.broker.placed == []


async def test_evaluate_unavailable_feed_no_trade(env):
    user, broker_acc = await _seed_paper_trader(env)
    env.set_feed(status="UNAVAILABLE")
    out = await env.evaluate(user.id, broker_acc.id)
    assert out["decision"] == "NO_TRADE"
    assert out["intent_id"] is None
    assert "UNAVAILABLE" in out["reason"]


async def test_evaluate_quantity_limit_gate(env):
    user, broker_acc = await _seed_paper_trader(env)
    with pytest.raises(ai.IntentGateError, match="quantity"):
        await env.evaluate(user.id, broker_acc.id, quantity=10**12)


async def test_evaluate_broker_unknown_and_not_owner_gates(env):
    user, broker_acc = await _seed_paper_trader(env)
    with pytest.raises(ai.IntentGateError) as ei:
        await env.evaluate(user.id, "brk_missing", quantity=5)
    assert ei.value.code == "broker_unknown"
    other = await env.make_user()
    with pytest.raises(ai.IntentGateError) as ei:
        await env.evaluate(other.id, broker_acc.id, quantity=5)
    assert ei.value.code == "broker_not_owner"


async def test_evaluate_insufficient_paper_margin(env):
    poor = await env.make_user(balance=100.0)
    broker_poor = await env.make_broker(poor.id)
    out = await env.evaluate(poor.id, broker_poor.id, quantity=100)
    assert out["decision"] == "REJECTED"
    assert "margin" in out["reason"]
    assert env.broker.placed == []


async def test_evaluate_risk_gate_rejected(env):
    user, broker_acc = await _seed_paper_trader(env)
    ai.set_engine_risk_getter(lambda: _FailingRisk())
    out = await env.evaluate(user.id, broker_acc.id)
    assert out["decision"] == "REJECTED"
    assert "kill switch" in out["reason"]
    assert env.broker.placed == []


async def test_evaluate_live_mode_rejected_when_broker_simulated(env):
    user, broker_acc = await _seed_paper_trader(env)
    out = await env.evaluate(user.id, broker_acc.id, requested_mode="LIVE")
    assert out["decision"] == "REJECTED"
    assert out["intent_id"] is None
    assert "live dispatch disallowed" in out["reason"]
    assert env.broker.placed == []


async def test_evaluate_same_task_is_idempotent(env):
    user, broker_acc = await _seed_paper_trader(env)
    task = f"task_{uuid.uuid4().hex[:12]}"
    r1 = await env.evaluate(user.id, broker_acc.id, agent_task_id=task)
    r2 = await env.evaluate(user.id, broker_acc.id, agent_task_id=task)
    assert r1["intent_id"] == r2["intent_id"]
    assert r1["status"] == "CREATED"
    assert await env.get_intent(r1["intent_id"]) is not None
    assert env.broker.placed == []  # evaluate never dispatches


# ── Execution: claim CAS, idempotency, gates at execute ─────────────────

async def test_execute_single_dispatch_and_durable_idempotency(env):
    user, broker_acc = await _seed_paper_trader(env)
    result = await env.evaluate(user.id, broker_acc.id)
    intent_id = result["intent_id"]
    first = await env.service.execute_intent(intent_id)
    assert first["ok"] is True
    assert first["order_id"] and first["position_id"]
    assert len(env.broker.placed) == 1
    assert await env.order_count_for(intent_id) == 1

    # Replay: already EXECUTED → idempotent, no second dispatch.
    replay = await env.service.execute_intent(intent_id)
    assert replay["ok"] is True and replay["idempotent"] is True
    assert len(env.broker.placed) == 1

    # Crash simulation: a worker lost its intent-status write; the durable
    # claim (FILLED) must still prevent a second broker dispatch.
    async with env.factory() as db:
        await db.execute(
            update(TradingIntentRecord)
            .where(TradingIntentRecord.id == intent_id)
            .values(status="CREATED")
        )
        await db.commit()
    again = await env.service.execute_intent(intent_id)
    assert again["ok"] is True and again["idempotent"] is True
    assert "claim FILLED" in again["reason"]
    assert len(env.broker.placed) == 1
    assert await env.order_count_for(intent_id) == 1


async def test_execute_concurrent_cas_single_winner(env):
    user, broker_acc = await _seed_paper_trader(env)
    result = await env.evaluate(user.id, broker_acc.id)
    intent_id = result["intent_id"]
    outcomes = await asyncio.gather(
        env.service.execute_intent(intent_id),
        env.service.execute_intent(intent_id),
        env.service.execute_intent(intent_id),
    )
    assert all(o["ok"] for o in outcomes)
    assert any(o.get("idempotent") for o in outcomes)
    assert len(env.broker.placed) == 1
    intent = await env.get_intent(intent_id)
    assert intent["status"] == "EXECUTED"


async def test_execute_approval_required_gates_rejection(env):
    user, broker_acc = await _seed_paper_trader(env)
    result = await env.evaluate(
        user.id,
        broker_acc.id,
        decision=ai.DECISION_NEEDS_APPROVAL,
        approval_required=True,
    )
    assert result["decision"] == "NEEDS_APPROVAL"
    intent_id = result["intent_id"]
    out = await env.service.execute_intent(intent_id)
    assert out["ok"] is False
    assert "approval required but not yet granted" in out["reason"]
    intent = await env.get_intent(intent_id)
    assert intent["status"] == "REJECTED"
    assert env.broker.placed == []


async def test_execute_approved_intent_succeeds(env):
    user, broker_acc = await _seed_paper_trader(env)
    result = await env.evaluate(
        user.id,
        broker_acc.id,
        decision=ai.DECISION_NEEDS_APPROVAL,
        approval_required=True,
    )
    intent_id = result["intent_id"]
    await env.grant_approval(intent_id, user.id)
    out = await env.service.execute_intent(intent_id)
    assert out["ok"] is True and out["order_id"]
    intent = await env.get_intent(intent_id)
    assert intent["status"] == "EXECUTED"
    assert intent["approved_by"] == user.id
    assert len(env.broker.placed) == 1


async def test_execute_broker_failure_fails_closed(env):
    user, broker_acc = await _seed_paper_trader(env)
    result = await env.evaluate(user.id, broker_acc.id)
    intent_id = result["intent_id"]
    env.broker.fail_next = True
    out = await env.service.execute_intent(intent_id)
    assert out["ok"] is False
    assert "broker dispatch failed" in out["reason"]
    intent = await env.get_intent(intent_id)
    assert intent["status"] == "FAILED"
    assert await _order_status_by_key(env, intent_id) == "REJECTED"
    assert len(env.broker.placed) == 0
    # Replay is idempotent (nothing to double-dispatch).
    replay = await env.service.execute_intent(intent_id)
    assert replay["ok"] is True and replay["idempotent"] is True


async def test_execute_risk_gate_fails_closed(env):
    user, broker_acc = await _seed_paper_trader(env)
    result = await env.evaluate(user.id, broker_acc.id)
    intent_id = result["intent_id"]
    ai.set_engine_risk_getter(lambda: _FailingRisk())
    out = await env.service.execute_intent(intent_id)
    assert out["ok"] is False
    assert "risk gate" in out["reason"]
    intent = await env.get_intent(intent_id)
    assert intent["status"] == "REJECTED"
    assert env.broker.placed == []


# ── PAPER lifecycle: evaluate → execute → close → credit ────────────────

async def test_paper_full_lifecycle(env):
    user, broker_acc = await _seed_paper_trader(env)
    result = await env.evaluate(
        user.id,
        broker_acc.id,
        quantity=10,
        stop_loss_price=245.0,
        take_profit_price=260.0,
    )
    intent_id = result["intent_id"]
    intent = await env.get_intent(intent_id)
    assert intent["status"] == "CREATED"
    assert intent["requested_mode"] == "PAPER"
    assert intent["risk_status"] == "PASSED"
    assert intent["margin_required"] > 0
    assert env.broker.placed == []

    execute_out = await env.service.execute_intent(intent_id)
    assert execute_out["ok"] is True
    order_id = execute_out["order_id"]
    position_id = execute_out["position_id"]
    intent = await env.get_intent(intent_id)
    assert intent["status"] == "EXECUTED"
    assert intent["order_id"] == order_id
    assert intent["position_id"] == position_id

    async with env.factory() as db:
        pos = await db.get(PositionRecord, position_id)
        assert pos is not None
        assert pos.status == "OPEN"
        assert pos.mode == "PAPER"
        assert float(pos.entry_price) == FILL_PRICE
        assert float(pos.stop_loss_price) == 245.0
        assert float(pos.take_profit_price) == 260.0
        assert pos.protection_state == "PAPER"
        order = await db.get(OrderRecord, order_id)
        assert order is not None and order.status == "FILLED"
        assert order.mode == "PAPER"
        assert order.agent_intent_id == intent_id

    # PAPER entry never touches the paper balance; broker saw one entry.
    assert await env.balance(user.id) == 1_000_000.0
    assert len(env.broker.placed) == 1

    # Close at the fresh feed price (250.0) vs entry fill (251.5).
    close_out = await env.service.close_position(intent_id, position_id)
    assert close_out["ok"] is True
    assert close_out["realized_pnl"] == -15.0
    assert await env.balance(user.id) == 999_985.0

    intent = await env.get_intent(intent_id)
    assert intent["status"] == "CLOSED"
    assert "pnl=-15.00" in intent["execution_reason"]

    async with env.factory() as db:
        pos = await db.get(PositionRecord, position_id)
        assert pos.status == "CLOSED"
        assert pos.realized_pnl == -15.0
        assert pos.closed_at is not None


async def test_close_replay_never_double_credits(env):
    user, broker_acc = await _seed_paper_trader(env)
    intent_id = await _open_position(env, user.id, broker_acc.id)
    intent = await env.get_intent(intent_id)
    position_id = intent["position_id"]

    bal_before = await env.balance(user.id)
    first = await env.service.close_position(intent_id, position_id)
    assert first["ok"] is True
    bal_after_first = await env.balance(user.id)
    assert bal_after_first != bal_before  # realized P&L credited exactly once

    second = await env.service.close_position(intent_id, position_id)
    assert second["ok"] is True and second["idempotent"] is True
    assert await env.balance(user.id) == bal_after_first  # never double-credit


async def test_close_cross_tenant_owner_rejected(env):
    user_a, broker_a = await _seed_paper_trader(env)
    intent_a = await _open_position(env, user_a.id, broker_a.id)
    pos_a = (await env.get_intent(intent_a))["position_id"]

    user_b, broker_b = await _seed_paper_trader(env)
    intent_b = await _open_position(env, user_b.id, broker_b.id)

    out = await env.service.close_position(intent_b, pos_a)
    assert out["ok"] is False
    assert "does not belong" in out["reason"]
    async with env.factory() as db:
        pos = await db.get(PositionRecord, pos_a)
        assert pos.status == "OPEN"


async def test_close_missing_position_rejected(env):
    user, broker_acc = await _seed_paper_trader(env)
    intent_id = await _open_position(env, user.id, broker_acc.id)
    out = await env.service.close_position(
        intent_id, "00000000-0000-4000-8000-000000000000"
    )
    assert out["ok"] is False
    assert "position" in out["reason"]


# ── HTTP envelope: admin-gated REST surface ─────────────────────────────

def _intent_payload(
    user: UserRecord,
    broker_acc: BrokerAccountRecord,
    *,
    quantity: int = 10,
    decision: str = "TRADE",
    agent_task_id: str | None = None,
) -> dict:
    return {
        "agent_task_id": agent_task_id or f"task_{uuid.uuid4().hex[:12]}",
        "user_id": user.id,
        "broker_account_id": broker_acc.id,
        "strategy_id": None,
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": quantity,
        "order_type": "MARKET",
        "limit_price": None,
        "trigger_price": None,
        "stop_loss_price": 245.0,
        "take_profit_price": 260.0,
        "confidence": 0.8,
        "reason": "api e2e",
        "requested_mode": "PAPER",
        "decision": decision,
        "approval_required": False,
    }


async def _api_headers(env: IntentEnv) -> tuple[dict[str, str], UserRecord]:
    admin = await env.make_user(role="admin")
    return {"Authorization": f"Bearer {env.token_for(admin)}"}, admin


async def test_api_401_without_token(api_client):
    resp = await api_client.post("/api/agents/intents", json={})
    assert resp.status_code == 401


async def test_api_403_for_trader(api_client, env):
    trader = await env.make_user(role="trader")
    headers = {"Authorization": f"Bearer {env.token_for(trader)}"}
    resp = await api_client.post("/api/agents/intents", headers=headers, json={})
    assert resp.status_code == 403


async def test_api_gate_rejection_422(api_client, env):
    headers, _admin = await _api_headers(env)
    user = await env.make_user()
    broker_acc = await env.make_broker(user.id)
    resp = await api_client.post(
        "/api/agents/intents",
        headers=headers,
        json=_intent_payload(user, broker_acc, quantity=10**12),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "quantity_limit"


async def test_api_evaluate_execute_get_list_close_flow(api_client, env):
    headers, _admin = await _api_headers(env)
    user = await env.make_user()
    broker_acc = await env.make_broker(user.id)

    # Evaluate → CREATED intent row (admin may act on behalf of the owner).
    resp = await api_client.post(
        "/api/agents/intents",
        headers=headers,
        json=_intent_payload(user, broker_acc),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "CREATED" and body["intent_id"]
    intent_id = body["intent_id"]

    # Execute → single broker dispatch.
    resp = await api_client.post(
        f"/api/agents/intents/{intent_id}/execute", headers=headers
    )
    assert resp.status_code == 200
    outcome = resp.json()
    assert outcome["ok"] is True
    assert outcome["order_id"] and outcome["position_id"]
    assert len(env.broker.placed) == 1

    # Replay → idempotent 200, no double dispatch.
    replay = await api_client.post(
        f"/api/agents/intents/{intent_id}/execute", headers=headers
    )
    assert replay.status_code == 200
    assert replay.json()["ok"] is True and replay.json()["idempotent"] is True
    assert len(env.broker.placed) == 1

    # GET single intent.
    resp = await api_client.get(
        f"/api/agents/intents/{intent_id}", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "EXECUTED"

    # LIST (admin query).
    resp = await api_client.get("/api/agents/intents", headers=headers)
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert intent_id in ids

    # Close canonical position (PAPER ⇒ engine-simulated exit + P&L credit).
    resp = await api_client.post(
        f"/api/agents/intents/{intent_id}/close",
        headers=headers,
        json={"position_id": outcome["position_id"]},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    intent_after = (
        await api_client.get(f"/api/agents/intents/{intent_id}", headers=headers)
    ).json()
    assert intent_after["status"] == "CLOSED"


async def test_api_get_unknown_intent_404(api_client, env):
    headers, _admin = await _api_headers(env)
    resp = await api_client.get(
        "/api/agents/intents/int_not_found", headers=headers
    )
    assert resp.status_code == 404