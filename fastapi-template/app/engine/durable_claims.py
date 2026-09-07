"""Shared durable order-claim kernel (P0-2 webhook durability).

Single implementation of the "claim a PENDING ``OrderRecord`` BEFORE broker
dispatch, CAS-finalize it AFTERWARDS" state machine so the LIVE strategy path
(per-user ``client_order_id``) and the TradeThrone webhook signal path
(tenant-less ``signal_key``) can never diverge in durability semantics.

Claim rules (the proven manual/DMA + strategy-entry semantics):
  * key already FILLED or PENDING  -> return ``None`` (caller must NOT
    dispatch; duplicate delivery or in-flight concurrent claim).
  * key already REJECTED/CANCELLED -> CAS-reclaim the row to PENDING and return
    its id (caller may dispatch -- retry of a failed signal).
  * no row                        -> INSERT a PENDING claim; if a concurrent
    identical claim won the INSERT race (unique index), rollback and return
    ``None`` (never double-dispatch).

Finalize (two-phase, mirrors the manual/DMA postback pattern):
  1. persist ``broker_order_id`` in its own commit (crash-window hardening).
  2. CAS-update PENDING -> FILLED and create Trade (+ optionally Position,
     linked back via ``claim.position_id``).

Reject:
  * CAS-update PENDING -> REJECTED (never from a terminal state).
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.models.trading import OrderRecord, PositionRecord, TradeRecord

# Non-retryable claim statuses for a given key (map to "must NOT dispatch").
_TERMINAL_LIVE = ("FILLED", "PENDING")


async def claim_order_record(
    *,
    key_predicate: Any,
    claim_values: dict[str, Any],
) -> Optional[str]:
    """Durably claim a PENDING ``OrderRecord`` matched by ``key_predicate``.

    ``key_predicate`` is a SQLAlchemy boolean expression selecting the row for
    the idempotency key (e.g. ``(user_id == u) & (client_order_id == k)`` or
    ``signal_key == k``).  ``claim_values`` carries every non-status column for
    a fresh claim AND for a CAS-reclaim of a REJECTED/CANCELLED row.

    Returns the claim row id when the caller may dispatch; returns ``None``
    when the key is already FILLED/in-flight or a concurrent INSERT won the
    race.  Never raises on duplicates -- the partial unique index + CAS are the
    concurrency backstops.
    """
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(OrderRecord).where(key_predicate)
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.status in _TERMINAL_LIVE:
                # Completed entry already booked, or same key is in-flight
                # (a concurrent dispatch).  Never double-dispatch.
                return None
            # REJECTED / CANCELLED -- retryable state: re-claim via CAS.
            retry = await session.execute(
                update(OrderRecord)
                .where(
                    key_predicate,
                    OrderRecord.status.not_in(_TERMINAL_LIVE),
                )
                .values(
                    **claim_values,
                    status="PENDING",
                    error_message=None,
                    broker_order_id=None,
                    filled_price=None,
                    filled_quantity=0,
                    position_id=None,
                )
            )
            if retry.rowcount == 1:
                await session.commit()
                return existing.id
            # Lost the CAS race: follow whatever the winner did.
            await session.rollback()
            return None

        claim = OrderRecord(**claim_values, status="PENDING")
        session.add(claim)
        try:
            await session.commit()
        except IntegrityError:
            # Concurrent identical claim committed first -- never double-dispatch.
            await session.rollback()
            return None
        return claim.id


async def fetch_claim_by_key(key_predicate: Any) -> Optional[OrderRecord]:
    """Return the current claim row for ``key_predicate`` (or None).

    Pure read used to build idempotent-replay payloads after a
    ``claim_order_record`` returned ``None``.
    """
    async with SessionLocal() as session:
        return (
            await session.execute(select(OrderRecord).where(key_predicate))
        ).scalar_one_or_none()


async def reject_order_claim(claim_id: str, reason: str) -> None:
    """CAS-reject a PENDING claim after broker dispatch failure.

    No-op (with rollback) if a concurrent finalizer already moved the claim out
    of PENDING -- a reject must never overwrite a FILLED row.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            update(OrderRecord)
            .where(
                OrderRecord.id == claim_id,
                OrderRecord.status == "PENDING",
            )
            .values(
                status="REJECTED",
                error_message=reason,
            )
        )
        if result.rowcount == 1:
            await session.commit()
        else:
            await session.rollback()
async def finalize_order_claim(
    *,
    claim_id: str,
    strategy_id: Optional[str],
    strategy_name: str,
    broker_order_id: str,
    symbol: str,
    side: str,
    quantity: int,
    filled_price: float,
    user_id: Optional[str],
    broker_account_id: Optional[str],
    mode: str,
    create_position: bool,
) -> dict[str, Any]:
    """Finalize a durable claim after broker acceptance (two-phase commit).

    Mirrors the manual/DMA postback pattern:
      1. Persist broker_order_id in its own commit (crash-window hardening).
      2. CAS-update claim to FILLED and create Trade (+ Position when
         ``create_position``), linking position_id onto the order row.

    If a concurrent worker/postback already finalized, the CAS rowcount check
    detects it and returns the existing trade payload.  A persistence failure
    here propagates to the caller (fail closed -- never acknowledge success).
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    async with SessionLocal() as session:
        # Phase 1: persist broker reference (its own commit).
        claim = await session.get(OrderRecord, claim_id)
        if claim is None:
            raise RuntimeError(f"order claim {claim_id} not found")
        if not claim.broker_order_id:
            claim.broker_order_id = broker_order_id
            await session.commit()

        # Phase 2: CAS-finalize FILLED + create Trade + (optionally) Position.
        result = await session.execute(
            update(OrderRecord)
            .where(
                OrderRecord.id == claim_id,
                OrderRecord.status == "PENDING",
                OrderRecord.position_id.is_(None),
            )
            .values(
                status="FILLED",
                filled_price=filled_price,
                filled_quantity=quantity,
                broker_order_id=broker_order_id,
                error_message=None,
            )
        )
        if result.rowcount != 1:
            # Already finalized by a concurrent worker or broker postback.
            await session.rollback()
            finalized = await session.get(OrderRecord, claim_id)
            exec_time = (
                finalized.updated_at.isoformat()
                if finalized and finalized.updated_at
                else datetime.now(timezone.utc).isoformat()
            )
            return {
                "event": "order_executed",
                "id": finalized.id if finalized else claim_id,
                "order_id": claim_id,
                "broker_order_id": broker_order_id or (
                    finalized.broker_order_id if finalized else None
                ),
                "strategy_name": strategy_name,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": filled_price,
                "pnl": None,
                "user_id": user_id,
                "mode": mode,
                "executed_at": exec_time,
            }

        await session.flush()

        trade = TradeRecord(
            id=str(_uuid.uuid4()),
            order_id=claim_id,
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=filled_price,
            pnl=None,
            mode=mode,
            user_id=user_id,
        )
        session.add(trade)
        position: Optional[PositionRecord] = None
        if create_position:
            position = PositionRecord(
                id=str(_uuid.uuid4()),
                user_id=user_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                side="LONG" if side.upper() == "BUY" else "SHORT",
                quantity=quantity,
                entry_price=filled_price,
                current_price=filled_price,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                mode=mode,
                status="OPEN",
                opened_at=datetime.now(timezone.utc),
            )
            session.add(position)
        await session.flush()

        if position is not None:
            claim.position_id = position.id
        await session.commit()

        exec_time = (
            trade.executed_at.isoformat()
            if trade.executed_at
            else datetime.now(timezone.utc).isoformat()
        )
        return {
            "event": "order_executed",
            "id": trade.id,
            "order_id": claim_id,
            "broker_order_id": broker_order_id,
            "strategy_name": strategy_name,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": filled_price,
            "pnl": None,
            "user_id": user_id,
            "mode": mode,
            "executed_at": exec_time,
        }



def signal_client_order_key(
    *,
    provider: str,
    strategy_name: Optional[str],
    symbol: str,
    side: str,
    quantity: int,
    ts_sec: int,
    signal: Optional[str] = None,
) -> str:
    """Deterministic tenant-less idempotency key for a webhook signal.

    Pins identity to (provider, strategy, signal type, symbol, side, quantity)
    and a coarse per-second timestamp of the delivery.  Re-deliveries of the
    same signal event collapse to one key (no double-dispatch); a genuinely
    new signal in a later second gets a fresh key.  Fits the
    ``ux_orders_signal_key`` partial unique index and the
    ``[A-Za-z0-9._-]{8,64}`` key charset.

    Cross-tenant safety: ``provider`` and ``strategy_name`` (the TradeThrone
    strategy identity) are part of the hash input, so two different strategies
    (or providers) sending the same symbol/side/quantity in the same second
    produce DIFFERENT keys -- no unsafe tenant-less collision.
    """
    import hashlib

    raw = (
        f"{provider}:{strategy_name or ''}:{signal or ''}:{symbol}:{side}:"
        f"{quantity}:{ts_sec}"
    )
    return "sig-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
