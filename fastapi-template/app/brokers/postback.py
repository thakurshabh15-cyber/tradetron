"""Broker postback reconciliation - signature-verified, broker-account-bound mutation.

Security contract (V3 hardening):

  1. A broker postback that can mutate financial order/trade state MUST be
     cryptographically verified before any state change.  The direct broker
     postback endpoints (``/api/brokers/webhooks|postback/{broker_name}``) and the
     queued webhook worker (``app/webhooks/handlers/broker_postback.py``) both go
     through this module.

  2. Every reconciled event is BOUND to a specific broker account: the matching
     ``OrderRecord`` must be linked to a CONNECTED, active broker account owned by
     the order's user, and the event's ``broker_account_id`` (when the payload
     names one) must equal the order's account.  Cross-account / cross-tenant
     events are ignored WITHOUT mutation, so a valid signature for provider X can
     never be replayed against another tenant's order.

  3. Signature verification reuses the existing provider verifiers
     (``app.webhooks.validation.signatures``) - no new cryptographic protocol is
     introduced.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.market_data.manager import ws_manager
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord

logger = get_logger("brokers.postback")

# Local order statuses that mean the fill is ALREADY finalized (booked on the
# trade/position ledgers) or terminally did NOT fill.  A confirmed FILLED event
# must never book a position + trade on top of these.  Both fill-booking
# finalizers (this reconciler and ``app/engine/order_reconciliation.py``) use
# the EXACT same predicate so the order row is the single, atomic
# mutual-exclusion point between them: in a concurrent postback + stale
# reconciliation race, only ONE transaction can claim the row (rowcount == 1)
# and book the fill; the loser matches 0 rows and skips.
FINALIZED_ORDER_STATUSES: tuple[str, ...] = tuple(
    s.upper()
    for s in (
        "FILLED",
        "REJECTED",
        "CANCELLED",
        "CANCELED",
        "EXPIRED",
        "CANCELLED/REJECTED",
    )
)


def verify_broker_postback_signature(
    body: bytes,
    headers: dict[str, str],
    provider: str,
) -> None:
    """Fail-closed signature verification for broker postbacks (V3).

    When the deployment requires verification (``webhook_local_mode`` is False) an
    unsigned, invalidly-signed, or unverifiable postback raises HTTP 401 BEFORE any
    financial state can be touched.  Local (dev/test) mode keeps its documented
    bypass.
    """
    from app.config import settings

    if settings.webhook_local_mode:
        return

    from app.webhooks.validation.signatures import (
        VerificationResult,
        get_verifier,
        init_verifiers,
    )

    # (Re)register verifiers from the CURRENT settings so verification always runs
    # against the configured provider secret (deterministic across tests).
    init_verifiers(settings)
    verifier = get_verifier(provider.lower())
    if verifier is None:
        logger.warning(
            "Broker postback for provider %s rejected: no signature verifier configured.",
            provider,
        )
        raise HTTPException(
            status_code=401,
            detail=(
                f"No signature verifier configured for provider {provider!r}; "
                "unsigned broker postbacks are rejected."
            ),
        )
    result: VerificationResult = verifier.verify(body, headers)
    if not result.valid:
        logger.warning(
            "Broker postback signature verification failed for provider %s: %s",
            provider, result.error,
        )
        raise HTTPException(
            status_code=401,
            detail=result.error or "Invalid signature",
        )


async def reconcile_broker_postback(
    db: AsyncSession,
    *,
    broker_order_id: str,
    broker_account_id: str | None,
    status: str,
    symbol: str = "",
    filled_quantity: int = 0,
    average_price: float = 0.0,
) -> dict[str, Any]:
    """Reconcile a signature-verified broker postback against an order.

    Broker-account binding (V3):
      - the order must be linked to a broker account;
      - that account must be CONNECTED, active, and owned by the order's user;
      - when the payload names a ``broker_account_id`` it must equal the order's
        account id (cross-account events are ignored without mutation).

    Trade-ledger idempotency (V4): a FILLED event books exactly ONE TradeRecord
    per order fill.  Broker retries / redelivery (HTTP retry on the direct
    endpoint, queued worker + direct path sharing this reconciler, or a fill
    webhook arriving AFTER the order was already finalized FILLED locally by the
    REST / DMA / copy-trading entry path) must never double-book the same fill.

    Returns an outcome dict with ``event_processed`` and an optional ``reason``.
    """
    norm_status = str(status or "").upper()

    stmt = select(OrderRecord).where(OrderRecord.broker_order_id == broker_order_id)
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()

    if order is None:
        logger.info(
            "Broker postback ignored: no order with broker_order_id=%s",
            broker_order_id,
        )
        return {"reconciled": False, "event_processed": False, "reason": "order_not_found"}

    # Cross-account guard: the postback must be bound to the order's OWN account.
    if broker_account_id and order.broker_account_id != broker_account_id:
        logger.warning(
            "Broker postback ignored (account mismatch): order %s belongs to account %s, "
            "event named %s",
            order.id, order.broker_account_id, broker_account_id,
        )
        return {"reconciled": False, "event_processed": False, "reason": "account_mismatch"}

    # The order MUST be bound to a CONNECTED, active broker account owned by the
    # order's user.  Without that binding there is no cryptographically
    # associable account for this financial event.
    account_stmt = select(BrokerAccountRecord).where(
        BrokerAccountRecord.id == order.broker_account_id,
        BrokerAccountRecord.status == "CONNECTED",
        BrokerAccountRecord.is_active.is_(True),
        BrokerAccountRecord.user_id == order.user_id,
    )
    account = (await db.execute(account_stmt)).scalars().first()
    if account is None:
        logger.warning(
            "Broker postback ignored for order %s: cannot bind to a CONNECTED broker "
            "account owned by the order's user.",
            order.id,
        )
        return {
            "reconciled": False,
            "event_processed": False,
            "reason": "order_not_bound_to_connected_account",
        }

    # Apply the state transition (the event has passed provider verification and
    # broker-account binding).
    if norm_status == "FILLED":
        fill_price = average_price or order.price or 0.0
        fill_qty = filled_quantity or order.quantity

        # V4.1 atomic fill claim: exactly ONE finalizer books the fill.  The
        # order row itself is the single serialization point shared with the
        # order-reconciliation engine (app/engine/order_reconciliation.py).
        # Both finalizers attempt the SAME conditional UPDATE: the row may be
        # claimed only while it is NOT already finalized, has NO linked
        # position, and carries NO fill trade bound to the order UUID.  When
        # the postback and a stale reconciliation pass race on the same PENDING
        # snapshot, the database re-evaluates this predicate at write time, so
        # exactly ONE transaction matches (rowcount == 1); the loser matches 0
        # rows and skips - a duplicate PositionRecord / TradeRecord cannot be
        # committed by either finalizer.
        not_already_booked = ~exists(
            select(TradeRecord.id).where(TradeRecord.order_id == OrderRecord.id)
        )
        claim = await db.execute(
            update(OrderRecord)
            .where(
                OrderRecord.id == order.id,
                OrderRecord.status.notin_(FINALIZED_ORDER_STATUSES),
                OrderRecord.position_id.is_(None),
                not_already_booked,
            )
            .values(
                status="FILLED",
                filled_price=fill_price,
                filled_quantity=fill_qty,
                error_message=None,
            )
            .execution_options(synchronize_session=False)
        )

        if claim.rowcount != 1:
            # Another finalizer already booked this fill: duplicate broker
            # delivery, the reconciliation engine won the race, or a local
            # REST/DMA/copy-trading entry finalized first.  Never re-book the
            # position/trade ledger.
            await db.refresh(order)
            logger.info(
                "Broker postback FILLED for order %s already booked by another "
                "finalizer; skipping (status=%s).",
                order.id, order.status,
            )
        else:
            await db.refresh(order)
            position = PositionRecord(
                user_id=order.user_id,
                broker_account_id=order.broker_account_id,
                symbol=order.symbol,
                side="LONG" if order.side.upper() == "BUY" else "SHORT",
                quantity=fill_qty,
                entry_price=fill_price,
                current_price=fill_price,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                mode="LIVE",
                status="OPEN",
                opened_at=datetime.now(timezone.utc),
            )
            db.add(position)
            await db.flush()
            order.position_id = position.id

            trade = TradeRecord(
                order_id=order.id,
                strategy_id=order.strategy_id,
                user_id=order.user_id,
                symbol=order.symbol,
                side=order.side,
                quantity=fill_qty,
                price=fill_price,
                entry_price=fill_price,
                exit_price=fill_price,
                mode="LIVE",
                exit_reason="BROKER_POSTBACK_FILL",
            )
            db.add(trade)
    else:
        # Non-fill transitions (OPEN / PARTIALLY_FILLED / REJECTED / CANCELLED
        # / ...) never book; state the local row directly.
        order.status = norm_status
        if filled_quantity:
            order.filled_quantity = filled_quantity
        if average_price:
            order.filled_price = average_price


    await db.commit()

    try:
        await ws_manager.broadcast(
            f"order_update:{order.strategy_id}",
            {
                "event": "ORDER_STATUS_CHANGED",
                "order_id": order.id,
                "broker_order_id": broker_order_id,
                "status": norm_status,
                "symbol": order.symbol,
                "filled_quantity": filled_quantity,
                "average_price": average_price,
            },
        )
    except Exception as exc:
        logger.warning("WebSocket broadcast failed for order %s: %s", order.id, exc)

    logger.info(
        "Reconciled broker postback: order=%s status=%s symbol=%s",
        broker_order_id, norm_status, symbol or order.symbol,
    )
    return {"reconciled": True, "event_processed": True, "reason": None}