"""Dashboard summary and task completion endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user, get_optional_current_user
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.trading import StrategyRecord, TradeRecord
from app.models.user import UserRecord

logger = get_logger("api.dashboard")
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class CompleteTaskRequest(BaseModel):
    task_id: str = Field(..., description="Unique task identifier")
    completed: bool = True


# In-memory store of completed dashboard tasks, scoped PER USER so one
# tenant can never see/reset another's onboarding progress.  Keyed by the
# authenticated user id (server-derived identity, never client-supplied).
# NOTE: this is intentionally in-memory (like the historical behavior) — it is
# cosmetic onboarding state that resets on restart.  It is NOT financial
# state.  A DB-backed version would require a schema migration that is not
# justified for a purely cosmetic checklist.
_COMPLETED_TASKS: dict[str, set[str]] = {}


@router.get("/summary")
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db),
    user: Optional[UserRecord] = Depends(get_optional_current_user),
):
    """Unified dashboard summary including weekReturn, monthReturn, topStrategies, and pendingTasks.

    Two contractual views:

    * **Authenticated** — every metric (PnL, returns, top strategies and trade
      counts) is strictly tenant-scoped to the server-derived ``user.id`` from
      the bearer token.  Another tenant's trades/strategies never appear, and a
      client-supplied ``user_id`` is never consulted.
    * **Anonymous** — the landing page fetches this endpoint via
      ``publicFetch`` so guests can render the demo dashboard; the global demo
      aggregates are the intentional public behavior and are preserved as-is.

    (Making the anonymous view disappear would break the intentional guest
    landing experience — see the frontend ``{ public: true }`` usage in
    ``client/src/pages/Dashboard.jsx``.)
    """
    from datetime import timedelta

    trades_stmt = select(TradeRecord).order_by(TradeRecord.executed_at.desc())
    strat_stmt = select(StrategyRecord).order_by(StrategyRecord.created_at.desc())

    if user is not None:
        # Server-derived tenant scope for authenticated callers.
        trades_stmt = trades_stmt.where(TradeRecord.user_id == user.id)
        strat_stmt = strat_stmt.where(StrategyRecord.user_id == user.id)

    trades_res = await db.execute(trades_stmt)
    all_trades = trades_res.scalars().all()

    total_realized_pnl = sum(t.pnl for t in all_trades if t.pnl is not None)
    winning_trades = sum(1 for t in all_trades if t.pnl and t.pnl > 0)
    total_trades_count = len(all_trades)
    win_rate = round((winning_trades / total_trades_count * 100), 1) if total_trades_count > 0 else 0.0

    # Real time-windowed returns computed strictly from executed trades
    base_capital = 100_000.0
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    def _get_tz_dt(dt):
        if not dt:
            return now
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    week_pnl = sum(
        t.pnl for t in all_trades
        if t.pnl is not None and _get_tz_dt(t.executed_at) >= week_ago
    )
    month_pnl = sum(
        t.pnl for t in all_trades
        if t.pnl is not None and _get_tz_dt(t.executed_at) >= month_ago
    )

    week_return = round((week_pnl / base_capital) * 100, 2) if base_capital else 0.0
    month_return = round((month_pnl / base_capital) * 100, 2) if base_capital else 0.0

    # 2. Top strategies.
    #
    # NO FABRICATED DATA EVER FOR AUTHENTICATED CALLERS.  The top-strategy list
    # is strictly derived from the caller's OWN persisted strategies and their
    # OWN executed trades.  A user with no strategies gets an honest empty list;
    # a strategy with no fills reports zero pnl/winRate/tradesCount — never an
    # invented "proof" figure.  (Until this change, an authenticated user with
    # no strategies received three hardcoded demo strategies with invented PnL
    # and winRate — misleading fake-product behavior.  See
    # tests/test_dashboard_no_fake_strategies.py.)
    #
    # The one intentional exception is the ANONYMOUS guest landing view, which
    # preserves the demo aggregate (public feature, see the docstring and the
    # frontend `{ public: true }` usage in client/src/pages/Dashboard.jsx).
    strat_res = await db.execute(strat_stmt)
    strat_records = strat_res.scalars().all()

    if user is None:
        # ── Anonymous guest: intentional demo top strategies ───────────────
        if strat_records:
            top_strategies = [
                {
                    "id": s.id,
                    "name": s.name,
                    "symbols": json.loads(s.symbols_json) if s.symbols_json else [],
                    "pnl": round(total_realized_pnl * 0.6, 2),
                    "winRate": 76.4,
                    "tradesCount": max(total_trades_count, 12),
                    "status": "Active" if s.enabled else "Paused",
                }
                for s in strat_records[:5]
            ]
        else:
            top_strategies = [
                {
                    "id": "sma-cross-50-200",
                    "name": "SMA (50/200) Golden Cross",
                    "symbols": ["AAPL", "NVDA", "MSFT"],
                    "pnl": 4820.50,
                    "winRate": 78.2,
                    "tradesCount": 34,
                    "status": "Active",
                },
                {
                    "id": "rsi-reversal-30",
                    "name": "RSI Oversold Momentum",
                    "symbols": ["GOOGL", "AMZN"],
                    "pnl": 3190.00,
                    "winRate": 71.4,
                    "tradesCount": 21,
                    "status": "Active",
                },
                {
                    "id": "bb-squeeze-breakout",
                    "name": "Bollinger Bands Volatility Squeeze",
                    "symbols": ["NVDA", "AAPL"],
                    "pnl": 2450.25,
                    "winRate": 68.9,
                    "tradesCount": 18,
                    "status": "Active",
                },
            ]
    else:
        # ── Authenticated: honest, tenant-scoped, trade-derived metrics ────
        strat_ids = [s.id for s in strat_records]
        # Aggregate realized PnL and win outcomes per strategy from the caller's
        # OWN executed trades (server-scoped to user.id above).
        strat_pnl: dict[str, float] = {}
        strat_wins: dict[str, int] = {}
        strat_trades: dict[str, int] = {}
        if strat_ids and all_trades:
            for t in all_trades:
                if not t.strategy_id or t.strategy_id not in strat_ids:
                    continue
                strat_trades[str(t.strategy_id)] = strat_trades.get(str(t.strategy_id), 0) + 1
                if t.pnl is not None:
                    strat_pnl[str(t.strategy_id)] = strat_pnl.get(str(t.strategy_id), 0.0) + float(t.pnl)
                    if t.pnl > 0:
                        strat_wins[str(t.strategy_id)] = strat_wins.get(str(t.strategy_id), 0) + 1

        top_strategies = []
        for s in strat_records[:5]:
            sid = str(s.id)
            n_trades = strat_trades.get(sid, 0)
            pnl = strat_pnl.get(sid, 0.0)
            wins = strat_wins.get(sid, 0)
            win_rate = round((wins / n_trades) * 100, 1) if n_trades > 0 else 0.0
            top_strategies.append({
                "id": s.id,
                "name": s.name,
                "symbols": json.loads(s.symbols_json) if s.symbols_json else [],
                "pnl": round(pnl, 2),
                "winRate": win_rate,
                "tradesCount": n_trades,
                "status": "Active" if s.enabled else "Paused",
            })

    # 3. Tasks list (Pending vs Completed) — scoped to the authenticated user
    #    (or, for the anonymous/public guest view, a fixed empty baseline so
    #    the landing page never leaks another tenant's onboarding progress).
    _user_tasks: set[str] = (
        _COMPLETED_TASKS.get(user.id, set()) if user is not None else set()
    )
    available_tasks = [
        {
            "id": "marketplace_setup",
            "title": "Subscribe to Marketplace Strategy",
            "description": "Choose a proven algorithmic strategy from the community marketplace.",
            "is_completed": "marketplace_setup" in _user_tasks,
        },
        {
            "id": "broker_setup",
            "title": "Connect Broker API",
            "description": "Link your live Angel One or Simulated broker account credentials.",
            "is_completed": "broker_setup" in _user_tasks,
        },
        {
            "id": "subscription_setup",
            "title": "Activate Pro Membership",
            "description": "Unlock unlimited multi-strategy execution and real-time alerts.",
            "is_completed": "subscription_setup" in _user_tasks,
        },
        {
            "id": "risk_limits_setup",
            "title": "Configure Max Drawdown Limit",
            "description": "Set auto-cutoff limits to prevent overnight account drawdowns.",
            "is_completed": "risk_limits_setup" in _user_tasks,
        },
    ]

    pending_tasks = [t for t in available_tasks if not t["is_completed"]]

    return {
        "totalRealizedPnl": round(total_realized_pnl, 2),
        "totalTrades": total_trades_count,
        "winRate": win_rate,
        "weekReturn": week_return,
        "monthReturn": month_return,
        "topStrategies": top_strategies,
        "pendingTasks": pending_tasks,
        "allTasks": available_tasks,
        "engineStatus": "RUNNING",
    }


@router.post("/complete-task")
async def complete_task(
    req: CompleteTaskRequest,
    user: UserRecord = Depends(get_current_user),
):
    """Mark the AUTHENTICATED user's dashboard setup or onboarding task as
    completed or pending.  Requires a bearer token — anonymous callers receive
    401 (a task cannot be toggled without an identity).  State is scoped to the
    server-derived ``user.id`` so one tenant can never alter another's progress.
    """
    tasks = _COMPLETED_TASKS.setdefault(user.id, set())
    if req.completed:
        tasks.add(req.task_id)
    else:
        tasks.discard(req.task_id)

    logger.info("User %s task %s completion state updated to %s", user.id, req.task_id, req.completed)
    return {
        "success": True,
        "task_id": req.task_id,
        "is_completed": req.task_id in _COMPLETED_TASKS.get(user.id, set()),
    }
