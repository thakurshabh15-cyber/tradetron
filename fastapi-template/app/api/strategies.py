"""Strategy CRUD and Marketplace endpoints."""

import json
from typing import Optional, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import get_db
from app.models.marketplace import MarketplaceStrategyRecord, StrategyDeploymentRecord
from app.models.trading import StrategyRecord
from app.schemas.trading import StrategyCreate, StrategyRead, StrategyUpdate

logger = get_logger("api.strategies")

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


class DeployStrategyRequest(BaseModel):
    execution_mode: str = Field("PAPER", description="PAPER or LIVE")
    broker_name: str = Field("Simulated", description="Simulated, Zerodha, Upstox, Angel One, or Binance")
    broker_account_id: Optional[str] = None
    multiplier: float = Field(1.0, ge=0.1, le=10.0)
    capital_allocated: float = Field(10000.0, ge=100.0)


class PublishMarketplaceRequest(BaseModel):
    strategy_id: str
    creator_name: str = "Quant Trader"
    category: str = "Trend Following"
    pricing_type: str = "FREE"
    price: float = 0.0
    description: str = ""


class KillSwitchRequest(BaseModel):
    action: str = Field("PAUSE_ALL", description="PAUSE_ALL, RESUME_ALL, or SQUARE_OFF_ALL")
    reason: str = Field("Manual emergency stop triggered by operator", description="Reason for kill switch")


DeployStrategyRequest.model_rebuild()
PublishMarketplaceRequest.model_rebuild()
KillSwitchRequest.model_rebuild()


def _row_to_read(row: StrategyRecord) -> dict:
    """Convert a DB row to a StrategyRead-compatible dict with normalization."""
    raw_conditions = json.loads(row.conditions_json) if row.conditions_json else []
    op_map = {">": "gt", "<": "lt", ">=": "gte", "<=": "lte", "=": "eq", "==": "eq"}
    norm_conditions = []
    for c in raw_conditions:
        if isinstance(c, dict):
            c_dict = dict(c)
            op = str(c_dict.get("operator", "gt")).lower()
            c_dict["operator"] = op_map.get(op, op)
            norm_conditions.append(c_dict)

    raw_action = json.loads(row.action_json) if row.action_json else {}
    norm_action = {}
    if isinstance(raw_action, dict):
        norm_action = dict(raw_action)
        if "action" in norm_action and "side" not in norm_action:
            norm_action["side"] = norm_action.pop("action")
        if "side" not in norm_action:
            norm_action["side"] = "BUY"
        if "quantity" not in norm_action:
            norm_action["quantity"] = 10
        if "order_type" not in norm_action:
            norm_action["order_type"] = "MARKET"

    return {
        "id": row.id,
        "name": row.name,
        "symbols": json.loads(row.symbols_json) if row.symbols_json else [],
        "conditions": norm_conditions,
        "action": norm_action,
        "enabled": row.enabled,
        "execution_mode": getattr(row, "execution_mode", "PAPER") or "PAPER",
        "broker_account_id": getattr(row, "broker_account_id", None),
        "capital_allocated": getattr(row, "capital_allocated", 10000.0) or 10000.0,
        "created_at": row.created_at,
    }


# Default seeded marketplace catalogue
_DEFAULT_MARKETPLACE_ITEMS = [
    {
        "id": "mkt-alpha-momentum-01",
        "creator_name": "AlphaQuant Labs",
        "name": "Alpha Momentum Breakout Pro",
        "description": "High-frequency volatility breakout algorithm with 50/200 SMA trend filter and dynamic ATR trailing stop.",
        "category": "Momentum",
        "pricing_type": "FREE",
        "price": 0.0,
        "min_capital": 5000.0,
        "win_rate": 78.4,
        "total_return_pct": 42.6,
        "max_drawdown_pct": 5.2,
        "subscribers_count": 318,
        "rating": 4.9,
        "symbols": ["AAPL", "NVDA", "MSFT"],
        "is_published": True,
    },
    {
        "id": "mkt-rsi-mean-rev-02",
        "creator_name": "Apex Algo Trading",
        "name": "RSI Extreme Mean Reversion",
        "description": "Exploits intra-day oversold bounces on mega-cap equities using RSI(14) < 30 and volume surges.",
        "category": "Mean Reversion",
        "pricing_type": "FREE",
        "price": 0.0,
        "min_capital": 2500.0,
        "win_rate": 74.2,
        "total_return_pct": 31.8,
        "max_drawdown_pct": 4.1,
        "subscribers_count": 245,
        "rating": 4.8,
        "symbols": ["GOOGL", "AMZN", "AAPL"],
        "is_published": True,
    },
    {
        "id": "mkt-bb-squeeze-03",
        "creator_name": "DeltaEdge Capital",
        "name": "Bollinger Bands Squeeze Scalper",
        "description": "Identifies low-volatility compression and enters explosive directional expansions.",
        "category": "Breakout",
        "pricing_type": "PAID",
        "price": 29.0,
        "min_capital": 10000.0,
        "win_rate": 81.1,
        "total_return_pct": 54.3,
        "max_drawdown_pct": 6.4,
        "subscribers_count": 189,
        "rating": 5.0,
        "symbols": ["NVDA", "MSFT"],
        "is_published": True,
    },
    {
        "id": "mkt-gold-trend-04",
        "creator_name": "ThetaForge",
        "name": "EMA 20/50 Dual Cross Trend",
        "description": "Multi-timeframe exponential moving average trend-following model designed for trend continuation.",
        "category": "Trend Following",
        "pricing_type": "FREE",
        "price": 0.0,
        "min_capital": 3000.0,
        "win_rate": 70.8,
        "total_return_pct": 27.5,
        "max_drawdown_pct": 4.9,
        "subscribers_count": 162,
        "rating": 4.7,
        "symbols": ["AAPL", "GOOGL"],
        "is_published": True,
    },
    {
        "id": "mkt-options-wheel-05",
        "creator_name": "OptionsMaster",
        "name": "High-IV Options Wheel Engine",
        "description": "Automated synthetic covered calls and cash-secured put rotation for consistent monthly premium harvest.",
        "category": "Options",
        "pricing_type": "PAID",
        "price": 49.0,
        "min_capital": 15000.0,
        "win_rate": 86.5,
        "total_return_pct": 48.0,
        "max_drawdown_pct": 7.1,
        "subscribers_count": 420,
        "rating": 4.95,
        "symbols": ["MSFT", "AMZN", "NVDA"],
        "is_published": True,
    },
]


@router.post("", response_model=StrategyRead, status_code=201)
async def create_strategy(
    payload: StrategyCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new trading strategy."""
    record = StrategyRecord(
        name=payload.name,
        symbols_json=json.dumps([s.upper() for s in payload.symbols]),
        conditions_json=json.dumps([c.model_dump(mode="json") for c in payload.conditions]),
        action_json=json.dumps(payload.action.model_dump(mode="json")),
        enabled=payload.enabled,
        execution_mode=payload.execution_mode.upper(),
        broker_account_id=payload.broker_account_id,
        capital_allocated=payload.capital_allocated,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    logger.info("Strategy created: %s (%s) [mode=%s]", record.name, record.id, record.execution_mode)

    # Signal engine to reload
    from app.main import get_engine

    engine = get_engine()
    if engine:
        await engine.reload_strategies()

    return _row_to_read(record)


@router.get("", response_model=list[StrategyRead])
async def list_strategies(db: AsyncSession = Depends(get_db)):
    """List all strategies."""
    result = await db.execute(
        select(StrategyRecord).order_by(StrategyRecord.created_at.desc())
    )
    return [_row_to_read(r) for r in result.scalars().all()]


# ── MARKETPLACE LISTING ROUTE (DECLARED BEFORE DYNAMIC {strategy_id}) ─────────
@router.get("/marketplace")
async def get_marketplace_strategies(
    page: int = 1,
    limit: int = 10,
    category: str | None = None,
    pricing_type: str | None = None,
    symbol: str | None = None,
    search: str | None = None,
    sort_by: str | None = "roi_desc",
    db: AsyncSession = Depends(get_db),
):
    """List marketplace strategies with pagination, category filter, symbol search, and text query."""
    # 1. Fetch DB published items
    db_stmt = select(MarketplaceStrategyRecord).where(MarketplaceStrategyRecord.is_published == True)  # noqa: E712
    db_res = await db.execute(db_stmt)
    db_items = db_res.scalars().all()

    items = []
    for d in db_items:
        items.append({
            "id": d.id,
            "creator_name": d.creator_name,
            "name": d.name,
            "description": d.description,
            "category": d.category,
            "pricing_type": d.pricing_type,
            "price": d.price,
            "min_capital": d.min_capital,
            "win_rate": d.win_rate,
            "total_return_pct": d.total_return_pct,
            "max_drawdown_pct": d.max_drawdown_pct,
            "subscribers_count": d.subscribers_count,
            "rating": d.rating,
            "symbols": json.loads(d.symbols_json) if d.symbols_json else ["AAPL"],
            "is_published": d.is_published,
        })

    # Combine with seed items if DB has fewer
    existing_ids = {i["id"] for i in items}
    for seed in _DEFAULT_MARKETPLACE_ITEMS:
        if seed["id"] not in existing_ids:
            items.append(seed)

    # 2. Filtering
    if category and category.lower() != "all":
        items = [i for i in items if i["category"].lower() == category.lower()]

    if pricing_type and pricing_type.lower() != "all":
        items = [i for i in items if i["pricing_type"].lower() == pricing_type.lower()]

    if symbol:
        sym_clean = symbol.strip().upper()
        items = [i for i in items if any(sym_clean in s.upper() for s in i.get("symbols", []))]

    if search:
        q = search.strip().lower()
        items = [
            i for i in items
            if q in i["name"].lower() or q in i.get("description", "").lower() or q in i.get("creator_name", "").lower()
        ]

    # 3. Sorting
    if sort_by == "roi_desc":
        items.sort(key=lambda x: x.get("total_return_pct", 0), reverse=True)
    elif sort_by == "subscribers_desc":
        items.sort(key=lambda x: x.get("subscribers_count", 0), reverse=True)
    elif sort_by == "rating_desc":
        items.sort(key=lambda x: x.get("rating", 0), reverse=True)
    elif sort_by == "drawdown_asc":
        items.sort(key=lambda x: x.get("max_drawdown_pct", 999))

    # 4. Pagination
    total = len(items)
    page_safe = max(1, page)
    limit_safe = max(1, min(limit, 100))
    start_idx = (page_safe - 1) * limit_safe
    end_idx = start_idx + limit_safe
    paginated_items = items[start_idx:end_idx]
    total_pages = (total + limit_safe - 1) // limit_safe

    return {
        "items": paginated_items,
        "total": total,
        "page": page_safe,
        "limit": limit_safe,
        "totalPages": total_pages,
    }


@router.post("/marketplace/publish")
async def publish_to_marketplace(
    req: PublishMarketplaceRequest,
    db: AsyncSession = Depends(get_db),
):
    """Publish a custom strategy to the public marketplace."""
    strat_stmt = select(StrategyRecord).where(StrategyRecord.id == req.strategy_id)
    res = await db.execute(strat_stmt)
    strat = res.scalar_one_or_none()
    if not strat:
        raise HTTPException(status_code=404, detail="Source strategy not found")

    mkt_item = MarketplaceStrategyRecord(
        creator_name=req.creator_name,
        name=strat.name,
        description=req.description or f"Custom algo by {req.creator_name}",
        category=req.category,
        pricing_type=req.pricing_type,
        price=req.price,
        symbols_json=strat.symbols_json,
        strategy_config_json=json.dumps({
            "conditions": json.loads(strat.conditions_json),
            "action": json.loads(strat.action_json),
        }),
        is_published=True,
    )
    db.add(mkt_item)
    await db.commit()
    await db.refresh(mkt_item)

    logger.info("Published strategy %s to marketplace as %s", strat.name, mkt_item.id)
    return {"success": True, "marketplace_id": mkt_item.id, "name": mkt_item.name}


# ── DYNAMIC SINGLE STRATEGY CRUD ─────────────────────────────────────────────
@router.get("/{strategy_id}", response_model=StrategyRead)
async def get_strategy(
    strategy_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get a single strategy by ID."""
    result = await db.execute(
        select(StrategyRecord).where(StrategyRecord.id == strategy_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return _row_to_read(row)


@router.patch("/{strategy_id}", response_model=StrategyRead)
async def update_strategy(
    strategy_id: str,
    payload: StrategyUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Partially update a strategy."""
    result = await db.execute(
        select(StrategyRecord).where(StrategyRecord.id == strategy_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")

    if payload.name is not None:
        row.name = payload.name
    if payload.symbols is not None:
        row.symbols_json = json.dumps([s.upper() for s in payload.symbols])
    if payload.conditions is not None:
        row.conditions_json = json.dumps(
            [c.model_dump(mode="json") for c in payload.conditions]
        )
    if payload.action is not None:
        row.action_json = json.dumps(payload.action.model_dump(mode="json"))
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.execution_mode is not None:
        row.execution_mode = payload.execution_mode.upper()
    if payload.broker_account_id is not None:
        row.broker_account_id = payload.broker_account_id
    if payload.capital_allocated is not None:
        row.capital_allocated = payload.capital_allocated

    await db.commit()
    await db.refresh(row)
    logger.info("Strategy updated: %s (%s)", row.name, row.id)

    from app.main import get_engine

    engine = get_engine()
    if engine:
        await engine.reload_strategies()

    return _row_to_read(row)


@router.delete("/{strategy_id}", status_code=204)
async def delete_strategy(
    strategy_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a strategy."""
    result = await db.execute(
        select(StrategyRecord).where(StrategyRecord.id == strategy_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")

    await db.delete(row)
    await db.commit()
    logger.info("Strategy deleted: %s", strategy_id)

    from app.main import get_engine

    engine = get_engine()
    if engine:
        await engine.reload_strategies()


@router.post("/{strategy_id}/deploy")
async def deploy_strategy(
    strategy_id: str,
    req: DeployStrategyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Deploy a strategy to live engine / broker."""
    strat_stmt = select(StrategyRecord).where(StrategyRecord.id == strategy_id)
    res = await db.execute(strat_stmt)
    strat = res.scalar_one_or_none()
    strat_name = strat.name if strat else f"Marketplace Strategy ({strategy_id[:8]})"
    mode = req.execution_mode.upper()

    # If Live Mode, ensure an active, non-expired broker account is linked
    if mode == "LIVE":
        from app.models.broker_account import BrokerAccountRecord
        b_stmt = select(BrokerAccountRecord).where(BrokerAccountRecord.status == "CONNECTED")
        if req.broker_account_id:
            b_stmt = select(BrokerAccountRecord).where(BrokerAccountRecord.id == req.broker_account_id)
        b_res = await db.execute(b_stmt)
        b_acc = b_res.scalars().first()

        if not b_acc:
            raise HTTPException(
                status_code=400,
                detail="Cannot deploy to Live Mode without a connected broker account. Please connect your broker account first.",
            )
        if b_acc.is_token_expired():
            raise HTTPException(
                status_code=400,
                detail=f"Your broker token for {b_acc.broker_name} has expired. Please re-authorize via OAuth first.",
            )
        if strat:
            strat.broker_account_id = b_acc.id

    deployment = StrategyDeploymentRecord(
        marketplace_strategy_id=strategy_id,
        strategy_name=strat_name,
        execution_mode=mode,
        broker_name=req.broker_name,
        multiplier=req.multiplier,
        capital_allocated=req.capital_allocated,
        status="RUNNING",
    )
    db.add(deployment)

    if strat:
        strat.enabled = True
        strat.execution_mode = mode
        strat.capital_allocated = req.capital_allocated

    await db.commit()
    await db.refresh(deployment)

    from app.main import get_engine
    engine = get_engine()
    if engine:
        await engine.reload_strategies()

    logger.info(
        "Deployed strategy %s [%s] in %s mode with multiplier x%.1f to broker %s",
        deployment.strategy_name,
        deployment.id,
        deployment.execution_mode,
        deployment.multiplier,
        deployment.broker_name,
    )
    return {
        "success": True,
        "deployment_id": deployment.id,
        "strategy_id": strategy_id,
        "status": deployment.status,
        "multiplier": deployment.multiplier,
        "broker_name": deployment.broker_name,
        "execution_mode": deployment.execution_mode,
    }


@router.post("/{strategy_id}/pause")
async def pause_strategy(
    strategy_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Pause strategy execution."""
    strat_stmt = select(StrategyRecord).where(StrategyRecord.id == strategy_id)
    res = await db.execute(strat_stmt)
    strat = res.scalar_one_or_none()

    if strat:
        strat.enabled = False
        await db.commit()

    from app.main import get_engine
    engine = get_engine()
    if engine:
        await engine.reload_strategies()

    logger.info("Paused strategy %s", strategy_id)
    return {"success": True, "strategy_id": strategy_id, "status": "PAUSED"}


@router.post("/kill-switch")
async def emergency_kill_switch(
    req: KillSwitchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Emergency Panic Button: Immediately pause all running strategies and halt trading engine order dispatch."""
    from app.main import get_engine
    from app.core.audit import log_audit_event
    from app.market_data.manager import ws_manager

    engine = get_engine()
    action = req.action.upper().strip()

    if action == "PAUSE_ALL" or action == "SQUARE_OFF_ALL":
        # 1. Update all DB strategies to disabled
        strat_stmt = select(StrategyRecord).where(StrategyRecord.enabled == True)  # noqa: E712
        strat_res = await db.execute(strat_stmt)
        active_strats = strat_res.scalars().all()
        for s in active_strats:
            s.enabled = False

        deploy_stmt = select(StrategyDeploymentRecord).where(StrategyDeploymentRecord.status == "RUNNING")
        deploy_res = await db.execute(deploy_stmt)
        active_deploys = deploy_res.scalars().all()
        for d in active_deploys:
            d.status = "PAUSED"

        await db.commit()

        # 2. Trigger engine risk manager kill-switch
        if engine and hasattr(engine, "risk_manager"):
            engine.risk_manager.trigger_kill_switch(req.reason)
            await engine.reload_strategies()

        # 3. Log audit event
        await log_audit_event(
            db=db,
            action="EMERGENCY_KILL_SWITCH_ACTIVATED",
            resource_type="TRADING_ENGINE",
            status="CRITICAL",
            details={"action": action, "reason": req.reason, "paused_strategies": len(active_strats)},
        )

        # 4. Broadcast emergency WebSocket alert
        await ws_manager.broadcast("trades", {
            "event": "KILL_SWITCH_ACTIVE",
            "message": f"EMERGENCY KILL-SWITCH: {req.reason}",
            "status": "HALTED",
        })

        return {
            "success": True,
            "status": "HALTED",
            "action": action,
            "paused_strategies_count": len(active_strats),
            "paused_deployments_count": len(active_deploys),
            "message": "All live strategy execution halted immediately.",
        }

    elif action == "RESUME_ALL":
        if engine and hasattr(engine, "risk_manager"):
            engine.risk_manager.reset_kill_switch()
            await engine.reload_strategies()

        await log_audit_event(
            db=db,
            action="KILL_SWITCH_RELEASED",
            resource_type="TRADING_ENGINE",
            status="SUCCESS",
            details={"action": action, "reason": "Operator resumed operations"},
        )

        return {
            "success": True,
            "status": "RUNNING",
            "action": action,
            "message": "Trading engine kill-switch released. Normal routing resumed.",
        }

    raise HTTPException(status_code=400, detail=f"Invalid kill-switch action: {req.action}")

