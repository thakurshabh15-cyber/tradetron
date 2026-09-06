"""Institutional High-Speed Copy Trading & Master-Slave Trade Fan-Out Engine.

Handles real-time concurrent trade mirroring via asyncio.gather for sub-50ms execution latency.
Applies lot multipliers, risk caps, and supports dual Paper/Live broker execution modes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
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
from app.models.user import UserRecord
from app.schemas.trading import OrderRequest, Side

logger = get_logger("engine.copy_trading")


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
        else:
            symbol = master_order.get("symbol", "NIFTY50")
            side = master_order.get("side", "BUY")
            master_qty = master_order.get("quantity", 1)
            order_type = master_order.get("order_type", "MARKET")
            price = master_order.get("price") or master_order.get("filled_price") or 1000.0
            mode = master_order.get("mode", "PAPER")

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
    ) -> dict[str, Any]:
        """Execute a single follower order inside an isolated session."""
        async with SessionLocal() as db:
            try:
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

                # LIVE dispatch state, resolved below (invariant A/B/D).
                broker_account_id: str | None = None
                broker_order_id_ref: str | None = None
                filled_price: float | None = price

                if follower_mode == "LIVE":
                    # ── Safety invariant (A/B/C/D): resolve the follower's OWN
                    #    connected broker account from server data, gate the LIVE
                    #    dispatch behind assert_live_dispatch_allowed(), and only
                    #    persist FILLED/OPEN after a confirmed broker fill.
                    _live_outcome = await self._dispatch_live_follower_order(
                        db=db,
                        follower=follower,
                        symbol=symbol,
                        side=side,
                        calc_qty=calc_qty,
                        order_type=order_type,
                        price=price,
                    )
                    if not _live_outcome["success"]:
                        return _live_outcome
                    broker_account_id = _live_outcome["broker_account_id"]
                    broker_order_id_ref = _live_outcome["broker_order_id_ref"]
                    filled_price = _live_outcome["filled_price"]

                # 4. Persist Follower OrderRecord (FILLED only after confirmed fill;
                #    PAPER fills immediately as before)
                order_id = f"CPY_ORD_{int(datetime.now(timezone.utc).timestamp())}_{str(uuid.uuid4())[:6]}"
                order = OrderRecord(
                    id=str(uuid.uuid4()),
                    user_id=follower.follower_user_id,
                    broker_account_id=broker_account_id,
                    broker_order_id=broker_order_id_ref or order_id,
                    symbol=symbol,
                    side=side,
                    quantity=calc_qty,
                    order_type=order_type,
                    price=price,
                    filled_price=filled_price,
                    filled_quantity=calc_qty,
                    status="FILLED",
                    mode=follower_mode,
                )
                db.add(order)

                # 5. Persist Follower TradeRecord
                trade = TradeRecord(
                    id=str(uuid.uuid4()),
                    order_id=order_id,
                    strategy_name=f"Copy Trading ({multiplier}x)",
                    symbol=symbol,
                    side=side,
                    quantity=calc_qty,
                    price=filled_price or price,
                    entry_price=filled_price or price,
                    pnl=0.0,
                    mode=follower_mode,
                    user_id=follower.follower_user_id,
                )
                db.add(trade)

                # 6. Persist Follower Open PositionRecord
                pos_side = "LONG" if side == "BUY" else "SHORT"
                position = PositionRecord(
                    id=str(uuid.uuid4()),
                    user_id=follower.follower_user_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                    side=pos_side,
                    quantity=calc_qty,
                    entry_price=filled_price or price,
                    current_price=filled_price or price,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    mode=follower_mode,
                    status="OPEN",
                    opened_at=datetime.now(timezone.utc),
                )
                db.add(position)

                # 7. Update Follower stats
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
                    price=filled_price or price,
                    mode=follower_mode,
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

    async def _dispatch_live_follower_order(
            self,
            db: AsyncSession,
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
              C/D. On ANY guard block or broker failure a REJECTED OrderRecord is
                 persisted and NO FILLED/OPEN Trade/Position state is created - a
                 live copy-trade is never fabricated.

            Returns an outcome dict: ``{"success": False, ...}`` with a REJECTED
            order already committed, or ``{"success": True, broker_account_id,
            broker_order_id_ref, filled_price}`` for a confirmed broker fill.
            """
            if not follower.broker_account_id:
                rejected = OrderRecord(
                    id=str(uuid.uuid4()),
                    user_id=follower.follower_user_id,
                    symbol=symbol,
                    side=side,
                    quantity=calc_qty,
                    order_type=order_type,
                    price=price,
                    filled_quantity=0,
                    status="REJECTED",
                    mode="LIVE",
                    error_message=(
                        "Copy-trade LIVE dispatch blocked: no broker account is linked "
                        "to this follower."
                    ),
                )
                db.add(rejected)
                await db.commit()
                logger.warning(
                    "Copy trade REJECTED for follower %s on %s: no linked broker account",
                    follower.follower_user_id,
                    symbol,
                )
                return {
                    "success": False,
                    "follower_user_id": follower.follower_user_id,
                    "reason": "no_owned_broker",
                    "order_id": rejected.id,
                }

            # Resolve the follower's own CONNECTED, active broker account - the
            # ownership constraint enforced at the API layer is re-enforced here
            # server-side so a stale/corrupt row can never route elsewhere.
            broker_stmt = select(BrokerAccountRecord).where(
                BrokerAccountRecord.id == follower.broker_account_id,
                BrokerAccountRecord.user_id == follower.follower_user_id,
                BrokerAccountRecord.status == "CONNECTED",
                BrokerAccountRecord.is_active.is_(True),
            )
            broker_rec = (await db.execute(broker_stmt)).scalars().first()

            if not broker_rec:
                rejected = OrderRecord(
                    id=str(uuid.uuid4()),
                    user_id=follower.follower_user_id,
                    symbol=symbol,
                    side=side,
                    quantity=calc_qty,
                    order_type=order_type,
                    price=price,
                    filled_quantity=0,
                    status="REJECTED",
                    mode="LIVE",
                    error_message=(
                        "Copy-trade LIVE dispatch blocked: no CONNECTED broker account "
                        "owned by this follower."
                    ),
                )
                db.add(rejected)
                await db.commit()
                logger.warning(
                    "Copy trade REJECTED for follower %s on %s: no owned connected broker",
                    follower.follower_user_id,
                    symbol,
                )
                return {
                    "success": False,
                    "follower_user_id": follower.follower_user_id,
                    "reason": "no_owned_broker",
                    "order_id": rejected.id,
                }

            # Safety invariant (B): LIVE dispatch MUST pass the guard before any
            # broker call is made.
            try:
                assert_live_dispatch_allowed()
            except BrokerModeBlockedError as guard_exc:
                rejected = OrderRecord(
                    id=str(uuid.uuid4()),
                    user_id=follower.follower_user_id,
                    broker_account_id=broker_rec.id,
                    symbol=symbol,
                    side=side,
                    quantity=calc_qty,
                    order_type=order_type,
                    price=price,
                    filled_quantity=0,
                    status="REJECTED",
                    mode="LIVE",
                    error_message=f"LIVE dispatch blocked: {guard_exc}",
                )
                db.add(rejected)
                await db.commit()
                logger.warning(
                    "Copy trade REJECTED for follower %s on %s: LIVE dispatch blocked (%s)",
                    follower.follower_user_id,
                    symbol,
                    guard_exc,
                )
                return {
                    "success": False,
                    "follower_user_id": follower.follower_user_id,
                    "reason": "live_dispatch_blocked",
                    "order_id": rejected.id,
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
                rejected = OrderRecord(
                    id=str(uuid.uuid4()),
                    user_id=follower.follower_user_id,
                    broker_account_id=broker_rec.id,
                    symbol=symbol,
                    side=side,
                    quantity=calc_qty,
                    order_type=order_type,
                    price=price,
                    filled_quantity=0,
                    status="REJECTED",
                    mode="LIVE",
                    error_message=f"Broker rejected copy order: {broker_exc}",
                )
                db.add(rejected)
                await db.commit()
                logger.error(
                    "Copy trade REJECTED for follower %s on %s: broker dispatch failed (%s)",
                    follower.follower_user_id,
                    symbol,
                    broker_exc,
                )
                return {
                    "success": False,
                    "follower_user_id": follower.follower_user_id,
                    "reason": "broker_dispatch_failed",
                    "order_id": rejected.id,
                }

            # Confirmed successful broker dispatch - only now may the caller persist
            # FILLED/OPEN state (safety invariant C).
            broker_order_id_ref = (
                broker_resp.get("order_id")
                or broker_resp.get("broker_order_id")
                or f"CPY_BROKER_{str(uuid.uuid4())[:6]}"
            )
            resp_price = broker_resp.get("filled_price") or broker_resp.get("price")
            filled_price = round(float(resp_price), 2) if resp_price else price
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

                # Record closing trade
                trade = TradeRecord(
                    id=str(uuid.uuid4()),
                    order_id=f"CPY_EXIT_{int(datetime.now(timezone.utc).timestamp())}_{str(uuid.uuid4())[:6]}",
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

                # Update follower user's paper balance if paper mode
                if p.mode == "PAPER" and p.user_id:
                    user = await db.get(UserRecord, p.user_id)
                    if user:
                        current_bal = getattr(user, "paper_balance", 1000000.0)
                        user.paper_balance = round(current_bal + realized_pnl, 2)
                        db.add(user)

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
