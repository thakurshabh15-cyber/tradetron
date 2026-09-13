"""P0-2 RED regression: the TradeThrone signal webhook must persist a durable
agent task in the database (source of truth) instead of trusting process-local
/ in-memory state.

Defect (P0-2): the original handler reached the engine's in-memory
``OrderManager`` and had no durable DB claim before broker dispatch; a crash /
restart / scale-out lost accepted signal state and a duplicate delivery could
double-dispatch.

The governed contract (current implementation): the webhook handler NEVER
dispatches to a broker.  It accepts the signal through
``AgentIntentTriggerBridge.submit_webhook_signal`` which:

  1. validates the TradeThrone payload (fail closed),
  2. canonicalizes symbol / quantity (lot-size compliance),
  3. resolves the owner server-side (never trusted from the payload),
  4. enqueues a durable ``trading_agent`` / ``execute_trade`` ``AgentTaskRecord``
     whose ``idempotency_key`` is the deterministic envelope-derived trigger key.

Broker dispatch happens exclusively on a later scheduler pass against the
durable intent (covered by the scheduler / intent suites).

Invariant being proven:
  1. Processing a *valid* entry signal through ``handle_tradethrone_signal``
     MUST result in a durable ``AgentTaskRecord`` (DB is the source of truth).
  2. Two deliveries of the same signal MUST NOT create duplicate durable tasks.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal, init_db
from app.engine.agent_intent_triggers import (
    AGENT_TYPE_TRADING,
    TASK_KIND_EXECUTE_TRADE,
    webhook_trigger_key,
)
from app.engine.agent_runtime import ensure_agent_registry
from app.models.agent import AgentRecord, AgentTaskRecord
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import StrategyRecord
from app.models.user import UserRecord
from app.webhooks.handlers.tradethrone_signal import handle_tradethrone_signal
from app.webhooks.queue.redis_streams import QueuedWebhook
from app.webhooks.validation.schemas import WebhookEnvelope

_SIGNAL = "entry_long"
_SYMBOL = "NIFTY"
_ACTION = "BUY"
_QTY = 65  # NIFTY lot size (65) --- compliant, no auto-correct surprises.
_STRATEGY = "red-repro"
_USER_ID = "u-tradethrone-red"


def _webhook(tag: str, ts: datetime | None = None) -> QueuedWebhook:
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
            "strategy_name": _STRATEGY,
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
    """Provision the server-side owner + operator-enabled trading_agent.

    The governed webhook path resolves ownership server-side (never from the
    payload): strategy ``red-repro`` -> user -> active broker account.  The
    ``trading_agent`` registry row is provisioned by ``ensure_agent_registry``
    and then operator-enabled (matches the migration 0010 ``enabled=False``
    opt-in contract).
    """
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
            assert agent is not None, (
                "trading_agent registry row missing after ensure_agent_registry()"
            )
            agent.enabled = True

            user = UserRecord(id=_USER_ID, email=f"{_USER_ID}@example.test")
            db.add(user)
            await db.flush()

            broker = BrokerAccountRecord(
                id="ba-tradethrone-red",
                user_id=user.id,
                broker_name="SIMULATED",
                account_name="Red Repro Sim",
            )
            broker.set_api_key("test-api-key")
            broker.set_api_secret("test-api-secret")
            broker.set_access_token("test-access-token")
            db.add(broker)
            await db.flush()

            db.add(
                StrategyRecord(
                    id="st-tradethrone-red",
                    user_id=user.id,
                    name=_STRATEGY,
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
async def test_valid_entry_signal_creates_durable_agent_task():
    """P0-2 invariant 1: a valid entry signal MUST be durably committed as an
    AgentTaskRecord (DB is the source of truth), never held only in process
    memory and never dispatched to a broker from the webhook path."""
    await handle_tradethrone_signal(_webhook(tag=uuid.uuid4().hex[:12]))

    async with SessionLocal() as db:
        tasks = (
            await db.execute(
                select(AgentTaskRecord).where(
                    AgentTaskRecord.agent_type == AGENT_TYPE_TRADING,
                    AgentTaskRecord.task_kind == TASK_KIND_EXECUTE_TRADE,
                )
            )
        ).scalars().all()

    assert len(tasks) == 1, (
        "handle_tradethrone_signal completed a VALID entry signal without "
        "creating a durable AgentTaskRecord. Accepted signal state exists only "
        "in memory -- P0-2 invariant violated."
    )

    task = tasks[0]
    assert task.status == "PENDING"
    assert task.idempotency_key and task.idempotency_key.startswith("ttr-")
    payload = json.loads(task.input_json)
    assert payload["symbol"] == "NIFTY"
    assert payload["side"] == "BUY"
    assert payload["quantity"] == _QTY
    assert payload["strategy_name"] == "red-repro"
    assert payload["trigger_source"] == "webhook"


@pytest.mark.asyncio
async def test_duplicate_signal_delivery_does_not_duplicate_task():
    """P0-2 invariant 2: two deliveries of the same signal (duplicate webhook /
    PEL recovery re-delivery) MUST NOT produce duplicate durable tasks."""
    key = uuid.uuid4().hex[:12]
    # SAME envelope timestamp == same coarse-second identity for the
    # deterministic trigger key -- exactly what a PEL / XAUTOCLAIM re-delivery
    # of the same signal event would carry.
    ts = datetime.now(timezone.utc)
    await handle_tradethrone_signal(_webhook(tag=f"{key}a", ts=ts))
    first = await _trading_tasks()
    assert len(first) == 1

    await handle_tradethrone_signal(_webhook(tag=f"{key}b", ts=ts))
    tasks = await _trading_tasks()
    assert len(tasks) == 1, (
        f"Expected exactly 1 durable AgentTaskRecord after two deliveries of "
        f"the same signal; got {len(tasks)}. The trigger path has no DB "
        f"idempotency claim, so duplicate delivery can duplicate state -- "
        f"P0-2 invariant violated."
    )
    assert tasks[0].id == first[0].id
    expect_key = webhook_trigger_key(
        provider="tradethrone",
        strategy_name=_STRATEGY,
        symbol=_SYMBOL,
        side=_ACTION,
        quantity=_QTY,
        ts_sec=int(ts.timestamp()),
        signal=_SIGNAL,
    )
    assert tasks[0].idempotency_key == expect_key


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