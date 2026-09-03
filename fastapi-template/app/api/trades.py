"""Trade history and statistics endpoints."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_db
from app.models.trading import TradeRecord
from app.schemas.trading import TradeRead, TradeStats

logger = get_logger("api.trades")

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("", response_model=list[TradeRead])
async def list_trades(
    symbol: str | None = Query(None, description="Filter by symbol"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Return paginated trade history, newest first."""
    stmt = select(TradeRecord).order_by(desc(TradeRecord.executed_at))

    if symbol:
        stmt = stmt.where(TradeRecord.symbol == symbol.upper())

    stmt = stmt.offset(offset).limit(limit)
    result = await db.execute(stmt)

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
        for r in result.scalars().all()
    ]


@router.get("/stats", response_model=TradeStats)
async def trade_stats(db: AsyncSession = Depends(get_db)):
    """Return aggregated trade statistics."""
    total = await db.scalar(select(func.count(TradeRecord.id)))
    total = total or 0

    if total == 0:
        return TradeStats()

    result = await db.execute(select(TradeRecord))
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
from fastapi import HTTPException
import asyncio
from app.api.auth import get_current_user
from app.brokers import BrokerModeBlockedError, get_broker_adapter, assert_live_dispatch_allowed
from app.models.user import UserRecord
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
    user: Optional[UserRecord] = Depends(get_current_user),
):
    """Close an open position at the real live market price and book realized PnL."""
    pos = await db.get(PositionRecord, position_id)
    if not pos or pos.status != "OPEN":
        raise HTTPException(status_code=404, detail="Open position not found or already closed")

    quote = unified_market_manager.get_quote(pos.symbol)
    inst = instrument_master.get_instrument(pos.symbol)
    exit_price = _quote_price(quote) or (inst.base_price if inst else pos.entry_price)

    is_long = pos.side in ("LONG", "BUY")
    closing_side_for_broker = "SELL" if is_long else "BUY"

    # ── EXEC-02 Fix: Dispatch real closing order to broker before DB update ──
    if pos.mode == "LIVE" and pos.broker_account_id:
        broker_acc_stmt = select(BrokerAccountRecord).where(
            BrokerAccountRecord.id == pos.broker_account_id
        )
        broker_acc_res = await db.execute(broker_acc_stmt)
        broker_acc = broker_acc_res.scalar_one_or_none()
        if broker_acc:
            # ── PHASE-3 LIVE/Paper separation guard ──────────────────────
            # A LIVE close order must also be gated by BROKER_MODE=live.
            try:
                assert_live_dispatch_allowed()
            except BrokerModeBlockedError as guard_exc:
                raise HTTPException(
                    status_code=403,
                    detail=str(guard_exc),
                ) from guard_exc
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
                raise HTTPException(
                    status_code=502,
                    detail=f"Broker failed to close position on exchange: {broker_exc}"
                )

    delta = (exit_price - pos.entry_price) if is_long else (pos.entry_price - exit_price)
    realized_pnl = round(delta * pos.quantity, 2)
    pnl_pct = round((delta / pos.entry_price) * 100, 2) if pos.entry_price else 0.0

    pos.status = "CLOSED"
    pos.closed_at = datetime.now(timezone.utc)
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

    if pos.mode == "PAPER" and user:
        current_bal = getattr(user, "paper_balance", 1000000.0)
        user.paper_balance = round(current_bal + realized_pnl, 2)
        db.add(user)

    await db.commit()

    # Trigger Copy Trading Fan-out for all active followers of master trader
    try:
        from app.engine.copy_trading import copy_trading_engine
        master_uid = user.id if user else pos.user_id
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
        "paper_balance": getattr(user, "paper_balance", 1000000.0) if user else 1000000.0,
    }


@router.post("/order")
@router.post("/place")
async def place_manual_order(
    req: ManualOrderRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Place manual DMA order for PAPER or LIVE execution with real fill price and open position tracking."""
    from app.engine.subscription import subscription_engine
    await subscription_engine.verify_feature_access(db, user.id, "trade_execution")

    clean_sym = req.symbol.upper().strip()

    # 1. Resolve real live market execution price from market provider / master
    quote = unified_market_manager.get_quote(clean_sym)
    inst = instrument_master.get_instrument(clean_sym)
    live_p = _quote_price(quote) or (inst.base_price if inst else (req.price or 1000.0))
    executed_price = round(req.price if req.order_type == "LIMIT" and req.price else live_p, 2)

    # 2. If LIVE mode, ensure a real connected broker exists
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
            filled_price = broker_resp.get("filled_price") or broker_resp.get("price")
            if filled_price:
                executed_price = round(float(filled_price), 2)
            logger.info("[LIVE] Manual order dispatched to broker: %s", broker_resp)
        except Exception as broker_exc:
            logger.error("[LIVE] Broker order placement failed: %s", broker_exc)
            raise HTTPException(
                status_code=502,
                detail=f"Broker rejected order: {broker_exc}"
            )

    # 3. Create persistent OrderRecord
    order_id = f"ORD_{int(datetime.now(timezone.utc).timestamp())}_{str(uuid.uuid4())[:8]}"
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
):
    """Sub-millisecond DMA execution: quote snap, lot auto-correction, margin &
    statutory charge engine, latency-instrumented dispatch, full persistence,
    SL/TP registration, notifications, audit, copy-trading fan-out."""
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

    # 3. Broker account resolution for LIVE routing
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

    # 4. Latency-instrumented dispatch (real broker for LIVE)
    t_dispatch = perf_counter()
    broker_order_ref = None
    if req.mode == "LIVE" and acc_row:
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
            filled_price = broker_resp.get("filled_price") or broker_resp.get("price")
            if filled_price:
                executed_price = round(float(filled_price), 2)
            logger.info("[LIVE] DMA order dispatched to broker: %s", broker_resp)
        except Exception as broker_exc:
            logger.error("[LIVE] DMA broker dispatch failed: %s", broker_exc)
            raise HTTPException(
                status_code=502,
                detail=f"Broker rejected DMA order: {broker_exc}"
            )
    dispatch_latency_ms = round((perf_counter() - t_dispatch) * 1000, 3)
    total_latency_ms = round((perf_counter() - t_start) * 1000, 3)

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

    order = OrderRecord(
        id=str(uuid.uuid4()), user_id=user.id, strategy_id=req.strategy_id,
        broker_account_id=broker_account_id, broker_order_id=broker_order_ref or order_id,
        symbol=clean_sym, side=req.side, quantity=quantity, order_type=req.order_type,
        price=executed_price, filled_price=executed_price, filled_quantity=quantity,
        status="FILLED", mode=req.mode,
    )
    db.add(order)
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
