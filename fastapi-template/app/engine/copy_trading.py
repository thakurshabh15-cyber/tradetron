"""Institutional High-Speed Copy Trading & Master-Slave Trade Fan-Out Engine.

Handles real-time concurrent trade mirroring via asyncio.gather for sub-50ms execution latency.
Applies lot multipliers, risk caps, and supports dual Paper/Live broker execution modes.

LIVE durable-claim hardening (P0):
    Every LIVE follower entry follows the same two-phase dispatch pattern as
    keyed manual/DMA and autonomous strategy entries:

      SIGNAL → DURABLE PENDING CLAIM → BROKER DISPATCH → PERSIST broker ref
      (own commit) → CAS FINALIZE FILLED + Trade + Position → DONE

    On any dispatch/guard failure the PENDING claim is CAS-rejected (no
    duplicate unkeyed REJECTED rows).  A crash after broker acceptance but
    before finalization leaves a recoverable PENDING row that the existing
    order-reconciliation engine can resolve via Window-B (broker_order_id
    present) or Window-C (broker_order_id absent → get_positions()).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.brokers import (
    BrokerModeBlockedError,
    assert_live_dispatch_allowed,
    get_broker_adapter,
)
from app.models.broker_account import BrokerAccountRecord
from app.models.copy_trading import CopyFollowerRecord, CopyGroupRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.schemas.trading import OrderRequest, Side

logger = get_logger("engine.copy_trading")


# ── Copy-trading durable-claim helpers ──────────────────────────────────────


def _copy_client_order_id(
    master_order_id: str,
    follower_user_id: str,
    symbol: str,
    side: str,
    calc_qty: int,
) -> str:
    """Deterministic idempotency key for a copy-trading LIVE follower entry."""
    raw = f"{master_order_id}:{follower_user_id}:{symbol}:{side}:{calc_qty}"
    return "cpy-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


async def _claim_copy_follower_order(
    *,
    follower_user_id: str,
    broker_account_id: str | None,
    symbol: str,
    side: str,
    quantity: int,
    order_type: str,
    price: float,
    mode: str,
    client_order_id: str,
) -> Optional[str]:
    """Durably claim an idempotency key for a LIVE copy-trading follower entry.

    Returns claim row id on success, None when caller must NOT dispatch.
    """
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == follower_user_id,
                    OrderRecord.client_order_id == client_order_id,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.status in ("FILLED", "PENDING"):
                return None
            retry = await session.execute(
                update(OrderRecord)
                .where(
                    OrderRecord.user_id == follower_user_id,
                    OrderRecord.client_order_id == client_order_id,
                    OrderRecord.status.not_in(("FILLED", "PENDING")),
                )
                .values(
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    order_type=order_type,
                    price=price,
                    mode=mode,
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
            await session.rollback()
            return None

        claim = OrderRecord(
            user_id=follower_user_id,
            client_order_id=client_order_id,
            broker_account_id=broker_account_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            mode=mode,
            status="PENDING",
        )
        session.add(claim)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return None
        return claim.id


async def _persist_copy_follower_broker_ref(
    claim_id: str,
    broker_order_id: str,
) -> None:
    """Persist the broker reference on the PENDING claim in its own commit.

    Window-D hardening: the reference must survive a crash before finalization
    so reconciliation's Window-B path (get_order_status) can read it back.
    """
    async with SessionLocal() as session:
        claim = await session.get(OrderRecord, claim_id)
        if claim is None:
            return
        if not claim.broker_order_id:
            claim.broker_order_id = broker_order_id
            await session.commit()


async def _reject_copy_follower_claim(
    claim_id: str,
    reason: str,
) -> None:
    """CAS-reject a LIVE copy-trading follower claim after dispatch failure."""
    async with SessionLocal() as session:
        result = await session.execute(
            update(OrderRecord)
            .where(
                OrderRecord.id == claim_id,
                OrderRecord.status == "PENDING",
            )
            # A rejected claim is terminal (never reconciled) and the follower
            # order must not retain any broker account reference — in particular
            # a cross-tenant ref must never leak onto a REJECTED order row.  The
            # claim is cleared before finalization only after a confirmed fill.
            .values(
                status="REJECTED",
                error_message=reason,
                broker_account_id=None,
            )
        )
        if result.rowcount == 1:
            await session.commit()
        else:
            await session.rollback()


async def _persist_copy_follower_live_fill(
    *,
    claim_id: str,
    broker_order_id: str,
    symbol: str,
    side: str,
    quantity: int,
    filled_price: float,
    follower_user_id: str,
    broker_account_id: str,
    multiplier: float,
    follower_id: str,
) -> dict[str, Any]:
    """Finalize a LIVE copy-trading follower claim after broker acceptance.

    Two-phase commit:
      1. Persist broker_order_id on the PENDING claim (its own commit).
      2. CAS-finalize FILLED + create Trade + Position atomically.
    """
    async with SessionLocal() as session:
        # Phase 1: persist broker reference (its own commit).
        claim = await session.get(OrderRecord, claim_id)
        if claim is None:
            raise RuntimeError(f"LIVE copy-trading claim {claim_id} not found")
        if not claim.broker_order_id:
            claim.broker_order_id = broker_order_id
            await session.commit()

        # Phase 2: CAS-finalize FILLED + create Trade + Position.
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
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await session.rollback()
            finalized = await session.get(OrderRecord, claim_id)
            return {
                "success": True,
                "follower_user_id": follower_user_id,
                "quantity": quantity,
                "order_id": claim_id,
                "broker_order_id": (
                    finalized.broker_order_id if finalized else broker_order_id
                ),
                "duplicate": True,
            }

        await session.flush()

        trade = TradeRecord(
            id=str(uuid.uuid4()),
            order_id=claim_id,
            strategy_name=f"Copy Trading ({multiplier}x)",
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=filled_price,
            entry_price=filled_price,
            pnl=0.0,
            mode="LIVE",
            user_id=follower_user_id,
        )
        position = PositionRecord(
            id=str(uuid.uuid4()),
            user_id=follower_user_id,
            broker_account_id=broker_account_id,
            symbol=symbol,
            side="LONG" if side.upper() == "BUY" else "SHORT",
            quantity=quantity,
            entry_price=filled_price,
            current_price=filled_price,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            mode="LIVE",
            status="OPEN",
            opened_at=datetime.now(timezone.utc),
        )
        session.add(trade)
        session.add(position)
        await session.flush()

        # Link position back to the order row.
        claim_obj = await session.get(OrderRecord, claim_id)
        if claim_obj:
            claim_obj.position_id = position.id
            await session.flush()

        # Update follower aggregate stats.
        follower_row = await session.get(CopyFollowerRecord, follower_id)
        if follower_row:
            follower_row.total_copied_trades = (
                (follower_row.total_copied_trades or 0) + 1
            )
            session.add(follower_row)

        await session.commit()

        return {
            "success": True,
            "follower_user_id": follower_user_id,
            "quantity": quantity,
            "order_id": claim_id,
        }


class CopyTradingEngine:
    """Core trade fan-out engine replicating Master trader orders to all active follower accounts."""

    def __init__(self) -> None:
        self._is_running = True

    async def mirror_trade(
        self,
        master_order: OrderRecord | dict[str, Any],
        master_user_id: str,
    ) -> dict[str, Any]:
        """Concurrently mirror a master order to all active followers across all master copy groups."""
        start_time = time.perf_counter()

        # Extract master order details
        if isinstance(master_order, OrderRecord):
            symbol = master_order.symbol
            side = master_order.side
            master_qty = master_order.quantity
            order_type = master_order.order_type
            price = master_order.price or master_order.filled_price or 1000.0
            mode = master_order.mode
            master_order_id = master_order.id
        else:
            symbol = master_order.get("symbol", "NIFTY50")
            side = master_order.get("side", "BUY")
            master_qty = master_order.get("quantity", 1)
            order_type = master_order.get("order_type", "MARKET")
            price = master_order.get("price") or master_order.get("filled_price") or 1000.0
            mode = master_order.get("mode", "PAPER")
            # Deterministic fallback for dict callers without a persisted order id.
            master_order_id = master_order.get("id", "")
            if not master_order_id:
                _raw = f"{symbol}:{side}:{master_qty}:{order_type}:{price}:{mode}"
                master_order_id = "dict-" + hashlib.sha256(_raw.encode("utf-8")).hexdigest()[:32]

        clean_sym = symbol.upper().strip()

        async with SessionLocal() as db:
            # 1. Fetch all active groups owned by the master
            groups_stmt = select(CopyGroupRecord).where(
                CopyGroupRecord.master_user_id == master_user_id,
                CopyGroupRecord.is_active.is_(True),
            )
            groups_res = await db.execute(groups_stmt)
            groups = groups_res.scalars().all()

            if not groups:
                return {
                    "mirrored": False,
                    "reason": "Master has no active copy groups",
                    "total_followers": 0,
                    "successful_copies": 0,
                    "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
                }

            group_ids = [g.id for g in groups]

            # 2. Fetch all active followers subscribing to these groups
            followers_stmt = select(CopyFollowerRecord).where(
                CopyFollowerRecord.group_id.in_(group_ids),
                CopyFollowerRecord.status == "ACTIVE",
            )
            followers_res = await db.execute(followers_stmt)
            followers = followers_res.scalars().all()

            if not followers:
                return {
                    "mirrored": False,
                    "reason": "No active followers in copy groups",
                    "total_followers": 0,
                    "successful_copies": 0,
                    "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
                }

            # 3. Build concurrent mirror execution tasks
            tasks = [
                self._execute_single_follower_order(
                    follower=follower,
                    symbol=clean_sym,
                    side=side,
                    master_qty=master_qty,
                    order_type=order_type,
                    price=price,
                    master_mode=mode,
                    master_order_id=master_order_id,
                )
                for follower in followers
            ]

            # 4. Execute all follower orders in parallel with asyncio.gather (sub-50ms target)
            results = await asyncio.gather(*tasks, return_exceptions=True)

            successful = 0
            failed = 0
            for r in results:
                if isinstance(r, dict) and r.get("success"):
                    successful += 1
                else:
                    failed += 1
                    if isinstance(r, Exception):
                        logger.error("Error executing follower copy order: %s", r)

            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(
                "⚡ [CopyTrading] Fan-out complete for Master %s on %s: %d/%d successful in %.2f ms",
                master_user_id,
                clean_sym,
                successful,
                len(followers),
                latency_ms,
            )

            return {
                "mirrored": True,
                "total_followers": len(followers),
                "successful_copies": successful,
                "failed_copies": failed,
                "latency_ms": latency_ms,
            }

    async def _execute_single_follower_order(
        self,
        follower: CopyFollowerRecord,
        symbol: str,
        side: str,
        master_qty: int,
        order_type: str,
        price: float,
        master_mode: str,
        master_order_id: str,
    ) -> dict[str, Any]:
        """Execute a single follower order.

        LIVE follower entries follow the durable-claim state machine:

            SIGNAL -> DURABLE PENDING CLAIM -> BROKER DISPATCH -> PERSIST
            broker ref (own commit) -> CAS FINALIZE FILLED + Trade + Position
            -> DONE

        A crash at any point after the claim leaves a recoverable PENDING row
        the order-reconciliation engine can resolve.
        """
        # 1. Calculate lot-multiplier scaled quantity
        multiplier = follower.multiplier or 1.0
        calc_qty = max(1, int(round(master_qty * multiplier)))

        # 2. Risk check: Max allocation cap
        total_val = calc_qty * price
        if follower.max_allocation and total_val > follower.max_allocation:
            if price > 0:
                calc_qty = max(1, int(follower.max_allocation / price))
            else:
                calc_qty = 1

        # 3. Follower execution mode.
        #    ── Safety invariant (C/E): A PAPER master signal can never
        #    manufacture a LIVE fill for a follower, so a paper master
        #    always fans out as PAPER bookkeeping. Only a LIVE master
        #    with a LIVE follower may reach a real broker.
        follower_mode = (
            "PAPER" if master_mode != "LIVE" else (follower.mode or "PAPER")
        )

        if follower_mode == "LIVE":
            return await self._execute_live_follower_order(
                follower=follower,
                symbol=symbol,
                side=side,
                calc_qty=calc_qty,
                order_type=order_type,
                price=price,
                master_order_id=master_order_id,
                multiplier=multiplier,
            )

        async with SessionLocal() as db:
            try:
                # PAPER path: persist FILLED OrderRecord + Trade + Position
                order_id = f"CPY_ORD_{int(datetime.now(timezone.utc).timestamp())}_{str(uuid.uuid4())[:6]}"
                order = OrderRecord(
                    id=str(uuid.uuid4()),
                    user_id=follower.follower_user_id,
                    symbol=symbol,
                    side=side,
                    quantity=calc_qty,
                    order_type=order_type,
                    price=price,
                    filled_price=price,
                    filled_quantity=calc_qty,
                    status="FILLED",
                    mode="PAPER",
                )
                db.add(order)

                # Persist Follower TradeRecord
                trade = TradeRecord(
                    id=str(uuid.uuid4()),
                    order_id=order.id,
                    strategy_name=f"Copy Trading ({multiplier}x)",
                    symbol=symbol,
                    side=side,
                    quantity=calc_qty,
                    price=price,
                    entry_price=price,
                    pnl=0.0,
                    mode="PAPER",
                    user_id=follower.follower_user_id,
                )
                db.add(trade)

                # Persist Follower Open PositionRecord
                position = PositionRecord(
                    id=str(uuid.uuid4()),
                    user_id=follower.follower_user_id,
                    symbol=symbol,
                    side="LONG" if side == "BUY" else "SHORT",
                    quantity=calc_qty,
                    entry_price=price,
                    current_price=price,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    mode="PAPER",
                    status="OPEN",
                    opened_at=datetime.now(timezone.utc),
                )
                db.add(position)
                order.position_id = position.id

                # Update Follower stats
                follower_row = await db.get(CopyFollowerRecord, follower.id)
                if follower_row:
                    follower_row.total_copied_trades = (follower_row.total_copied_trades or 0) + 1
                    db.add(follower_row)

                await db.commit()

                from app.engine.alerts import notify_trade_fill
                await notify_trade_fill(
                    follower.follower_user_id,
                    symbol=symbol,
                    side=side,
                    quantity=calc_qty,
                    price=price,
                    mode="PAPER",
                )

                return {
                    "success": True,
                    "follower_user_id": follower.follower_user_id,
                    "quantity": calc_qty,
                    "order_id": order_id,
                }
            except Exception as exc:
                logger.error(
                    "Failed to copy trade for follower %s: %s",
                    follower.follower_user_id,
                    exc,
                )
                return {"success": False, "error": str(exc)}

    async def _execute_live_follower_order(
        self,
        *,
        follower: CopyFollowerRecord,
        symbol: str,
        side: str,
        calc_qty: int,
        order_type: str,
        price: float,
        master_order_id: str,
        multiplier: float,
    ) -> dict[str, Any]:
        """Execute a single LIVE follower order with the durable-claim machine.

        Safety invariants preserved:
          A. The broker account is re-derived server-side from the follower row
             and MUST be owned by ``follower.follower_user_id``.
          B. ``assert_live_dispatch_allowed()`` runs BEFORE any broker call.
          C. FILLED/OPEN state is persisted ONLY after a confirmed broker fill.
          D. A claim helper returning None (duplicate/in-flight) NEVER dispatches.
        """
        # Invariant A (pre-claim): a follower with no linked broker account can
        # never reach a broker.  No claim is created in that case.
        if not follower.broker_account_id:
            logger.warning(
                "Copy trade REJECTED for follower %s on %s: no linked broker account",
                follower.follower_user_id,
                symbol,
            )
            return {
                "success": False,
                "follower_user_id": follower.follower_user_id,
                "reason": "no_owned_broker",
            }

        # Durable PENDING claim BEFORE broker dispatch.
        claim_key = _copy_client_order_id(
            master_order_id,
            follower.follower_user_id,
            symbol,
            side,
            calc_qty,
        )
        claim_id = await _claim_copy_follower_order(
            follower_user_id=follower.follower_user_id,
            broker_account_id=follower.broker_account_id,
            symbol=symbol,
            side=side,
            quantity=calc_qty,
            order_type=order_type,
            price=price,
            mode="LIVE",
            client_order_id=claim_key,
        )
        if claim_id is None:
            # Same key already FILLED or PENDING -- never dispatch a second
            # real order for the same signal.
            logger.info(
                "Copy trade skipped for follower %s on %s: duplicate/in-flight claim",
                follower.follower_user_id,
                symbol,
            )
            return {
                "success": True,
                "follower_user_id": follower.follower_user_id,
                "quantity": calc_qty,
                "duplicate": True,
            }

        # Broker dispatch (validation + dispatch only; NO DB mutations).
        outcome = await self._dispatch_live_follower_order(
            follower=follower,
            symbol=symbol,
            side=side,
            calc_qty=calc_qty,
            order_type=order_type,
            price=price,
        )
        if not outcome.get("success"):
            # CAS-reject the SAME durable claim -- never a second unkeyed row.
            await _reject_copy_follower_claim(
                claim_id,
                outcome.get("reason", "live_dispatch_failed"),
            )
            return outcome

        broker_order_id_ref = outcome.get("broker_order_id_ref")
        if not broker_order_id_ref:
            # Ambiguous broker response: the broker may have accepted the order
            # but returned no reference.  Leave the durable PENDING claim
            # recoverable via the existing Window-C reconciliation path
            # (get_positions).  NEVER fabricate a fill or a synthetic ref, and
            # never dispatch again because the response was ambiguous.
            logger.warning(
                "Copy trade accepted WITHOUT broker reference for follower %s on %s; "
                "claim %s left PENDING for Window-C reconciliation",
                follower.follower_user_id,
                symbol,
                claim_id,
            )
            return {
                "success": True,
                "follower_user_id": follower.follower_user_id,
                "quantity": calc_qty,
                "order_id": claim_id,
                "pending_recovery": True,
            }

        filled_price = outcome.get("filled_price")
        if not filled_price:
            # Reference known but fill state uncertain: persist the broker ref
            # in its own commit and leave the claim PENDING for Window-B
            # reconciliation (get_order_status).
            await _persist_copy_follower_broker_ref(
                claim_id, broker_order_id_ref,
            )
            logger.info(
                "Copy trade accepted with broker ref %s for follower %s on %s; "
                "fill unconfirmed, claim %s left PENDING for Window-B reconciliation",
                broker_order_id_ref,
                follower.follower_user_id,
                symbol,
                claim_id,
            )
            return {
                "success": True,
                "follower_user_id": follower.follower_user_id,
                "quantity": calc_qty,
                "order_id": claim_id,
                "pending_recovery": True,
            }

        # Two-phase finalization: broker ref (own commit) then CAS FILLED.
        try:
            return await _persist_copy_follower_live_fill(
                claim_id=claim_id,
                broker_order_id=broker_order_id_ref,
                symbol=symbol,
                side=side,
                quantity=calc_qty,
                filled_price=filled_price,
                follower_user_id=follower.follower_user_id,
                broker_account_id=outcome["broker_account_id"],
                multiplier=multiplier,
                follower_id=follower.id,
            )
        except Exception as exc:
            # Finalization failed AFTER broker acceptance.  The durable PENDING
            # claim + broker ref keep the order recoverable by reconciliation.
            logger.error(
                "Failed to finalize copy trade for follower %s: %s",
                follower.follower_user_id,
                exc,
            )
            return {
                "success": False,
                "follower_user_id": follower.follower_user_id,
                "reason": "finalization_failed_recoverable",
                "order_id": claim_id,
            }

    async def _dispatch_live_follower_order(
            self,
            follower: CopyFollowerRecord,
            symbol: str,
            side: str,
            calc_qty: int,
            order_type: str,
            price: float,
        ) -> dict[str, Any]:
            """Resolve the follower's OWN broker account and dispatch a REAL live order.

            Safety invariants enforced here (P0-1a / P0-1b):
              A. The broker account is re-derived from server data (the follower row)
                 and MUST be owned by ``follower.follower_user_id`` - the client can
                 never select another user's broker account.
              B. ``assert_live_dispatch_allowed()`` is invoked before any broker call;
                 when the deployment is not explicitly ``BROKER_MODE=live`` the guard
                 raises ``BrokerModeBlockedError``.

            This helper performs NO database mutations.  The caller owns claim
            persistence: on failure the durable PENDING claim is CAS-rejected.
            """
            if not follower.broker_account_id:
                logger.warning(
                    "Copy trade blocked for follower %s on %s: no linked broker account",
                    follower.follower_user_id,
                    symbol,
                )
                return {
                    "success": False,
                    "follower_user_id": follower.follower_user_id,
                    "reason": "no_owned_broker",
                }

            # Resolve the follower's own CONNECTED, active broker account (server-
            # side ownership re-enforcement, own session - never another user's).
            async with SessionLocal() as broker_db:
                broker_stmt = select(BrokerAccountRecord).where(
                    BrokerAccountRecord.id == follower.broker_account_id,
                    BrokerAccountRecord.user_id == follower.follower_user_id,
                    BrokerAccountRecord.status == "CONNECTED",
                    BrokerAccountRecord.is_active.is_(True),
                )
                broker_rec = (await broker_db.execute(broker_stmt)).scalars().first()

            if not broker_rec:
                logger.warning(
                    "Copy trade blocked for follower %s on %s: no owned connected broker",
                    follower.follower_user_id,
                    symbol,
                )
                return {
                    "success": False,
                    "follower_user_id": follower.follower_user_id,
                    "reason": "no_owned_broker",
                }

            # Safety invariant (B): LIVE dispatch MUST pass the guard before any
            # broker call is made.
            try:
                assert_live_dispatch_allowed()
            except BrokerModeBlockedError as guard_exc:
                logger.warning(
                    "Copy trade blocked for follower %s on %s: LIVE dispatch blocked (%s)",
                    follower.follower_user_id,
                    symbol,
                    guard_exc,
                )
                return {
                    "success": False,
                    "follower_user_id": follower.follower_user_id,
                    "reason": "live_dispatch_blocked",
                }

            # Confirm-before-fill: dispatch through the follower's own adapter.
            broker_client = get_broker_adapter(broker_rec)
            broker_req = OrderRequest(
                symbol=symbol,
                side=Side.BUY if side == "BUY" else Side.SELL,
                quantity=calc_qty,
                order_type=order_type,
            )
            try:
                broker_resp = await broker_client.place_order(broker_req)
            except Exception as broker_exc:
                logger.error(
                    "Copy trade dispatch FAILED for follower %s on %s: %s",
                    follower.follower_user_id,
                    symbol,
                    broker_exc,
                )
                return {
                    "success": False,
                    "follower_user_id": follower.follower_user_id,
                    "reason": "broker_dispatch_failed",
                }

            # Confirmed successful broker dispatch - the caller may now persist
            # FILLED/OPEN state, but ONLY from the broker's own returned data.
            # Per the durable-claim design, a MISSING broker reference must
            # NEVER be replaced by a synthetic one: a fabricated ref would make
            # reconciliation's Window-B path query the broker for a fake id and
            # be stuck forever; leaving the ref empty routes recovery through
            # Window-C (get_positions) which can detect real exposure.
            broker_order_id_ref = (
                broker_resp.get("order_id")
                or broker_resp.get("broker_order_id")
                or None
            )
            resp_price = broker_resp.get("filled_price") or broker_resp.get("price")
            filled_price = round(float(resp_price), 2) if resp_price else None
            logger.info(
                "Copy trade dispatched LIVE for follower %s on %s via broker %s: %s",
                follower.follower_user_id,
                symbol,
                broker_rec.broker_name,
                broker_resp,
            )
            return {
                "success": True,
                "broker_account_id": broker_rec.id,
                "broker_order_id_ref": broker_order_id_ref,
                "filled_price": filled_price,
            }

    async def mirror_close_position(
        self,
        symbol: str,
        master_user_id: str,
        exit_price: float,
    ) -> dict[str, Any]:
        """Concurrently close matching open positions across all active followers of master."""
        start_time = time.perf_counter()
        clean_sym = symbol.upper().strip()

        async with SessionLocal() as db:
            # 1. Fetch active copy groups
            groups_stmt = select(CopyGroupRecord).where(
                CopyGroupRecord.master_user_id == master_user_id,
                CopyGroupRecord.is_active.is_(True),
            )
            groups_res = await db.execute(groups_stmt)
            groups = groups_res.scalars().all()
            if not groups:
                return {"mirrored": False, "closed_count": 0}

            group_ids = [g.id for g in groups]
            followers_stmt = select(CopyFollowerRecord).where(
                CopyFollowerRecord.group_id.in_(group_ids),
                CopyFollowerRecord.status == "ACTIVE",
            )
            followers_res = await db.execute(followers_stmt)
            followers = followers_res.scalars().all()
            if not followers:
                return {"mirrored": False, "closed_count": 0}

            follower_user_ids = [f.follower_user_id for f in followers]

            # 2. Find open positions on this symbol for these followers
            pos_stmt = select(PositionRecord).where(
                PositionRecord.user_id.in_(follower_user_ids),
                PositionRecord.symbol == clean_sym,
                PositionRecord.status == "OPEN",
            )
            pos_res = await db.execute(pos_stmt)
            positions = pos_res.scalars().all()

            if not positions:
                return {"mirrored": False, "closed_count": 0}

            # Map each OPEN position to the server-side follower subscription row
            # that owns it.  V3 invariant A: the follower identity - and therefore
            # the broker account used for a LIVE close - is derived from server
            # data (the follower row), never from request fields.
            followers_by_user = {f.follower_user_id: f for f in followers}

            # 3. Concurrently close all follower positions
            tasks = []
            for pos in positions:
                follower = followers_by_user.get(pos.user_id)
                if follower is None:
                    logger.warning(
                        "Mirrored close: no active follower row for user %s on %s; "
                        "position %s left untouched",
                        pos.user_id, clean_sym, pos.id,
                    )
                    continue
                tasks.append(
                    self._close_single_follower_position(
                        pos=pos,
                        follower=follower,
                        exit_price=exit_price,
                    )
                )

            if not tasks:
                return {"mirrored": False, "closed_count": 0}

            results = await asyncio.gather(*tasks, return_exceptions=True)
            closed_count = sum(1 for r in results if isinstance(r, dict) and r.get("success"))

            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(
                "⚡ [CopyTrading] Mirrored position close on %s for Master %s: %d positions closed in %.2f ms",
                clean_sym,
                master_user_id,
                closed_count,
                latency_ms,
            )

            return {
                "mirrored": True,
                "closed_count": closed_count,
                "latency_ms": latency_ms,
            }

    async def _close_single_follower_position(
        self,
        pos: PositionRecord,
        follower: CopyFollowerRecord,
        exit_price: float,
    ) -> dict[str, Any]:
        """Close a follower position and calculate realized PnL.

        V3 LIVE-close safety invariants (mirror the entry-side invariants):

          A. The broker account is re-derived server-side from the follower
             subscription row and MUST be owned by ``follower.follower_user_id``.
          B. ``assert_live_dispatch_allowed()`` runs BEFORE any LIVE close
             dispatch.
          C. CLOSED / Trade / PnL state for a LIVE position is persisted ONLY
             after the follower's own broker adapter confirms the exit fill;
             guard blocks and broker failures persist a REJECTED close
             OrderRecord and NEVER fabricate a successful close.
          D. PAPER closes remain pure bookkeeping (no broker involved).
        """
        async with SessionLocal() as db:
            try:
                p = await db.get(PositionRecord, pos.id)
                if not p or p.status != "OPEN":
                    return {"success": False}

                # Reload the follower subscription server-side so a stale/corrupt
                # caller reference can never route the close through another
                # user's broker account (invariant A).
                follower_row = await db.get(CopyFollowerRecord, follower.id)
                if follower_row is None:
                    logger.warning(
                        "Copy close: follower subscription %s missing for user %s; "
                        "position %s untouched",
                        follower.id, pos.user_id, pos.id,
                    )
                    return {"success": False}

                is_long = p.side in ("LONG", "BUY")
                closing_side = "SELL" if is_long else "BUY"
                effective_exit_price = float(exit_price)

                # P0 FK FIX baseline: the LIVE branch below creates a REAL
                # close OrderRecord; PAPER closes create theirs in the shared
                # tail so the exit TradeRecord references an actual orders.id
                # UUID (never a synthetic CPY_EXIT_* display string).
                close_order: Optional[OrderRecord] = None

                if p.mode == "LIVE":
                    # ── LIVE close: broker confirmation is mandatory ──────────────
                    # A. Resolve the follower's OWN CONNECTED, active broker account
                    #    from server data (the follower row) - never trust request
                    #    fields or a stored account reference.
                    broker_stmt = select(BrokerAccountRecord).where(
                        BrokerAccountRecord.id == follower_row.broker_account_id,
                        BrokerAccountRecord.user_id == follower_row.follower_user_id,
                        BrokerAccountRecord.status == "CONNECTED",
                        BrokerAccountRecord.is_active.is_(True),
                    )
                    broker_rec = (await db.execute(broker_stmt)).scalars().first()
                    if not broker_rec:
                        await self._persist_rejected_close_order(
                            db, follower_row, p, closing_side,
                            "(V3) LIVE close blocked: no CONNECTED broker account "
                            "owned by this follower.",
                        )
                        return {"success": False, "reason": "no_owned_broker"}

                if p.mode == "LIVE":
                    # B. LIVE dispatch MUST pass the guard before any broker call.
                    try:
                        assert_live_dispatch_allowed()
                    except BrokerModeBlockedError as guard_exc:
                        await self._persist_rejected_close_order(
                            db, follower_row, p, closing_side,
                            f"(V3) LIVE close blocked: {guard_exc}",
                        )
                        return {"success": False, "reason": "live_dispatch_blocked"}

                    # ── P1 FIX: Atomic CAS claim (LIVE copy-trading close) ──
                    # Claim the position atomically before dispatching a real
                    # broker close order.  Only one concurrent caller wins
                    # (rowcount == 1); losers skip.  The claim is COMMITTED
                    # immediately below (before dispatch), and if broker
                    # dispatch fails it is explicitly reverted to OPEN before
                    # the REJECTED order is committed by
                    # _persist_rejected_close_order.
                    cas_result = await db.execute(
                        update(PositionRecord)
                        .where(PositionRecord.id == p.id, PositionRecord.status == "OPEN")
                        .values(status="CLOSED", closed_at=datetime.now(timezone.utc))
                    )
                    if cas_result.rowcount != 1:
                        return {"success": False, "reason": "already_closed"}
                    p.status = "CLOSED"
                    p.closed_at = datetime.now(timezone.utc)

                    # â”€â”€ P1 FIX: Durability â€” commit the CAS claim BEFORE dispatch â”€â”€
                    # The live-dispatch guard and broker resolution have both
                    # passed.  Commit the CAS now so the CLOSED claim is DURABLE
                    # before the real broker order leaves the process.  A crash
                    # before dispatch yields a recoverable "phantom close"
                    # (DB=CLOSED / exchange=OPEN) that can never cause a second
                    # broker action; the pre-fix dispatch-then-commit order could
                    # crash with DB=OPEN / exchange=CLOSED and a subsequent
                    # retry would dispatch a second real close order.  If the
                    # dispatch fails below, the committed CAS is explicitly
                    # reverted to OPEN before the REJECTED order is committed.
                    await db.commit()
                    await db.refresh(p)

                    # Dispatch the real closing order through the follower's own
                    # broker adapter (invariant B).
                    broker_client = get_broker_adapter(broker_rec)
                    broker_req = OrderRequest(
                        symbol=p.symbol,
                        side=Side.SELL if is_long else Side.BUY,
                        quantity=p.quantity,
                        order_type="MARKET",
                    )
                    try:
                        broker_resp = await broker_client.place_order(broker_req)
                    except Exception as broker_exc:
                        # P1 FIX: Revert the now-COMMITTED CAS claim before
                        # persisting the rejection, because
                        # _persist_rejected_close_order commits.  Without this
                        # revert the position would stay durably CLOSED with
                        # only a REJECTED order — a fabricated close.
                        await db.execute(
                            update(PositionRecord)
                            .where(PositionRecord.id == p.id)
                            .values(status="OPEN", closed_at=None)
                        )
                        p.status = "OPEN"
                        p.closed_at = None
                        await self._persist_rejected_close_order(
                            db, follower_row, p, closing_side,
                            f"(V3) LIVE close rejected by broker: {broker_exc}",
                        )
                        return {"success": False, "reason": "broker_dispatch_failed"}

                    # C. Confirmed broker exit - only now may CLOSED financial state
                    #    be persisted (invariant C), using the broker-confirmed fill
                    #    price when one is returned.
                    broker_order_id_ref = (
                        broker_resp.get("order_id")
                        or broker_resp.get("broker_order_id")
                        or f"CPY_CLOSE_{str(uuid.uuid4())[:6]}"
                    )
                    resp_price = broker_resp.get("filled_price") or broker_resp.get("price")
                    if resp_price:
                        effective_exit_price = round(float(resp_price), 2)

                    close_order = OrderRecord(
                        id=str(uuid.uuid4()),
                        user_id=follower_row.follower_user_id,
                        broker_account_id=broker_rec.id,
                        broker_order_id=broker_order_id_ref,
                        symbol=p.symbol,
                        side=closing_side,
                        quantity=p.quantity,
                        order_type="MARKET",
                        price=p.current_price or p.entry_price,
                        filled_price=effective_exit_price,
                        filled_quantity=p.quantity,
                        status="FILLED",
                        mode="LIVE",
                    )
                    db.add(close_order)
                    logger.info(
                        "Copy LIVE close dispatched for follower %s on %s via broker %s: %s",
                        follower_row.follower_user_id, p.symbol, broker_rec.broker_name,
                        broker_resp,
                    )

                # Realized PnL is always computed from the broker-confirmed exit
                # price (LIVE) / the master exit price (PAPER) so the booked trade
                # matches the executed fill.
                delta = (effective_exit_price - p.entry_price) if is_long else (p.entry_price - effective_exit_price)
                realized_pnl = round(delta * p.quantity, 2)
                pnl_pct = round((delta / p.entry_price) * 100, 2) if p.entry_price else 0.0

                # For PAPER positions the CAS claim was not applied above (no
                # broker dispatch path); claim now before booking financial state.
                # For LIVE positions the CAS was already claimed before dispatch.
                if p.status != "CLOSED":
                    paper_cas = await db.execute(
                        update(PositionRecord)
                        .where(PositionRecord.id == p.id, PositionRecord.status == "OPEN")
                        .values(status="CLOSED", closed_at=datetime.now(timezone.utc))
                    )
                    if paper_cas.rowcount != 1:
                        return {"success": False, "reason": "already_closed"}
                    p.status = "CLOSED"
                    p.closed_at = datetime.now(timezone.utc)

                # pos.status and pos.closed_at already set by CAS above
                p.current_price = effective_exit_price
                p.realized_pnl = realized_pnl
                p.unrealized_pnl = 0.0
                db.add(p)

                # P0 FK FIX: LIVE closes already persisted a FILLED close OrderRecord
                # above; PAPER closes are pure bookkeeping with no broker
                # dispatch, so create the bookkeeping close order now. Either
                # way the exit TradeRecord must reference a REAL orders.id UUID
                # (pre-fix it stored a synthetic CPY_EXIT_* display string that
                # violates trades.order_id -> orders.id on Postgres).
                if close_order is None:
                    close_order = OrderRecord(
                        id=str(uuid.uuid4()),
                        user_id=p.user_id,
                        broker_account_id=p.broker_account_id,
                        symbol=p.symbol,
                        side=closing_side,
                        quantity=p.quantity,
                        order_type="MARKET",
                        price=effective_exit_price,
                        filled_price=effective_exit_price,
                        filled_quantity=p.quantity,
                        status="FILLED",
                        mode=p.mode,
                    )
                    db.add(close_order)

                # Record closing trade
                trade = TradeRecord(
                    id=str(uuid.uuid4()),
                    order_id=close_order.id,
                    strategy_name="Copy Trading Exit",
                    symbol=p.symbol,
                    side=closing_side,
                    quantity=p.quantity,
                    price=effective_exit_price,
                    entry_price=p.entry_price,
                    exit_price=effective_exit_price,
                    pnl=realized_pnl,
                    pnl_pct=pnl_pct,
                    exit_reason="MASTER_SIGNAL_EXIT",
                    mode=p.mode,
                    user_id=p.user_id,
                )
                db.add(trade)

                # Update the POSITION OWNER's paper balance if paper mode.
                # P1-1 hardening: resolve the owner from the position row
                # (p.user_id) via the ONE shared owner-scoped primitive — the
                # caller/follower context can never redirect the credit.
                if p.mode == "PAPER":
                    from app.engine.paper_account import credit_paper_pnl

                    await credit_paper_pnl(db, p.user_id, realized_pnl)

                # Update follower aggregate stats
                follower_stmt = select(CopyFollowerRecord).where(
                    CopyFollowerRecord.follower_user_id == p.user_id,
                    CopyFollowerRecord.status == "ACTIVE",
                )
                f_res = await db.execute(follower_stmt)
                follower_rec = f_res.scalars().first()
                if follower_rec:
                    follower_rec.realized_pnl = round((follower_rec.realized_pnl or 0.0) + realized_pnl, 2)
                    db.add(follower_rec)

                await db.commit()
                return {"success": True, "realized_pnl": realized_pnl}
            except Exception as exc:
                logger.error("Error closing follower position %s: %s", pos.id, exc)
                return {"success": False, "error": str(exc)}

    async def _persist_rejected_close_order(
        self,
        db: AsyncSession,
        follower: CopyFollowerRecord,
        p: PositionRecord,
        closing_side: str,
        error_message: str,
    ) -> None:
        """Persist the ONLY state allowed when a LIVE close is blocked or fails.

        A REJECTED close OrderRecord is committed and the OPEN position / trade /
        PnL state is left untouched - a failed live close is never fabricated.
        """
        rejected = OrderRecord(
            id=str(uuid.uuid4()),
            user_id=follower.follower_user_id,
            symbol=p.symbol,
            side=closing_side,
            quantity=p.quantity,
            order_type="MARKET",
            price=p.current_price or p.entry_price,
            filled_quantity=0,
            status="REJECTED",
            mode="LIVE",
            error_message=error_message,
        )
        db.add(rejected)
        await db.commit()
        logger.warning(
            "Copy LIVE close REJECTED for follower %s on %s: %s",
            follower.follower_user_id, p.symbol, error_message,
        )


copy_trading_engine = CopyTradingEngine()
