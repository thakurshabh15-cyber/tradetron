"""Broker postback handler for order updates from Zerodha, Upstox, Angel One, Binance."""

from __future__ import annotations

from app.webhooks.queue.redis_streams import QueuedWebhook
from app.brokers.postback import reconcile_broker_postback
from app.engine.order_reconciliation import normalize_broker_status
from app.db.session import get_db
from app.core.logging import get_logger
from app.core.monitoring import monitoring_sentinel

logger = get_logger("webhook.handlers.broker")


async def handle_broker_postback(webhook: QueuedWebhook) -> None:
    """Process broker order postback (Zerodha, Upstox, Angel One, Binance).

    V3 hardening: the mutation is delegated to the shared reconciler
    (``app.brokers.postback``) which binds the event to the order's own CONNECTED,
    tenant-owned broker account.  Signature verification is performed at the
    ingress layer; this handler never trusts an unsigned/spoofed event.

    Status normalization (P1): a queued event first reduces the raw broker
    status to the canonical order vocabulary (``normalize_broker_status``).
    COMPLETE / COMPLETED -> FILLED, so a normalized fill flows into the SAME
    atomic fill-claim path as any other FILLED event.  Rejection/cancellation
    reduces to REJECTED / CANCELLED, open states reduce to OPEN.  An
    unrecognized status is fail-safe: it is NOT normalized to a fill, NOT
    guessed as a terminal state, and the event is dropped (XACKed) WITHOUT any
    order/trade/position mutation - so the reconciler's duplicate-guard and
    atomic CAS semantics stay authoritative for every legitimate event.
    """
    envelope = webhook.envelope
    payload = envelope.payload

    broker_order_id = payload.get("broker_order_id") or payload.get("order_id")
    raw_status = payload.get("status", "")
    symbol = payload.get("symbol") or payload.get("tradingsymbol", "")
    filled_qty = int(payload.get("filled_quantity", 0) or 0)
    avg_price = float(payload.get("average_price", 0.0) or 0.0)
    broker_account_id = payload.get("broker_account_id")

    if not broker_order_id:
        raise ValueError("Missing broker_order_id in payload")

    # Normalize the raw broker status to the canonical order vocabulary BEFORE
    # reconciliation.  Unknown -> None (fail-safe): we never fabricate a fill
    # and never guess a terminal state; we simply skip without mutation.
    status = normalize_broker_status(raw_status)
    if status is None:
        logger.warning(
            "Broker postback for order=%s dropped: unrecognized status %r",
            broker_order_id, str(raw_status),
        )
        return

    # Get DB session
    async for db in get_db():
        outcome = await reconcile_broker_postback(
            db,
            broker_order_id=broker_order_id,
            broker_account_id=broker_account_id,
            status=status,
            symbol=symbol,
            filled_quantity=filled_qty,
            average_price=avg_price,
        )
        if outcome.get("reason"):
            logger.warning(
                "Broker postback not reconciled for order=%s: %s",
                broker_order_id, outcome["reason"],
            )
        else:
            logger.info(
                "Processed broker postback: order=%s status=%s symbol=%s",
                broker_order_id, status, symbol,
            )



# Register handler
from app.webhooks.workers.pool import worker_pool, WorkerConfig

worker_pool.register_pool(WorkerConfig(
    pool_name="broker_critical",
    queue_names=["webhooks:broker:critical"],
    concurrency=10,  # High concurrency for critical path
    handler=handle_broker_postback,
))