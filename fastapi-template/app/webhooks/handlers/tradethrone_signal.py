"""TradeThrone signal webhook handler — governed, intent-only.

External signals arrive via the webhook ingress router and are handled here at
the durable-acceptance boundary.  This handler NEVER calls a broker adapter,
an ``OrderManager`` or a ``place_*`` helper directly: the signal is normalized,
its owner is resolved server-side, and it is enqueued as a durable
``trading_agent`` / ``execute_trade`` agent task whose trigger key is derived
from the envelope (stable across re-deliveries).  A later scheduler pass
(``AgentIntentTriggerBridge.handle_scheduler_event``) turns the task into a
durable ``TradingIntentRecord`` through ``AgentTradingService`` — the only
component that may reach the broker.

Fail closed: unsupported execution modes / entry order types, unresolved
owners and invalid payloads raise :class:`TriggerIntentError` so the worker
nacks / requeues the signal.  The worker never acknowledges a signal that was
not durably accepted.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.engine.agent_intent_triggers import (
    TriggerIntentError,
    agent_intent_trigger_bridge,
)
from app.webhooks.queue.redis_streams import QueuedWebhook

logger = get_logger("webhook.handlers.tradethrone")


async def handle_tradethrone_signal(webhook: QueuedWebhook) -> None:
    """Process a TradeThrone signal webhook (durable acceptance only).

    Never dispatches to a broker: the signal becomes a durable agent task here;
    the scheduler's governed ``execute_trade`` handler is the only path that
    can create/execute an intent.
    """
    envelope = webhook.envelope
    try:
        result = await agent_intent_trigger_bridge.submit_webhook_signal(webhook)
    except TriggerIntentError as exc:
        logger.error(
            "TradeThrone signal FAILED CLOSED: provider=%s code=%s message=%s",
            envelope.provider,
            exc.code,
            exc.message,
        )
        # Fail closed (P0-2): never XACK-as-success a signal that was not
        # durably accepted.  Re-raise so the worker nacks / requeues; the
        # durable trigger key dedupes a retry without double-dispatch.
        raise

    logger.info(
        "TradeThrone signal accepted (no dispatch): provider=%s key=%s task=%s",
        envelope.provider,
        result.get("trigger_key"),
        result.get("agent_task_id"),
    )


# Register handler for tradethrone pools
from app.webhooks.workers.pool import worker_pool, WorkerConfig  # noqa: E402

# Critical pool for risk alerts
worker_pool.register_pool(WorkerConfig(
    pool_name="tradethrone_critical",
    queue_names=["webhooks:tradethrone:critical"],
    concurrency=10,
    handler=handle_tradethrone_signal,
))

# High priority pool for signals
worker_pool.register_pool(WorkerConfig(
    pool_name="tradethrone_high",
    queue_names=["webhooks:tradethrone:high"],
    concurrency=10,
    handler=handle_tradethrone_signal,
))

# Normal pool for position updates, strategy status
worker_pool.register_pool(WorkerConfig(
    pool_name="tradethrone_normal",
    queue_names=["webhooks:tradethrone:normal"],
    concurrency=5,
    handler=handle_tradethrone_signal,
))


# Register handler for custom_normal pool
worker_pool.register_pool(WorkerConfig(
    pool_name="custom_normal",
    queue_names=["webhooks:custom:normal"],
    concurrency=5,
    handler=handle_tradethrone_signal,
))