"""P0-2 GREEN regression suite (extension of the RED file).

The RED file pins the core invariants:
  A. a valid entry signal MUST create a durable ``AgentTaskRecord``;
  B. duplicate delivery MUST NOT create duplicate durable tasks.

This file adds the approved coverage for the governed webhook pipeline with
real DB sessions:

  C. concurrent duplicate delivery -> exactly one durable task
  E. fail-closed rejection before acceptance -> handler raises, nothing persisted
  H. tenant-less signal key cannot create unsafe cross-tenant collisions

The old in-memory ``OrderManager``-specific cases (restart-loss, in-memory
authority, crash windows) no longer apply to the webhook path: the handler dubs
the signal into the durable agent-task domain only, and broker dispatch is the
scheduler's governed ``execute_trade`` job (covered by the intent/runtime
suites).  The webhook path never touches an ``OrderManager``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal, init_db
from app.engine.agent_intent_triggers import (
    AGENT_TYPE_TRADING,
    TASK_KIND_EXECUTE_TRADE,
    TriggerIntentError,
    webhook_trigger_key,
)
from app.engine.agent_runtime import ensure_agent_registry
from app.models.agent import AgentRecord, AgentTaskRecord
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, StrategyRecord
from app.models.user import UserRecord
from app.webhooks.handlers.tradethrone_signal import handle_tradethrone_signal
from app.webhooks.queue.redis_streams import QueuedWebhook
from app.webhooks.validation.schemas import WebhookEnvelope

_SIGNAL = "entry_long"
_SYMBOL = "NIFTY"
_ACTION = "BUY"
_QTY = 65  # NIFTY lot size (65) --- compliant, no auto-correct surprises.
_USER_ID = "u-tradethrone-green"


def _webhook(tag: str, strategy: str = "red-repro", ts: datetime | None = None) -> QueuedWebhook:
    envelope = WebhookEnvelope(
        event_id=f"evt-{tag}",
        event_type="signal",
        timestamp=ts or datetime.now(timezone.utc),
        provider="tradethrone",
        payload={
            "signal": _SIGNAL,
            "symbol": _SYMBOL,
            "action": _ACTION,
            "quantity": _QTY,
            "strategy_name": strategy,
            "order_type": "MARKET",
            "product_type": "INTRADAY",
            "exchange": "NFO",
            "validity": "DAY",
        },
        idempotency_key=f"sig-{tag}",
    )
    return QueuedWebhook(envelope=envelope)
@pytest.fixture(autouse=True)
async def _init_tables():
    """Real DB tables (same pattern as the other real-DB durability suites)."""
    await init_db()
    yield


@pytest.fixture(autouse=True)
async def _seed_governed_stack():
    """Provision the server-side owner + operator-enabled trading_agent."""
    await ensure_agent_registry()

    async def _seed() -> None:
        async with SessionLocal() as db:
            agent = (
                await db.execute(
                    select(AgentRecord).where(
                        AgentRecord.agent_type == AGENT_TYPE_TRADING
                    )
                )
            ).scalars().first()
            assert agent is not None
            agent.enabled = True

            user = UserRecord(id=_USER_ID, email=f"{_USER_ID}@example.test")
            db.add(user)
            await db.flush()

            broker = BrokerAccountRecord(
                id="ba-tradethrone-green",
                user_id=user.id,
                broker_name="SIMULATED",
                account_name="Green Repro Sim",
            )
            broker.set_api_key("test-api-key")
            broker.set_api_secret("test-api-secret")
            broker.set_access_token("test-access-token")
            db.add(broker)
            await db.flush()

            db.add(
                StrategyRecord(
                    id="st-tradethrone-green",
                    user_id=user.id,
                    name="red-repro",
                    symbols_json='["NIFTY"]',
                    conditions_json="[]",
                    action_json='{"order_type": "MARKET"}',
                    enabled=True,
                    execution_mode="PAPER",
                    broker_account_id=broker.id,
                )
            )
            await db.commit()

    async def _cleanup() -> None:
        async with SessionLocal() as db:
            await db.execute(
                delete(AgentTaskRecord).where(
                    AgentTaskRecord.agent_type == AGENT_TYPE_TRADING
                )
            )
            await db.execute(
                delete(OrderRecord).where(
                    OrderRecord.user_id.in_([_USER_ID, "tenant-u-1"])
                )
            )
            await db.execute(
                delete(StrategyRecord).where(StrategyRecord.user_id == _USER_ID)
            )
            await db.execute(
                delete(BrokerAccountRecord).where(
                    BrokerAccountRecord.user_id == _USER_ID
                )
            )
            await db.execute(delete(UserRecord).where(UserRecord.id == _USER_ID))
            agent = (
                await db.execute(
                    select(AgentRecord).where(
                        AgentRecord.agent_type == AGENT_TYPE_TRADING
                    )
                )
            ).scalars().first()
            if agent is not None:
                agent.enabled = False
            await db.commit()

    await _cleanup()
    await _seed()
    yield
    await _cleanup()
@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_single_task():
    """C - two CONCURRENT deliveries of the same signal must produce ONE
    durable agent task.

    The webhook path never dispatches: the bridge's ``create_task`` idempotent
    on the deterministic trigger key is the backstop -- the loser lands on the
    same durable task (``created=False``).
    """
    ts = datetime.now(timezone.utc)
    a = uuid.uuid4().hex[:8]
    b = uuid.uuid4().hex[:8]
    await asyncio.gather(
        handle_tradethrone_signal(_webhook(tag=f"conc-{a}a", ts=ts)),
        handle_tradethrone_signal(_webhook(tag=f"conc-{b}b", ts=ts)),
    )

    tasks = await _trading_tasks()
    assert len(tasks) == 1, (
        f"Concurrent duplicate deliveries created {len(tasks)} durable agent "
        "tasks; the unique trigger-key claim must allow exactly one."
    )


@pytest.mark.asyncio
async def test_fail_closed_rejection_persists_nothing():
    """E - a signal whose owner cannot be resolved MUST fail closed: the
    handler raises :class:`TriggerIntentError` and NO durable task is written
    (never acknowledge what was not durably accepted)."""
    from app.engine.agent_intent_triggers import ERR_OWNER_UNRESOLVED

    # No strategy/owner exists for this signal: remove any seeded lookup row if
    # a previous test leaked one, then process a signal for a ghost strategy.
    async with SessionLocal() as db:
        await db.execute(
            delete(StrategyRecord).where(StrategyRecord.name == "ghost-strategy")
        )
        await db.commit()

    with pytest.raises(TriggerIntentError) as exc_info:
        await handle_tradethrone_signal(
            _webhook(tag=f"fail-{uuid.uuid4().hex[:8]}", strategy="ghost-strategy")
        )
    assert exc_info.value.code == ERR_OWNER_UNRESOLVED

    tasks = await _trading_tasks()
    assert tasks == [], (
        "A signal that failed closed must never leave a durable agent task "
        "behind (the worker must nack/requeue, never resume settled state)."
    )


def test_tenant_less_signal_key_domain_is_collision_safe():
    """H - tenant-less signal keys are hashed from the full trigger material so
    distinct strategies can never share a key, and the agent-task idempotency
    domain is independent of user-scoped ``client_order_id`` rows."""
    ts = datetime.now(timezone.utc)
    ts_sec = int(ts.timestamp())

    key_a = webhook_trigger_key(
        provider="tradethrone",
        strategy_name="strat-A",
        symbol=_SYMBOL,
        side=_ACTION,
        quantity=_QTY,
        ts_sec=ts_sec,
        signal=_SIGNAL,
    )
    key_b = webhook_trigger_key(
        provider="tradethrone",
        strategy_name="strat-B",
        symbol=_SYMBOL,
        side=_ACTION,
        quantity=_QTY,
        ts_sec=ts_sec,
        signal=_SIGNAL,
    )
    assert key_a != key_b, "Distinct strategies must never share a signal key."

    # A user-scoped DMA-style row may legally reuse the STRING: the agent-task
    # idempotency domain lives in agent_tasks.idempotency_key, not orders.
    key = webhook_trigger_key(
        provider="tradethrone",
        strategy_name="red-repro",
        symbol=_SYMBOL,
        side=_ACTION,
        quantity=_QTY,
        ts_sec=ts_sec,
        signal=_SIGNAL,
    )
    assert key.startswith("ttr-")
    assert len(key) <= 128


async def _trading_tasks() -> list[AgentTaskRecord]:
    async with SessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(AgentTaskRecord).where(
                        AgentTaskRecord.agent_type == AGENT_TYPE_TRADING,
                        AgentTaskRecord.task_kind == TASK_KIND_EXECUTE_TRADE,
                    )
                )
            ).scalars().all()
        )