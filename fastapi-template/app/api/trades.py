"""Trade history and statistics endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user, get_optional_current_user
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.trading import TradeRecord
from app.models.user import UserRecord
from app.schemas.trading import TradePublicRead, TradeRead, TradeStats

logger = get_logger("api.trades")

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("")
async def list_trades(
    symbol: str | None = Query(None, description="Filter by symbol"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_optional_current_user),
):
    """Return paginated trade history, newest first.

    Two contractual views depending on authentication state:

    * **Authenticated** — server-derived ``user.id`` scopes results to the
      caller's own trades.  Full ``TradeRead`` fields returned (``order_id``,
      ``strategy_name``, ``pnl``).
    * **Anonymous** — global public trade tape returned as ``TradePublicRead``
      (``id``, ``symbol``, ``side``, ``quantity``, ``price``, ``executed_at``
      only).  ``pnl``, ``order_id`` and ``strategy_name`` are never exposed.

    Client-supplied ``user_id`` query parameters are silently ignored.
    """
    stmt = select(TradeRecord).order_by(desc(TradeRecord.executed_at))

    if user is not None:
        stmt = stmt.where(TradeRecord.user_id == user.id)

    if symbol:
        stmt = stmt.where(TradeRecord.symbol == symbol.upper())

    stmt = stmt.offset(offset).limit(limit)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    if user is not None:
        return [
            TradeRead(
                id=r.id,
                order_id=r.order_id,
                strategy_name=r.strategy_name,
                symbol=r.symbol,
                side=r.side,
                quantity=r.quantity,
                price=Decimal(str(r.price)),
                pnl=Decimal(str(r.pnl)) if r.pnl is not None else None,
                executed_at=r.executed_at,
            )
            for r in rows
        ]

    return [
        TradePublicRead(
            id=r.id,
            symbol=r.symbol,
            side=r.side,
            quantity=r.quantity,
            price=Decimal(str(r.price)),
            executed_at=r.executed_at,
        )
        for r in rows
    ]


@router.get("/stats", response_model=TradeStats)
async def trade_stats(
    db: AsyncSession = Depends(get_db),
    user: UserRecord = Depends(get_current_user),
):
    """Return the authenticated caller's OWN aggregated trade statistics.

    Requires a valid bearer token — anonymous callers receive HTTP 401.  All
    aggregates are server-scoped to ``user.id`` derived from the token.
    """
    total = await db.scalar(
        select(func.count(TradeRecord.id)).where(TradeRecord.user_id == user.id)
    )
    total = total or 0

    if total == 0:
        return TradeStats()

    result = await db.execute(
        select(TradeRecord).where(TradeRecord.user_id == user.id)
    )
    trades = result.scalars().all()

    pnl_total = sum(t.pnl or 0 for t in trades)
    winning = sum(1 for t in trades if (t.pnl or 0) > 0)
    losing = sum(1 for t in trades if (t.pnl or 0) < 0)

    return TradeStats(
        total_trades=total,
        winning_trades=winning,
        losing_trades=losing,
        total_pnl=Decimal(str(round(pnl_total, 2))),
        win_rate=round(winning / total * 100, 1) if total else 0,
    )


# ── Manual Order Placement with Real Market Price & Open Positions ──────────────
from datetime import datetime, timezone
import uuid
from pydantic import BaseModel, Field
from typing import Optional, Literal
from fastapi import Header, HTTPException
import asyncio
import re
from sqlalchemy.exc import IntegrityError
from app.brokers import BrokerModeBlockedError, get_broker_adapter, assert_live_dispatch_allowed
from app.models.trading import OrderRecord, PositionRecord
from app.models.broker_account import BrokerAccountRecord
from app.market_data.unified_manager import unified_market_manager
from app.market_data.instruments import instrument_master


def _quote_price(quote) -> float | None:
    """Safely extract the last traded price from a unified-market quote.

    ``unified_market_manager.get_quote()`` returns a normalized tick
    **dict** (``{"price": ...}``); legacy provider paths may still hand
    back attribute-style objects.  Support both shapes so market-data
    regressions can never crash order routing or position valuation.
    """
    if quote is None:
        return None
    if isinstance(quote, dict):
        candidates = (
            quote.get("price"),
            quote.get("last_price"),
            quote.get("ltp"),
            quote.get("close"),
        )
    else:
        candidates = tuple(
            getattr(quote, attr, None)
            for attr in ("price", "last_price", "ltp", "close")
        )
    for value in candidates:
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


class ManualOrderRequest(BaseModel):
    symbol: str = Field(..., description="NSE/Crypto ticker symbol e.g. NIFTY50, RELIANCE, BTCUSDT")
    side: Literal["BUY", "SELL"]
    quantity: int = Field(..., gt=0)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    price: Optional[float] = None
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    client_order_id: Optional[str] = Field(
        None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]{8,64}$",
        description=(
            "Optional caller-supplied idempotency key. When provided the order "
            "is durably claimed (PENDING) before any broker dispatch; retries "
            "with the same key replay the stored result instead of executing "
            "again. Unique per authenticated user."
        ),
    )


async def _resolve_idempotency_key(
    body_key: Optional[str],
    header_key: Optional[str],
) -> Optional[str]:
    """Normalize and validate an optional client idempotency key.

    Accepts either the JSON body ``client_order_id`` or the ``Idempotency-Key``
    request header; when both are supplied they must agree. A request with no
    key preserves the legacy non-idempotent execution contract.
    """
    body_key = (body_key or "").strip()
    header_key = (header_key or "").strip()
    if body_key and header_key and body_key != header_key:
        raise HTTPException(
            status_code=422,
            detail="Idempotency mismatch: body client_order_id and Idempotency-Key header differ",
        )
    key = body_key or header_key or None
    if key is not None and not re.fullmatch(r"^[A-Za-z0-9._-]{8,64}$", key):
        raise HTTPException(
            status_code=422,
            detail="Idempotency key must be 8-64 characters of [A-Za-z0-9._-]",
        )
    return key


async def _claim_or_replay_order(
    db: AsyncSession,
    *,
    user_id: str,
    client_order_id: str,
    symbol: str,
    side: str,
    quantity: int,
    order_type: str,
    price: float | None,
    mode: str,
    broker_account_id: str | None = None,
    strategy_id: str | None = None,
) -> tuple[str, OrderRecord]:
    """Claim an idempotency key durably or reap a previous result.

    The uniqueness invariant is enforced by the ``ux_orders_user_client_order_id``
    partial unique index (per authenticated user), so two concurrent identical
    requests can never both claim the same key.

    Returns ``(outcome, order)``:

    ``claimed`` — a durable PENDING ``OrderRecord`` was committed BEFORE any
    broker dispatch.  The caller performs the dispatch and finalizes the row as
    FILLED or REJECTED.

    ``replay`` — a previously FILLED order exists for this user + key.  The
    caller must return the stored result and perform NO new dispatch or side
    effect.

    ``in_progress`` — a PENDING claim already exists for this user + key; the
    caller must return a deterministic conflict (HTTP 409) — never a second
    dispatch.  This helper raises the 409 itself.
    """
    existing = (
        await db.execute(
            select(OrderRecord).where(
                OrderRecord.user_id == user_id,
                OrderRecord.client_order_id == client_order_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.status == "FILLED":
            return "replay", existing
        if existing.status == "PENDING":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "idempotency key already in progress",
                    "client_order_id": client_order_id,
                    "order_id": existing.broker_order_id or existing.id,
                    "status": "PENDING",
                },
            )
        # REJECTED / CANCELLED — the clearly-defined retry: re-claim the SAME
        # durable row (audit continuity) as PENDING and execute again.
        #
        # The re-claim must be a single conditional UPDATE (compare-and-swap),
        # NOT a read-mutate-commit: the partial unique index
        # ``ux_orders_user_client_order_id`` only guards INSERTs of NEW claims
        # and cannot stop two concurrent same-key retries from both flipping
        # this existing row to PENDING, both committing, and both dispatching a
        # duplicate real order.  By restricting the UPDATE to rows still in a
        # retryable state, exactly one retry wins; losers see PENDING (409) or
        # a completed FILLED (replay) and never dispatch again.
        retry_update = await db.execute(
            update(OrderRecord)
            .where(
                OrderRecord.user_id == user_id,
                OrderRecord.client_order_id == client_order_id,
                OrderRecord.status.not_in(["FILLED", "PENDING"]),
            )
            .values(
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
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
        if retry_update.rowcount == 1:
            await db.commit()
            await db.refresh(existing)
            return "claimed", existing

        # Lost the race — another retry already re-claimed the key (now PENDING)
        # or the request completed (now FILLED).  Re-read and follow the
        # standard semantics: replay a completed order, else a deterministic
        # in-progress conflict.  Never a second dispatch.
        await db.rollback()
        winner = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == user_id,
                    OrderRecord.client_order_id == client_order_id,
                )
            )
        ).scalar_one_or_none()
        if winner is not None and winner.status == "FILLED":
            return "replay", winner
        raise HTTPException(
            status_code=409,
            detail={
                "error": "idempotency key already in progress",
                "client_order_id": client_order_id,
                "order_id": (winner.broker_order_id or winner.id) if winner else None,
                "status": "PENDING",
            },
        )

    claim = OrderRecord(
        user_id=user_id,
        client_order_id=client_order_id,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        price=price,
        mode=mode,
        status="PENDING",
    )
    db.add(claim)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent identical request committed its claim first. Roll back
        # and report the winner deterministically — replay if already FILLED,
        # otherwise an in-progress conflict. Never a second dispatch.
        await db.rollback()
        winner = (
            await db.execute(
                select(OrderRecord).where(
                    OrderRecord.user_id == user_id,
                    OrderRecord.client_order_id == client_order_id,
                )
            )
        ).scalar_one_or_none()
        if winner is not None and winner.status == "FILLED":
            return "replay", winner
        raise HTTPException(
            status_code=409,
            detail={
                "error": "idempotency key already in progress",
                "client_order_id": client_order_id,
                "order_id": (winner.broker_order_id or winner.id) if winner else None,
                "status": "PENDING",
            },
        )
    await db.refresh(claim)
    return "claimed", claim


async def _manual_replay_response(db: AsyncSession, order: OrderRecord) -> dict:
    """Rebuild the persisted manual-order result for an idempotent replay.

    Only state stored on the durable order row (plus the linked trade's
    execution timestamp) is returned. No new dispatch, position, or side
    effect is ever created.
    """
    trade = (
        await db.execute(
            select(TradeRecord).where(
                TradeRecord.user_id == order.user_id,
                TradeRecord.order_id == order.broker_order_id,
            )
        )
    ).scalar_one_or_none()
    executed_at = (
        trade.executed_at.isoformat()
        if trade is not None and trade.executed_at is not None
        else order.created_at.isoformat()
    )
    return {
        "success": True,
        "order_id": order.broker_order_id or order.id,
        "symbol": order.symbol,
        "side": order.side,
        "quantity": order.quantity,
        "price": order.filled_price if order.filled_price is not None else order.price,
        "mode": order.mode,
        "status": "FILLED",
        "position_id": order.position_id,
        "copy_fanout": None,
        "executed_at": executed_at,
        "idempotent_replay": True,
    }


async def _dma_replay_response(db: AsyncSession, order: OrderRecord) -> dict:
    """Rebuild the persisted DMA result for an idempotent replay.

    Only fields stored on the durable order row are returned.  Transient
    first-execution analytics (product, margin_required, charges, latency)
    are NOT persisted and are therefore deliberately omitted; the returned
    payload is reconstruction, not a re-execution.
    """
    lot_size = get_lot_size(order.symbol)
    return {
        "success": True,
        "order_id": order.broker_order_id or order.id,
        "broker_order_id": order.broker_order_id or order.id,
        "symbol": order.symbol,
        "side": order.side,
        "lots": order.quantity // lot_size if lot_size and lot_size > 0 else order.quantity,
        "lot_size": lot_size,
        "quantity": order.quantity,
        "executed_price": order.filled_price if order.filled_price is not None else order.price,
        "position_id": order.position_id,
        "mode": order.mode,
        "status": "FILLED",
        "copy_fanout": None,
        "executed_at": order.created_at.isoformat(),
        "idempotent_replay": True,
    }


@router.get("/positions")
@router.get("/api/positions")
async def list_open_positions(
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_current_user),
):
    """Retrieve active open positions with live market valuation."""
    stmt = select(PositionRecord).where(PositionRecord.status == "OPEN").order_by(desc(PositionRecord.opened_at))
    if user:
        stmt = stmt.where(PositionRecord.user_id == user.id)

    res = await db.execute(stmt)
    positions = res.scalars().all()

    output = []
    for p in positions:
        quote = unified_market_manager.get_quote(p.symbol)
        inst = instrument_master.get_instrument(p.symbol)
        live_p = _quote_price(quote) or (inst.base_price if inst else p.entry_price)

        is_long = p.side in ("LONG", "BUY")
        delta = (live_p - p.entry_price) if is_long else (p.entry_price - live_p)
        unrealized_pnl = round(delta * p.quantity, 2)
        unrealized_pnl_pct = round((delta / p.entry_price) * 100, 2) if p.entry_price else 0.0

        output.append({
            "id": p.id,
            "symbol": p.symbol,
            "side": p.side,
            "quantity": p.quantity,
            "entry_price": p.entry_price,
            "current_price": live_p,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "mode": p.mode,
            "status": p.status,
            "opened_at": p.opened_at.isoformat() if p.opened_at else datetime.now(timezone.utc).isoformat(),
        })
    return output


@router.post("/positions/{position_id}/close")
async def close_position(
    position_id: str,
    db: AsyncSession = Depends(get_db),
    user: UserRecord = Depends(get_current_user),
):
    """Close an open position at the real live market price and book realized PnL.

    Object-level authorization: only the owner of the position may close it.
    Without this check any authenticated user could square off another user's
    LIVE position (dispatching real broker orders against a foreign account
    state) and book/erase their realized PnL — a cross-tenant trading-control
    vulnerability (IDOR).
    """
    pos = await db.get(PositionRecord, position_id)
    if not pos or pos.status != "OPEN":
        raise HTTPException(status_code=404, detail="Open position not found or already closed")

    if pos.user_id != user.id and getattr(user, "role", "").upper() not in ("ADMIN", "SUPERADMIN"):
        raise HTTPException(status_code=403, detail="Not authorized to close this position")

    quote = unified_market_manager.get_quote(pos.symbol)
    inst = instrument_master.get_instrument(pos.symbol)
    exit_price = _quote_price(quote) or (inst.base_price if inst else pos.entry_price)

    is_long = pos.side in ("LONG", "BUY")
    closing_side_for_broker = "SELL" if is_long else "BUY"

    # ── P1 FIX: Atomic CAS claim — sole gate preventing concurrent close race──
    # A conditional UPDATE that transitions OPEN→CLOSED atomically.  Exactly one
    # concurrent request wins (rowcount == 1); losers get 404 and must NEVER
    # dispatch a broker close order or book duplicate PnL/trade state.
    #
    # Failure safety (P1 durability fix): for LIVE positions the claim is
    # committed BEFORE the broker dispatch runs (see below), so a crash between
    # dispatch and PnL commit can never leave DB=OPEN with the exchange already
    # CLOSED (a retry would double-close real inventory).  If the broker
    # dispatch itself fails, the committed CAS is explicitly reverted to OPEN.
    result = await db.execute(
        update(PositionRecord)
        .where(PositionRecord.id == position_id, PositionRecord.status == "OPEN")
        .values(status="CLOSED", closed_at=datetime.now(timezone.utc))
    )
    if result.rowcount != 1:
        raise HTTPException(
            status_code=404,
            detail="Open position not found or already closed by concurrent request",
        )
    # Sync ORM object to match the atomic CAS state
    pos.status = "CLOSED"
    pos.closed_at = datetime.now(timezone.utc)

    # ── EXEC-02 Fix: Dispatch real closing order to broker before DB update ──
    if pos.mode == "LIVE":
        # A LIVE position MUST resolve its routing broker account before it may
        # be marked CLOSED.  If the account was deleted (dangling FK in SQLite)
        # or FK `SET NULL`'d (PostgreSQL) while the position was still OPEN, we
        # have real exchange exposure with no way to square it off.  Failing to
        # close the real position while booking PnL would be a fabricated LIVE
        # close — a financial-correctness defect.  Fail closed instead so the
        # operator is alerted rather than silently reporting a fake exit.
        broker_acc = None
        if pos.broker_account_id:
            broker_acc_stmt = select(BrokerAccountRecord).where(
                BrokerAccountRecord.id == pos.broker_account_id
            )
            broker_acc_res = await db.execute(broker_acc_stmt)
            broker_acc = broker_acc_res.scalar_one_or_none()

        if not broker_acc:
            logger.error(
                "[LIVE] Cannot close position %s: broker account %r is "
                "missing/disconnected. Refusing to fabricate a close.",
                pos.id,
                pos.broker_account_id,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Cannot close LIVE position: its broker account is no longer "
                    "connected. Reconnect the broker account before closing so the "
                    "real position on the exchange can be squared off."
                ),
            )

        # ── PHASE-3 LIVE/Paper separation guard ──────────────────────
        # A LIVE close order must also be gated by BROKER_MODE=live.
        try:
            assert_live_dispatch_allowed()
        except BrokerModeBlockedError as guard_exc:
            raise HTTPException(
                status_code=403,
                detail=str(guard_exc),
            ) from guard_exc

        # â”€â”€ P1 FIX: Durability â€” commit the CAS claim BEFORE any broker dispatch â”€â”€
        # Every pre-flight check (broker resolution, live-dispatch guard) has
        # passed, so the position may now be durably claimed CLOSED.  If the
        # process crashes AFTER this commit but BEFORE the dispatch, the local
        # ledger shows CLOSED while the exchange is still OPEN â€” a "phantom
        # close" that reconciliation / manual intervention can recover and that
        # can NEVER trigger a second broker action.  The pre-fix order
        # (dispatch-then-commit) left DB=OPEN / exchange=CLOSED on a crash, so
        # a retry would re-dispatch a close order against real inventory â€”
        # a double close.  Committing first trades that double-close risk for
        # a recoverable divergence.  If the dispatch below fails, the committed
        # CAS is explicitly reverted to OPEN so the close stays retryable.
        await db.commit()
        await db.refresh(pos)

        try:
            broker_client = get_broker_adapter(broker_acc)
            from app.schemas.trading import OrderRequest, Side
            close_order_req = OrderRequest(
                symbol=pos.symbol,
                side=Side.SELL if is_long else Side.BUY,
                quantity=pos.quantity,
                order_type="MARKET",
            )
            broker_resp = await broker_client.place_order(close_order_req)
            filled_price = broker_resp.get("filled_price") or broker_resp.get("price")
            if filled_price:
                exit_price = float(filled_price)
            logger.info(
                "[LIVE] Broker close order dispatched for position %s: %s",
                pos.id, broker_resp
            )
        except Exception as broker_exc:
            logger.error(
                "[LIVE] Broker close order failed for position %s: %s",
                pos.id, broker_exc
            )
            # P1 FIX: The CAS claim was already COMMITTED before dispatch.
            # Explicitly revert it to OPEN so the position stays retryable and no
            # fabricated CLOSED / PnL is left behind.
            await db.execute(
                update(PositionRecord)
                .where(PositionRecord.id == position_id)
                .values(status="OPEN", closed_at=None)
            )
            pos.status = "OPEN"
            pos.closed_at = None
            await db.commit()
            raise HTTPException(
                status_code=502,
                detail=f"Broker failed to close position on exchange: {broker_exc}"
            )

    delta = (exit_price - pos.entry_price) if is_long else (pos.entry_price - exit_price)
    realized_pnl = round(delta * pos.quantity, 2)
    pnl_pct = round((delta / pos.entry_price) * 100, 2) if pos.entry_price else 0.0

    # pos.status and pos.closed_at already set by the CAS claim above
    pos.current_price = exit_price
    pos.realized_pnl = realized_pnl
    pos.unrealized_pnl = 0.0

    # Record offsetting closing trade
    closing_side = closing_side_for_broker
    trade = TradeRecord(
        id=str(uuid.uuid4()),
        order_id=f"EXIT_{int(datetime.now(timezone.utc).timestamp())}",
        strategy_name="Manual Position Exit",
        symbol=pos.symbol,
        side=closing_side,
        quantity=pos.quantity,
        price=exit_price,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        pnl=realized_pnl,
        pnl_pct=pnl_pct,
        exit_reason="MANUAL_CLOSE",
        mode=pos.mode,
        user_id=pos.user_id,
    )
    db.add(trade)

    owner_balance: Optional[float] = None
    if pos.mode == "PAPER":
        # ── P1-1 PAPER accounting hardening ──────────────────────────────
        # The realized P&L of a PAPER close ALWAYS belongs to the account
        # that OWNS the position (pos.user_id) — never the authenticated
        # caller.  An admin/operator closing another user's position must not
        # credit the operator's own balance (cross-user accounting
        # corruption).  Missing owner => no credit (fail-safe).  The credit
        # commits atomically with the CAS claim above, so a replay/concurrent
        # close can never double-book it.
        from app.engine.paper_account import credit_paper_pnl

        owner_balance = await credit_paper_pnl(db, pos.user_id, realized_pnl)

    await db.commit()

    # Trigger Copy Trading Fan-out for all active followers of the trade
    # owner.  P1-2: the fan-out identity must be the POSITION OWNER
    # (pos.user_id) — never the authenticated caller.  When an
    # ADMIN/SUPERADMIN closes another trader's position, keying the fan-out to
    # the caller's id would (a) skip the owner's followers entirely (their
    # mirrored OPEN position is left stale, diverging from the owner's book)
    # and (b) mirror the close into the caller's OWN copy groups, phantom-
    # closing unrelated follower positions.  This mirrors the P1-1 accounting
    # rule: the same economic event resolves its owner once, from the position
    # row.  For an owner closing their own position user.id == pos.user_id, so
    # the common path is behavior-preserving.
    try:
        from app.engine.copy_trading import copy_trading_engine
        master_uid = pos.user_id
        if master_uid:
            asyncio.create_task(
                copy_trading_engine.mirror_close_position(
                    symbol=pos.symbol,
                    master_user_id=master_uid,
                    exit_price=exit_price,
                )
            )
    except Exception as exc:
        logger.warning("[CopyTrading] Notice on follower position exit fan-out: %s", exc)

    return {
        "success": True,
        "position_id": pos.id,
        "symbol": pos.symbol,
        "quantity": pos.quantity,
        "entry_price": pos.entry_price,
        "exit_price": exit_price,
        "realized_pnl": realized_pnl,
        "pnl_pct": pnl_pct,
        "status": "CLOSED",
        "paper_balance": owner_balance if owner_balance is not None else (getattr(user, "paper_balance", 1000000.0) if user else 1000000.0),
    }


@router.post("/order")
@router.post("/place")
async def place_manual_order(
    req: ManualOrderRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """Place manual DMA order for PAPER or LIVE execution with real fill price and open position tracking.

    Idempotency (P0): when the caller supplies ``client_order_id`` (body) or an
    ``Idempotency-Key`` request header, the order is durably claimed as PENDING
    *before* any broker dispatch, and a retry with the same key replays the
    stored result instead of executing a second time. Requests without a key
    keep the legacy (non-idempotent) contract exactly.
    """
    from app.engine.subscription import subscription_engine
    await subscription_engine.verify_feature_access(db, user.id, "trade_execution")

    clean_sym = req.symbol.upper().strip()

    # 1. Resolve real live market execution price from market provider / master
    quote = unified_market_manager.get_quote(clean_sym)
    inst = instrument_master.get_instrument(clean_sym)
    live_p = _quote_price(quote) or (inst.base_price if inst else (req.price or 1000.0))
    executed_price = round(req.price if req.order_type == "LIMIT" and req.price else live_p, 2)

    # 2. If LIVE mode, ensure a real connected broker exists, and enforce the
    #    BROKER_MODE=live guard BEFORE any durable idempotency claim — a blocked
    #    deployment must never leave a PENDING claim behind that would brick
    #    retries for the key.
    broker_account_id = None
    if req.mode == "LIVE":
        broker_stmt = select(BrokerAccountRecord).where(
            BrokerAccountRecord.user_id == user.id,
            BrokerAccountRecord.status == "CONNECTED",
            BrokerAccountRecord.is_active.is_(True),
        )
        broker_res = await db.execute(broker_stmt)
        broker_acc = broker_res.scalars().first()
        if not broker_acc:
            raise HTTPException(
                status_code=400,
                detail="No active connected broker account found. Please link your broker before switching to Live Execution.",
            )
        broker_account_id = broker_acc.id

        # ── PHASE-3 LIVE/Paper separation guard ────────────────────────────
        # Even with a connected broker account, a LIVE order MUST NOT reach a
        # real broker unless the deployment is explicitly BROKER_MODE=live.
        try:
            assert_live_dispatch_allowed()
        except BrokerModeBlockedError as guard_exc:
            raise HTTPException(
                status_code=403,
                detail=str(guard_exc),
            ) from guard_exc

    # 2b. Idempotency claim (keyed requests only) — the durable PENDING order
    #     row is committed BEFORE the broker dispatch below.
    order: Optional[OrderRecord] = None
    replay_order: Optional[OrderRecord] = None
    retry_key = await _resolve_idempotency_key(req.client_order_id, idempotency_key)
    if retry_key:
        outcome, order = await _claim_or_replay_order(
            db,
            user_id=user.id,
            client_order_id=retry_key,
            symbol=clean_sym,
            side=req.side,
            quantity=req.quantity,
            order_type=req.order_type,
            price=executed_price,
            mode=req.mode,
            broker_account_id=broker_account_id,
        )
        if outcome == "replay":
            replay_order = order
        # outcome == "in_progress" → _claim_or_replay_order raised HTTP 409.

    # 2c. LIVE broker dispatch — runs only for the first claim of a key, or
    #     every legacy unkeyed request. Never for a replay of a completed one.
    if req.mode == "LIVE" and replay_order is None:
        # ── EXEC-01a Fix: Dispatch real entry order to broker ──
        try:
            broker_client = get_broker_adapter(broker_acc)
            from app.schemas.trading import OrderRequest, Side
            live_order_req = OrderRequest(
                symbol=clean_sym,
                side=Side.BUY if req.side == "BUY" else Side.SELL,
                quantity=req.quantity,
                order_type=req.order_type,
                price=executed_price if req.order_type == "LIMIT" else None,
            )
            broker_resp = await broker_client.place_order(live_order_req)
            # Crash-window hardening (P1): durably persist the broker's returned
            # order reference on the PENDING claim IMMEDIATELY after acceptance,
            # in its own commit. If the process dies between broker acceptance
            # and the finalize commit, the row still carries the reference so the
            # reconciliation worker can read the broker's status back.
            broker_order_ref = broker_resp.get("order_id") or broker_resp.get("broker_order_id")
            if order is not None and order.status == "PENDING" and broker_order_ref:
                order.broker_order_id = str(broker_order_ref)
                await db.commit()
            filled_price = broker_resp.get("filled_price") or broker_resp.get("price")
            if filled_price:
                executed_price = round(float(filled_price), 2)
            logger.info("[LIVE] Manual order dispatched to broker: %s", broker_resp)
        except Exception as broker_exc:
            logger.error("[LIVE] Broker order placement failed: %s", broker_exc)
            if order is not None and order.status == "PENDING":
                # Keyed claim: persist a durable REJECTED — never a ghost and
                # never a fabricated FILLED. The same key can then be retried.
                order.status = "REJECTED"
                order.error_message = f"Broker rejected order: {broker_exc}"
                await db.commit()
            raise HTTPException(
                status_code=502,
                detail=f"Broker rejected order: {broker_exc}"
            )

    if replay_order is not None:
        return await _manual_replay_response(db, replay_order)

    # 3. Create persistent OrderRecord
    order_id = f"ORD_{int(datetime.now(timezone.utc).timestamp())}_{str(uuid.uuid4())[:8]}"
    if order is None:
        # Legacy unkeyed request — unchanged: the order row is born FILLED.
        order = OrderRecord(
            id=str(uuid.uuid4()),
            user_id=user.id,
            broker_account_id=broker_account_id,
            broker_order_id=order_id,
            symbol=clean_sym,
            side=req.side,
            quantity=req.quantity,
            order_type=req.order_type,
            price=executed_price,
            filled_price=executed_price,
            filled_quantity=req.quantity,
            status="FILLED",
            mode=req.mode,
        )
        db.add(order)
    else:
        # Keyed request — finalize the durable PENDING claim with the result.
        if order.broker_order_id is None:
            order.broker_order_id = order_id
        order.price = executed_price
        order.filled_price = executed_price
        order.filled_quantity = req.quantity
        order.status = "FILLED"

    # 4. Create persistent TradeRecord
    trade = TradeRecord(
        id=str(uuid.uuid4()),
        order_id=order_id,
        strategy_name="DMA Fast Order",
        symbol=clean_sym,
        side=req.side,
        quantity=req.quantity,
        price=executed_price,
        entry_price=executed_price,
        pnl=0.0,
        mode=req.mode,
        user_id=user.id,
    )
    db.add(trade)

    # 5. Create new Open PositionRecord
    pos_side = "LONG" if req.side == "BUY" else "SHORT"
    position = PositionRecord(
        id=str(uuid.uuid4()),
        user_id=user.id,
        broker_account_id=broker_account_id,
        symbol=clean_sym,
        side=pos_side,
        quantity=req.quantity,
        entry_price=executed_price,
        current_price=executed_price,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        mode=req.mode,
        status="OPEN",
        opened_at=datetime.now(timezone.utc),
    )
    db.add(position)
    order.position_id = position.id

    await db.commit()
    await db.refresh(trade)
    await db.refresh(position)

    from app.engine.alerts import notify_trade_fill
    await notify_trade_fill(
        user.id,
        symbol=trade.symbol,
        side=trade.side,
        quantity=trade.quantity,
        price=trade.price,
        mode=trade.mode,
    )

    from app.core.audit import log_audit_event
    await log_audit_event(
        db=db,
        action="MANUAL_ORDER_PLACED",
        resource_type="ORDER",
        user_id=user.id,
        status="EXECUTED",
        details={
            "symbol": clean_sym,
            "side": req.side,
            "quantity": req.quantity,
            "price": executed_price,
            "mode": req.mode,
            "position_id": position.id,
        },
    )

    # 6. Trigger Real-time Copy Trading Fan-out across all active follower accounts
    fanout_result = None
    try:
        from app.engine.copy_trading import copy_trading_engine
        fanout_result = await copy_trading_engine.mirror_trade(
            master_order=order,
            master_user_id=user.id,
        )
    except Exception as exc:
        logger.warning("[CopyTrading] Fan-out trigger exception: %s", exc)

    return {
        "success": True,
        "order_id": order_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "price": trade.price,
        "mode": trade.mode,
        "status": "FILLED",
        "position_id": position.id,
        "copy_fanout": fanout_result,
        "executed_at": trade.executed_at.isoformat() if trade.executed_at else datetime.now(timezone.utc).isoformat(),
    }
from time import perf_counter

from fastapi import HTTPException

from app.api.dma_engine import (
    DMAOrderRequest,
    MARGIN_MULTIPLIERS,
    classify_asset,
    compute_margin_required,
    compute_statutory_charges,
    get_lot_size,
)


class RiskTargetUpdate(BaseModel):
    stop_loss_price: Optional[float] = Field(None, gt=0)
    take_profit_price: Optional[float] = Field(None, gt=0)


dma_router = APIRouter(prefix="/api/v1/orders", tags=["dma-execution"])


@dma_router.post("/execute-dma")
async def execute_dma_order(
    req: DMAOrderRequest,
    db: AsyncSession = Depends(get_db),
    user: UserRecord = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """Sub-millisecond DMA execution: quote snap, lot auto-correction, margin &
    statutory charge engine, latency-instrumented dispatch, full persistence,
    SL/TP registration, notifications, audit, copy-trading fan-out.

    Idempotency (P0): when the caller supplies ``client_order_id`` (body) or an
    ``Idempotency-Key`` request header, the order is durably claimed as PENDING
    *before* any broker dispatch, and a retry with the same key replays the
    stored result instead of executing a second time. Requests without a key
    keep the legacy (non-idempotent) contract exactly.
    """
    t_start = perf_counter()

    clean_sym = req.symbol.upper().strip()
    quote = unified_market_manager.get_quote(clean_sym)
    executed_price = _quote_price(quote)
    if executed_price is None:
        inst = instrument_master.get_instrument(clean_sym)
        executed_price = float(inst.base_price) if inst else None
    if executed_price is None:
        raise HTTPException(status_code=409, detail=f"No live quote available for {clean_sym}")

    if req.order_type == "LIMIT":
        if not req.limit_price:
            raise HTTPException(status_code=422, detail="LIMIT orders require limit_price")
        executed_price = float(req.limit_price)

    # 1. Dynamic lot-size auto-correction (NIFTY 65 / BANKNIFTY 30 / SENSEX 20)
    lot_size = get_lot_size(clean_sym)
    lots = max(1, int(req.lots))
    quantity = lots * lot_size

    # 2. Pre-trade analytics
    margin_required, asset_class = compute_margin_required(clean_sym, req.product, quantity, executed_price)
    charges = compute_statutory_charges(clean_sym, req.side, req.product, quantity, executed_price)

    # 3. Broker account resolution for LIVE routing, and the BROKER_MODE=live
    #    guard — both fire BEFORE any durable idempotency claim (a blocked
    #    deployment must never leave a PENDING claim behind).
    broker_account_id = None
    if req.mode == "LIVE":
        stmt_acc = select(BrokerAccountRecord).where(
            BrokerAccountRecord.user_id == user.id,
            BrokerAccountRecord.status == "CONNECTED",
            BrokerAccountRecord.is_active.is_(True),
        )
        if req.broker_account_id:
            stmt_acc = stmt_acc.where(BrokerAccountRecord.id == req.broker_account_id)
        acc_row = (await db.execute(stmt_acc)).scalars().first()
        if not acc_row:
            raise HTTPException(status_code=403, detail="LIVE DMA requires a CONNECTED broker account.")
        broker_account_id = acc_row.id

        # ── PHASE-3 LIVE/Paper separation guard ──────────────────────────
        # Even with a connected broker, a LIVE DMA order must be gated by
        # BROKER_MODE=live.
        try:
            assert_live_dispatch_allowed()
        except BrokerModeBlockedError as guard_exc:
            raise HTTPException(
                status_code=403,
                detail=str(guard_exc),
            ) from guard_exc

    # 3b. Idempotency claim (keyed requests only) — the durable PENDING order
    #     row is committed BEFORE the broker dispatch below.
    order: Optional[OrderRecord] = None
    replay_order: Optional[OrderRecord] = None
    retry_key = await _resolve_idempotency_key(req.client_order_id, idempotency_key)
    if retry_key:
        outcome, order = await _claim_or_replay_order(
            db,
            user_id=user.id,
            client_order_id=retry_key,
            symbol=clean_sym,
            side=req.side,
            quantity=quantity,
            order_type=req.order_type,
            price=executed_price,
            mode=req.mode,
            broker_account_id=broker_account_id,
            strategy_id=req.strategy_id,
        )
        if outcome == "replay":
            replay_order = order
        # outcome == "in_progress" → _claim_or_replay_order raised HTTP 409.

    # 4. Latency-instrumented dispatch (real broker for LIVE). Runs only for
    #    the first claim of a key, or every legacy unkeyed request.
    t_dispatch = perf_counter()
    broker_order_ref = None
    if req.mode == "LIVE" and acc_row and replay_order is None:
        # ── EXEC-01b Fix: Dispatch real DMA order to broker ──
        try:
            broker_client = get_broker_adapter(acc_row)
            from app.schemas.trading import OrderRequest, Side
            dma_order_req = OrderRequest(
                symbol=clean_sym,
                side=Side.BUY if req.side == "BUY" else Side.SELL,
                quantity=quantity,
                order_type=req.order_type,
                price=executed_price if req.order_type == "LIMIT" else None,
            )
            broker_resp = await broker_client.place_order(dma_order_req)
            broker_order_ref = broker_resp.get("order_id") or broker_resp.get("broker_order_id")
            # Crash-window hardening (P1): durably persist the broker's returned
            # order reference on the PENDING claim IMMEDIATELY after acceptance,
            # in its own commit — the reference must survive a process crash
            # before the finalize commit so reconciliation can read it back.
            if order is not None and order.status == "PENDING" and broker_order_ref:
                order.broker_order_id = str(broker_order_ref)
                await db.commit()
            filled_price = broker_resp.get("filled_price") or broker_resp.get("price")
            if filled_price:
                executed_price = round(float(filled_price), 2)
            logger.info("[LIVE] DMA order dispatched to broker: %s", broker_resp)
        except Exception as broker_exc:
            logger.error("[LIVE] DMA broker dispatch failed: %s", broker_exc)
            if order is not None and order.status == "PENDING":
                # Keyed claim: persist a durable REJECTED — never a ghost and
                # never a fabricated FILLED. The same key can then be retried.
                order.status = "REJECTED"
                order.error_message = f"Broker rejected DMA order: {broker_exc}"
                await db.commit()
            raise HTTPException(
                status_code=502,
                detail=f"Broker rejected DMA order: {broker_exc}"
            )
    dispatch_latency_ms = round((perf_counter() - t_dispatch) * 1000, 3)
    total_latency_ms = round((perf_counter() - t_start) * 1000, 3)

    if replay_order is not None:
        return await _dma_replay_response(db, replay_order)

    # 5. Persistence
    order_id = f"DMA_{int(datetime.now(timezone.utc).timestamp())}_{str(uuid.uuid4())[:8]}"
    long_side = req.side == "BUY"
    sl_price = (
        round(executed_price * (1 - req.stop_loss_pct / 100), 2)
        if (req.stop_loss_pct and long_side)
        else (round(executed_price * (1 + req.stop_loss_pct / 100), 2) if req.stop_loss_pct else None)
    )
    tp_price = (
        round(executed_price * (1 + req.take_profit_pct / 100), 2)
        if (req.take_profit_pct and long_side)
        else (round(executed_price * (1 - req.take_profit_pct / 100), 2) if req.take_profit_pct else None)
    )

    if order is None:
        # Legacy unkeyed request — unchanged: the order row is born FILLED.
        order = OrderRecord(
            id=str(uuid.uuid4()), user_id=user.id, strategy_id=req.strategy_id,
            broker_account_id=broker_account_id, broker_order_id=broker_order_ref or order_id,
            symbol=clean_sym, side=req.side, quantity=quantity, order_type=req.order_type,
            price=executed_price, filled_price=executed_price, filled_quantity=quantity,
            status="FILLED", mode=req.mode,
        )
        db.add(order)
    else:
        # Keyed request — finalize the durable PENDING claim with the result.
        if order.broker_order_id is None:
            order.broker_order_id = broker_order_ref or order_id
        order.price = executed_price
        order.filled_price = executed_price
        order.filled_quantity = quantity
        order.status = "FILLED"
    trade = TradeRecord(
        id=str(uuid.uuid4()), order_id=order_id, strategy_id=req.strategy_id,
        strategy_name="Institutional DMA", symbol=clean_sym, side=req.side,
        quantity=quantity, price=executed_price, entry_price=executed_price,
        pnl=0.0, mode=req.mode, user_id=user.id,
    )
    db.add(trade)
    position = PositionRecord(
        id=str(uuid.uuid4()), user_id=user.id, broker_account_id=broker_account_id,
        symbol=clean_sym, side="LONG" if long_side else "SHORT", quantity=quantity,
        entry_price=executed_price, current_price=executed_price,
        unrealized_pnl=0.0, realized_pnl=0.0,
        stop_loss_price=sl_price, take_profit_price=tp_price,
        mode=req.mode, status="OPEN", opened_at=datetime.now(timezone.utc),
    )
    db.add(position)
    order.position_id = position.id
    await db.commit()
    await db.refresh(trade)
    await db.refresh(position)

    # 6. Notifications / audit / fan-out (best-effort)
    try:
        from app.engine.alerts import notify_trade_fill
        await notify_trade_fill(user.id, symbol=clean_sym, side=req.side, quantity=quantity, price=executed_price, mode=req.mode)
    except Exception as exc:
        logger.debug("notify skip: %s", exc)
    try:
        await log_audit_event(db=db, action="DMA_ORDER_EXECUTED", resource_type="ORDER",
                              user_id=user.id, resource_id=order.id, status="EXECUTED",
                              details={"symbol": clean_sym, "lots": lots, "lot_size": lot_size,
                                       "quantity": quantity, "price": executed_price,
                                       "margin": margin_required, "charges": charges["total"],
                                       "latency_ms": total_latency_ms})
    except Exception as exc:
        logger.debug("audit skip: %s", exc)
    fanout_result = None
    try:
        from app.engine.copy_trading import copy_trading_engine
        fanout_result = await copy_trading_engine.mirror_trade(master_order=order, master_user_id=user.id)
    except Exception as exc:
        logger.warning("[DMA] fan-out exception: %s", exc)

    return {
        "success": True,
        "order_id": order_id,
        "broker_order_id": broker_order_ref or order_id,
        "symbol": clean_sym,
        "side": req.side,
        "product": req.product,
        "lots": lots,
        "lot_size": lot_size,
        "quantity": quantity,
        "executed_price": executed_price,
        "margin_required": margin_required,
        "margin_multiplier": MARGIN_MULTIPLIERS.get(asset_class, 1.0),
        "asset_class": asset_class,
        "charges": charges,
        "stop_loss_price": sl_price,
        "take_profit_price": tp_price,
        "position_id": position.id,
        "mode": req.mode,
        "status": "FILLED",
        "latency_ms": total_latency_ms,
        "dispatch_latency_ms": dispatch_latency_ms,
        "within_50ms_slo": total_latency_ms <= 50,
        "copy_fanout": fanout_result,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }


@dma_router.patch("/positions/{position_id}/risk-targets")
async def modify_position_risk_targets(
    position_id: str,
    req: RiskTargetUpdate,
    db: AsyncSession = Depends(get_db),
    user: UserRecord = Depends(get_current_user),
):
    """Update SL/TP levels on an open position (chart drag-to-modify backend)."""
    stmt = select(PositionRecord).where(
        PositionRecord.id == position_id,
        PositionRecord.user_id == user.id,
        PositionRecord.status == "OPEN",
    )
    pos = (await db.execute(stmt)).scalar_one_or_none()
    if not pos:
        raise HTTPException(status_code=404, detail="Open position not found")

    is_long = pos.side in ("LONG", "BUY")
    if req.stop_loss_price is not None:
        if is_long and req.stop_loss_price >= pos.entry_price:
            raise HTTPException(status_code=422, detail="LONG stop-loss must sit below entry price")
        if not is_long and req.stop_loss_price <= pos.entry_price:
            raise HTTPException(status_code=422, detail="SHORT stop-loss must sit above entry price")
        pos.stop_loss_price = round(req.stop_loss_price, 2)

    if req.take_profit_price is not None:
        if is_long and req.take_profit_price <= pos.entry_price:
            raise HTTPException(status_code=422, detail="LONG take-profit must sit above entry price")
        if not is_long and req.take_profit_price >= pos.entry_price:
            raise HTTPException(status_code=422, detail="SHORT take-profit must sit below entry price")
        pos.take_profit_price = round(req.take_profit_price, 2)

    await db.commit()
    from app.market_data.manager import ws_manager
    try:
        await ws_manager.broadcast(f"position:{position_id}", {
            "event": "RISK_TARGET_UPDATED",
            "position_id": position_id,
            "stop_loss_price": pos.stop_loss_price,
            "take_profit_price": pos.take_profit_price,
        })
    except Exception:
        pass

    return {
        "success": True,
        "position_id": position_id,
        "stop_loss_price": pos.stop_loss_price,
        "take_profit_price": pos.take_profit_price,
    }
