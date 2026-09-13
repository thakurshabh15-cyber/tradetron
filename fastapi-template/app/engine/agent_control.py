"""Phase 1 Step 4: autonomous-agent control plane (config + decisions + loop).

The Agent Console in the browser is a CONTROL PLANE, never an execution
authority.  This service owns the durable per-tenant ``agent_configs`` and
``agent_decisions`` rows and every lifecycle transition; the browser only ever
calls read/command endpoints on this module and can never widen a safety
bound (mode, autonomy ladder, ownership, broker mode).

Layered contracts (all fail-closed, deterministic):

* Configuration   — exactly one ``agent_configs`` row per tenant; every field
  validated server-side; ``user_id`` never client-supplied; ``LIVE`` refused
  unless ``settings.broker_mode == "live"``; autonomy 0..3 only.
* Lifecycle       — ``IDLE → RUNNING ⇄ PAUSED → STOPPED`` (+ ``FAILED`` only
  when the evaluation loop cannot make progress) with CAS-protected durable
  status transitions.  STARTING/STOPPING render states are derived from
  in-flight control requests — they are never fake persisted rows.
* Evaluation      — the bounded evaluation loop (``AgentEvaluationScheduler``)
  enqueues ``trading_agent / evaluate_market`` tasks for RUNNING configs on a
  fixed interval with a slot idempotency key (``cfg:{id}:eval:{slot}``), so a
  crashed/restarted loop can never enqueue duplicate work.  One durable
  decision row per evaluated symbol per slot.
* Execution       — the ``evaluate_market`` handler NEVER reaches a broker.
  A TRADE/NEEDS_APPROVAL decision enqueues a ``trading_agent / execute_trade``
  task (created_by="agent") so the Phase 1 Step 2 runtime autonomy/approval
  gates are consulted and the ONLY broker-reaching path stays
  ``AgentIntentTriggerBridge → AgentTradingService``.
* Traceability    — agent_config → task (evaluate) → decision → task
  (execute) → intent → order → position.  A bounded link-back sweep inside the
  scheduler loop records the terminal intent/order ids onto the decision row.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select

from app.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.agent import AgentRecord, AgentRuntimeConfigRecord, AgentTaskRecord
from app.models.agent_control import (
    AGENT_STATUS_FAILED,
    AGENT_STATUS_IDLE,
    AGENT_STATUS_PAUSED,
    AGENT_STATUS_RUNNING,
    AGENT_STATUS_STOPPED,
    AUTONOMY_LEVELS,
    AUTONOMY_LIVE,
    AUTONOMY_OBSERVE,
    DECISION_FAILED,
    DECISION_NEEDS_APPROVAL,
    DECISION_NO_TRADE,
    DECISION_REJECTED,
    DECISION_TRADE,
    MODE_LIVE,
    MODE_PAPER,
    AgentConfigRecord,
    AgentDecisionRecord,
)

logger = get_logger("engine.agent_control")

# ── Deterministic error codes ───────────────────────────────────────────────
ERR_CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"
ERR_CONFIG_EXISTS = "CONFIG_EXISTS"
ERR_STRATEGY_NOT_FOUND = "STRATEGY_NOT_FOUND"
ERR_STRATEGY_OWNERSHIP = "STRATEGY_OWNERSHIP_DENIED"
ERR_BAD_SYMBOLS = "INVALID_MARKET_UNIVERSE"
ERR_BAD_MODE = "EXECUTION_MODE_UNAVAILABLE"
ERR_BAD_AUTONOMY = "INVALID_AUTONOMY_LEVEL"
ERR_BAD_POLICY = "INVALID_POLICY"
ERR_ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
ERR_INVALID_STATE = "INVALID_STATE"
ERR_OWNER_MISMATCH = "OWNER_MISMATCH"
ERR_TASK_NOT_APPROVABLE = "TASK_NOT_APPROVABLE"
ERR_EVALUATION_FAILED = "EVALUATION_FAILED"

#: Bound on how many decision rows the link-back sweep touches per pass.
_LINK_BACK_BATCH = 50

# ── Shared rolling-indicator state for the evaluation loop (bounded) ───────
# The TradingEngine keeps ONE persistent StrategyEvaluator for its whole
# lifetime so SMA/EMA/RSI/MACD/ATR/BOLLINGER and cross_above/cross_below
# conditions see genuine price history.  The agent loop mirrors exactly that:
# one shared, bounded evaluator (per-symbol deques capped at 1000 points)
# namespaced per strategy, so an indicator condition keeps its rolling history
# across slots instead of resetting to a single tick on every evaluation.
_SHARED_EVALUATOR: Optional[Any] = None


def get_shared_evaluator() -> Any:
    """Return the process-wide :class:`StrategyEvaluator` (lazy, bounded)."""
    global _SHARED_EVALUATOR
    if _SHARED_EVALUATOR is None:
        from app.engine.strategy_evaluator import StrategyEvaluator

        _SHARED_EVALUATOR = StrategyEvaluator()
    return _SHARED_EVALUATOR


def reset_shared_evaluator() -> None:
    """Drop the shared evaluator so its history starts fresh.

    Tests use this to keep every hermetic environment independent.  A process
    restart naturally loses in-memory history too — indicator conditions simply
    require warm-up again (honest behavior, documented, never a fabricated signal).
    """
    global _SHARED_EVALUATOR
    _SHARED_EVALUATOR = None


class AgentControlError(Exception):
    """Deterministic control-plane rejection (mapped to HTTP by the API)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    ts = value
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str, sort_keys=True)


def _load(text: Optional[str]) -> Any:
    if not text:
        return {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {}
def config_to_dict(rec: AgentConfigRecord) -> dict[str, Any]:
    """Deterministic ISO-8601/UTC serialization (never exposes internal JSON)."""
    raw_symbols = _load(rec.symbols_json)
    if not isinstance(raw_symbols, dict):
        symbols: list[str] = []
    else:
        symbols = raw_symbols.get("symbols", [])
    return {
        "id": rec.id,
        "user_id": rec.user_id,
        "name": rec.name,
        "strategy_id": rec.strategy_id,
        "symbols": symbols,
        "execution_mode": rec.execution_mode,
        "autonomy_level": int(rec.autonomy_level),
        "approval_policy": _load(rec.approval_policy_json),
        "risk_policy": _load(rec.risk_policy_json),
        "status": rec.status,
        "last_error": rec.last_error,
        "started_at": _iso(rec.started_at),
        "stopped_at": _iso(rec.stopped_at),
        "created_at": _iso(rec.created_at),
        "updated_at": _iso(rec.updated_at),
    }


def decision_to_dict(rec: AgentDecisionRecord) -> dict[str, Any]:
    """Deterministic decision serialization (Phase F decision contract)."""
    return {
        "decision_id": rec.id,
        "agent_config_id": rec.agent_config_id,
        "user_id": rec.user_id,
        "task_id": rec.task_id,
        "intent_id": rec.intent_id,
        "order_id": rec.order_id,
        "strategy_id": rec.strategy_id,
        "decision": rec.decision,
        "symbol": rec.symbol,
        "side": rec.side,
        "quantity": rec.quantity,
        "mode": rec.mode,
        "reason": rec.reason,
        "risk_result": rec.risk_result,
        "risk_reason": rec.risk_reason,
        "approval_result": rec.approval_result,
        "execution_result": rec.execution_result,
        "error": _load(rec.error_json),
        "created_at": _iso(rec.created_at),
        "updated_at": _iso(rec.updated_at),
    }
class AgentControlService:
    """Tenant-scoped autonomous-agent control plane (config, decisions, loop)."""

    # ── Validation helpers (server-side truth; the browser never widens) ──
    @staticmethod
    def _validate_symbols(symbols: Any) -> list[str]:
        if not isinstance(symbols, list) or not symbols:
            raise AgentControlError(
                ERR_BAD_SYMBOLS, "market universe must be a non-empty list of symbols"
            )
        cleaned: list[str] = []
        for raw in symbols:
            sym = str(raw or "").strip().upper()
            if not sym or len(sym) > 30:
                raise AgentControlError(ERR_BAD_SYMBOLS, f"invalid symbol {raw!r}")
            if sym not in cleaned:
                cleaned.append(sym)
        if len(cleaned) > 40:
            raise AgentControlError(ERR_BAD_SYMBOLS, "market universe capped at 40 symbols")
        return cleaned

    @staticmethod
    def _validate_autonomy(level: Any) -> int:
        try:
            value = int(level)
        except (TypeError, ValueError):
            raise AgentControlError(
                ERR_BAD_AUTONOMY, f"autonomy_level must be an integer, got {level!r}"
            ) from None
        if value not in AUTONOMY_LEVELS:
            raise AgentControlError(
                ERR_BAD_AUTONOMY,
                f"autonomy_level must be one of {AUTONOMY_LEVELS}, got {value}",
            )
        return value

    @staticmethod
    def _validate_mode(mode: Any) -> str:
        raw = str(mode or MODE_PAPER).strip().upper()
        if raw == "DEMO":
            raw = MODE_PAPER  # canonical alias, never persisted
        if raw not in (MODE_PAPER, MODE_LIVE):
            raise AgentControlError(
                ERR_BAD_MODE, f"execution_mode must be PAPER or LIVE, got {mode!r}"
            )
        # LIVE is a server-gated authority: the control plane refuses to even
        # configure an agent for LIVE unless the broker is already in live mode.
        if raw == MODE_LIVE and settings.broker_mode != "live":
            raise AgentControlError(
                ERR_BAD_MODE,
                "LIVE execution is unavailable (broker not in live mode); "
                "PAPER-only until a broker sandbox is configured",
            )
        return raw

    @staticmethod
    def _validate_approval_policy(policy: Any) -> dict[str, Any]:
        if policy is None:
            policy = {}
        if not isinstance(policy, dict):
            raise AgentControlError(ERR_BAD_POLICY, "approval_policy must be an object")
        approval_required = bool(policy.get("approval_required", True))
        window = int(policy.get("window_seconds", settings.agent_approval_window_seconds))
        if not 60 <= window <= 86400:
            raise AgentControlError(
                ERR_BAD_POLICY, "approval window must be within [60, 86400] seconds"
            )
        return {"approval_required": approval_required, "window_seconds": window}

    @staticmethod
    def _validate_risk_policy(policy: Any) -> dict[str, Any]:
        if policy is None:
            policy = {}
        if not isinstance(policy, dict):
            raise AgentControlError(ERR_BAD_POLICY, "risk_policy must be an object")
        max_position = int(policy.get("max_position_size", settings.max_position_size))
        if not 1 <= max_position <= settings.agent_max_intent_quantity:
            raise AgentControlError(
                ERR_BAD_POLICY,
                f"max_position_size must be within [1, {settings.agent_max_intent_quantity}]",
            )
        rate = int(policy.get("max_orders_per_minute", settings.max_orders_per_minute))
        if not 1 <= rate <= 60:
            raise AgentControlError(ERR_BAD_POLICY, "max_orders_per_minute must be within [1, 60]")
        require_fresh_feed = bool(policy.get("require_fresh_feed", True))
        return {
            "max_position_size": max_position,
            "max_orders_per_minute": rate,
            "require_fresh_feed": require_fresh_feed,
        }

    # ── Strategy binding (ownership is mandatory, fail-closed) ──────────
    async def _resolve_strategy(self, strategy_id: str, user_id: str):
        from app.models.trading import StrategyRecord

        async with SessionLocal() as db:
            row = await db.get(StrategyRecord, strategy_id)
            if row is None:
                raise AgentControlError(ERR_STRATEGY_NOT_FOUND, f"strategy {strategy_id!r} not found")
            if row.user_id and str(row.user_id) != user_id:
                raise AgentControlError(ERR_STRATEGY_OWNERSHIP, "strategy belongs to another user")
            return {
                "id": row.id,
                "user_id": row.user_id,
                "symbols_json": row.symbols_json,
                "conditions_json": row.conditions_json,
                "action_json": row.action_json,
                "execution_mode": row.execution_mode,
                "broker_account_id": row.broker_account_id,
            }

    # ── Persisted-state reads (tenant-scoped) ───────────────────────────
    async def get_config(self, user_id: str) -> Optional[AgentConfigRecord]:
        async with SessionLocal() as db:
            return (
                await db.execute(
                    select(AgentConfigRecord).where(AgentConfigRecord.user_id == user_id)
                )
            ).scalar_one_or_none()

    async def get_config_owned(self, config_id: str, user_id: str) -> AgentConfigRecord:
        async with SessionLocal() as db:
            row = await db.get(AgentConfigRecord, config_id)
        if row is None or str(row.user_id) != user_id:
            raise AgentControlError(ERR_CONFIG_NOT_FOUND, "agent config not found")
        return row

    async def create_config(
        self,
        user_id: str,
        *,
        name: str,
        strategy_id: Optional[str] = None,
        symbols: list[str],
        execution_mode: str,
        autonomy_level: int,
        approval_policy: dict[str, Any],
        risk_policy: dict[str, Any],
    ) -> dict[str, Any]:
        existing = await self.get_config(user_id)
        if existing is not None:
            raise AgentControlError(ERR_CONFIG_EXISTS, "an agent config already exists for this user")
        if not name or not name.strip() or len(name) > 120:
            raise AgentControlError(ERR_INVALID_STATE, "agent name is required (<= 120 chars)")
        clean_symbols = self._validate_symbols(symbols)
        mode = self._validate_mode(execution_mode)
        autonomy = self._validate_autonomy(autonomy_level)
        approval = self._validate_approval_policy(approval_policy)
        risk = self._validate_risk_policy(risk_policy)
        strategy = None
        if strategy_id:
            strategy = await self._resolve_strategy(strategy_id, user_id)

        now = _utcnow()
        async with SessionLocal() as db:
            rec = AgentConfigRecord(
                user_id=user_id,
                name=name.strip(),
                strategy_id=strategy["id"] if strategy else None,
                symbols_json=_dump({"symbols": clean_symbols}),
                execution_mode=mode,
                autonomy_level=autonomy,
                approval_policy_json=_dump(approval),
                risk_policy_json=_dump(risk),
                status=AGENT_STATUS_IDLE,
                created_at=now,
                updated_at=now,
            )
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
        await self._broadcast_state(user_id, "agent_configured", rec.id)
        return config_to_dict(rec)

    async def patch_config(
        self,
        config_id: str,
        user_id: str,
        *,
        name: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbols: Optional[list[str]] = None,
        execution_mode: Optional[str] = None,
        autonomy_level: Optional[int] = None,
        approval_policy: Optional[dict[str, Any]] = None,
        risk_policy: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        rec = await self.get_config_owned(config_id, user_id)
        # Configuration is immutable while the agent is live — the browser can
        # never hot-swap a strategy/limits into a RUNNING execution.
        if rec.status == AGENT_STATUS_RUNNING:
            raise AgentControlError(
                ERR_ILLEGAL_TRANSITION,
                "pause or stop the agent before changing its configuration",
            )
        mutation: dict[str, Any] = {}
        if name is not None:
            if not name.strip() or len(name) > 120:
                raise AgentControlError(ERR_INVALID_STATE, "agent name is required (<= 120 chars)")
            mutation["name"] = name.strip()
        if strategy_id is not None:
            mutation["strategy_id"] = (
                (await self._resolve_strategy(strategy_id, user_id))["id"]
                if strategy_id else None
            )
        if symbols is not None:
            mutation["symbols_json"] = _dump({"symbols": self._validate_symbols(symbols)})
        if execution_mode is not None:
            mutation["execution_mode"] = self._validate_mode(execution_mode)
        if autonomy_level is not None:
            mutation["autonomy_level"] = self._validate_autonomy(autonomy_level)
        if approval_policy is not None:
            mutation["approval_policy_json"] = _dump(
                self._validate_approval_policy(approval_policy)
            )
        if risk_policy is not None:
            mutation["risk_policy_json"] = _dump(self._validate_risk_policy(risk_policy))
        if not mutation:
            raise AgentControlError(ERR_INVALID_STATE, "nothing to update")

        async with SessionLocal() as db:
            row = await db.get(AgentConfigRecord, config_id)
            if row is None or str(row.user_id) != user_id:
                raise AgentControlError(ERR_CONFIG_NOT_FOUND, "agent config not found")
            for key, value in mutation.items():
                setattr(row, key, value)
            row.updated_at = _utcnow()
            await db.commit()
            await db.refresh(row)
        await self._broadcast_state(user_id, "agent_configured", config_id)
        return config_to_dict(row)
# ── Lifecycle state machine (CAS-protected, idempotent) ─────────────
    async def _transition(
        self,
        config_id: str,
        user_id: str,
        *,
        allowed_from: tuple[str, ...],
        target: str,
        name: str,
        idempotent_when_target: bool = True,
    ) -> dict[str, Any]:
        async with SessionLocal() as db:
            row = await db.get(AgentConfigRecord, config_id)
            if row is None or str(row.user_id) != user_id:
                raise AgentControlError(ERR_CONFIG_NOT_FOUND, "agent config not found")
            if row.status == target:
                if not idempotent_when_target:
                    raise AgentControlError(
                        ERR_ILLEGAL_TRANSITION,
                        f"cannot {name} agent in status {row.status} (already {target})",
                    )
                await db.commit()
                return {"ok": True, "idempotent": True, "config": config_to_dict(row)}
            if row.status not in allowed_from:
                raise AgentControlError(
                    ERR_ILLEGAL_TRANSITION,
                    f"cannot {name} agent in status {row.status} "
                    f"(allowed: {', '.join(sorted(allowed_from))})",
                )
            now = _utcnow()
            row.status = target
            row.last_error = None
            if target == AGENT_STATUS_RUNNING:
                row.started_at = now
                row.stopped_at = None
            if target in (AGENT_STATUS_STOPPED, AGENT_STATUS_FAILED, AGENT_STATUS_PAUSED):
                row.stopped_at = now
            row.updated_at = now
            await db.commit()
            await db.refresh(row)
        await self._broadcast_state(user_id, "agent_state_changed", config_id, row.status)
        return {"ok": True, "idempotent": False, "config": config_to_dict(row)}

    async def start(self, config_id: str, user_id: str) -> dict[str, Any]:
        return await self._transition(
            config_id,
            user_id,
            allowed_from=(AGENT_STATUS_IDLE, AGENT_STATUS_STOPPED, AGENT_STATUS_FAILED, AGENT_STATUS_PAUSED),
            target=AGENT_STATUS_RUNNING,
            name="start",
            idempotent_when_target=True,
        )

    async def pause(self, config_id: str, user_id: str) -> dict[str, Any]:
        return await self._transition(
            config_id,
            user_id,
            allowed_from=(AGENT_STATUS_RUNNING,),
            target=AGENT_STATUS_PAUSED,
            name="pause",
            idempotent_when_target=False,
        )

    async def resume(self, config_id: str, user_id: str) -> dict[str, Any]:
        return await self._transition(
            config_id,
            user_id,
            allowed_from=(AGENT_STATUS_PAUSED,),
            target=AGENT_STATUS_RUNNING,
            name="resume",
            idempotent_when_target=True,
        )

    async def stop(self, config_id: str, user_id: str) -> dict[str, Any]:
        return await self._transition(
            config_id,
            user_id,
            allowed_from=(AGENT_STATUS_RUNNING, AGENT_STATUS_PAUSED, AGENT_STATUS_IDLE, AGENT_STATUS_FAILED),
            target=AGENT_STATUS_STOPPED,
            name="stop",
            idempotent_when_target=False,
        )

    # ── Tenant-scoped human approval (real task approval, owner-verified) ─
    async def approve_task(self, task_id: str, user_id: str) -> dict[str, Any]:
        async with SessionLocal() as db:
            task = await db.get(AgentTaskRecord, task_id)
            if task is None:
                raise AgentControlError(ERR_TASK_NOT_APPROVABLE, "task not found")
            # A tenant may only approve tasks their own agent produced.
            owned = str(task.requested_by or "") == user_id
            if not owned:
                raise AgentControlError(ERR_TASK_NOT_APPROVABLE, "task not found")
            if not task.requires_approval:
                raise AgentControlError(ERR_TASK_NOT_APPROVABLE, "task does not require approval")
        from app.engine.agent_runtime import agent_runtime_default

        outcome = await agent_runtime_default().approve_task(task_id, approved_by=user_id)
        await self._broadcast_state(user_id, "agent_approval", task_id)
        return outcome

    # ── Live-update notification (reuses the tenant-scoped /ws/events feed) ─
    async def _broadcast_state(
        self, user_id: str, event: str, ref_id: str, status: Optional[str] = None
    ) -> None:
        try:
            from app.market_data.manager import ws_manager

            await ws_manager.broadcast_user(
                "trades",
                user_id,
                {"event": event, "ref_id": ref_id, "status": status, "user_id": user_id},
            )
        except Exception:  # noqa: BLE001 - a lost socket must never fail the op
            logger.debug("agent state broadcast failed for user %s", user_id, exc_info=True)
# ── Console bundle: ONE request, everything the page needs ──────────
    async def bundle(self, user_id: str) -> dict[str, Any]:
        """Single tenant-scoped snapshot for the Agent Console (no N+1)."""
        from app.models.agent_intent import TradingIntentRecord
        from app.models.trading import OrderRecord, PositionRecord

        config = await self.get_config(user_id)

        async with SessionLocal() as db:
            cfg = await db.get(AgentRuntimeConfigRecord, 1)
            trading_agent = (
                await db.execute(
                    select(AgentRecord).where(AgentRecord.agent_type == "trading_agent")
                )
            ).scalar_one_or_none()
            autonomous_enabled = bool(cfg.autonomous_mode_enabled) if cfg else False
            global_level = int((cfg.global_autonomy_level or 0) if cfg else 0)
            agent_enabled = bool(trading_agent.enabled) if trading_agent else False
            agent_max = int(trading_agent.max_autonomy_level or 0) if trading_agent else 0

            from app.engine.agent_runtime import task_to_dict

            decisions_rows = list(
                (await db.execute(
                    select(AgentDecisionRecord)
                    .where(AgentDecisionRecord.user_id == user_id)
                    .order_by(AgentDecisionRecord.created_at.desc())
                    .limit(10)
                )).scalars().all()
            )
            tasks_rows = list(
                (await db.execute(
                    select(AgentTaskRecord)
                    .where(AgentTaskRecord.requested_by == user_id)
                    .order_by(AgentTaskRecord.created_at.desc())
                    .limit(10)
                )).scalars().all()
            )
            tasks = []
            for t in tasks_rows:
                td = task_to_dict(t)
                td["decision_ids"] = [r.id for r in decisions_rows if r.task_id == t.id]
                tasks.append(td)

            intents_rows = list(
                (await db.execute(
                    select(TradingIntentRecord)
                    .where(TradingIntentRecord.user_id == user_id)
                    .order_by(TradingIntentRecord.created_at.desc())
                    .limit(10)
                )).scalars().all()
            )
            intents = [
                {
                    "intent_id": it.id, "agent_task_id": it.agent_task_id,
                    "symbol": it.symbol, "side": it.side, "quantity": it.quantity,
                    "decision": it.decision, "status": it.status, "mode": it.requested_mode,
                    "order_id": it.order_id, "position_id": it.position_id,
                    "created_at": _iso(it.created_at),
                }
                for it in intents_rows
            ]

            orders_rows = list(
                (await db.execute(
                    select(OrderRecord)
                    .where(OrderRecord.user_id == user_id)
                    .order_by(OrderRecord.created_at.desc())
                    .limit(10)
                )).scalars().all()
            )
            orders = [
                {
                    "order_id": o.id, "symbol": o.symbol, "side": o.side,
                    "quantity": o.quantity, "status": o.status,
                    "filled_price": getattr(o, "filled_price", None),
                    "created_at": _iso(o.created_at),
                }
                for o in orders_rows
            ]

            positions_rows = list(
                (await db.execute(
                    select(PositionRecord)
                    .where(PositionRecord.user_id == user_id, PositionRecord.status == "OPEN")
                    .order_by(PositionRecord.opened_at.desc())
                    .limit(10)
                )).scalars().all()
            )
            positions = [
                {
                    "position_id": p.id, "symbol": p.symbol, "side": p.side,
                    "quantity": p.quantity, "entry_price": getattr(p, "entry_price", None),
                    "status": p.status,
                    "protection_state": p.protection_state,
                    "created_at": _iso(p.opened_at),
                }
                for p in positions_rows
            ]

        return {
            "config": config_to_dict(config) if config else None,
            "gate": {
                "autonomous_mode_enabled": autonomous_enabled,
                "global_autonomy_level": global_level,
                "trading_agent_enabled": agent_enabled,
                "trading_agent_max_autonomy": agent_max,
                "effective_autonomy_ceiling": min(global_level, agent_max)
                if autonomous_enabled else 0,
                "broker_mode": settings.broker_mode,
                "live_available": settings.broker_mode == "live",
            },
            "activity": {
                "decisions": [decision_to_dict(r) for r in decisions_rows],
                "tasks": tasks,
                "intents": intents,
                "orders": orders,
                "positions": positions,
            },
        }

    # ── Evaluation: bounded per-slot work, idempotent ───────────────────
    async def evaluate_due(self) -> int:
        """Scan RUNNING configs and enqueue evaluation tasks (bounded, idempotent).

        Returns the number of tasks enqueued.
        """
        from app.engine.agent_runtime import (
            CREATOR_USER,
            agent_runtime_default,
            ERR_INVALID_INPUT,
            ERR_INVALID_IDEMPOTENCY_KEY,
            ERR_UNSUPPORTED_TASK_KIND,
        )

        async with SessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(AgentConfigRecord).where(
                            AgentConfigRecord.status == AGENT_STATUS_RUNNING
                        )
                    )
                ).scalars().all()
            )

        enqueued = 0
        now = _utcnow()
        slot = int(now.timestamp() // max(1.0, settings.agent_evaluation_interval))
        for config_rec in rows:
            idem_key = f"cfg:{config_rec.id}:eval:{slot}"
            payload = {
                "config_id": config_rec.id,
                "user_id": config_rec.user_id,
                "strategy_id": config_rec.strategy_id,
                "symbols": _load(config_rec.symbols_json).get("symbols", [])
                if isinstance(_load(config_rec.symbols_json), dict) else [],
                "autonomy_level": int(config_rec.autonomy_level),
                "approval_policy": _load(config_rec.approval_policy_json),
                "risk_policy": _load(config_rec.risk_policy_json),
                "requested_mode": config_rec.execution_mode,
                "slot": slot,
            }
            try:
                runtime = agent_runtime_default()
                task_dict, created = await runtime.create_task(
                    agent_type="trading_agent",
                    task_kind="evaluate_market",
                    input_payload=payload,
                    created_by=CREATOR_USER,
                    requires_approval=False,
                    idempotency_key=idem_key,
                    timeout_seconds=60.0,
                    requested_by=config_rec.user_id,
                )
                if created:
                    enqueued += 1
            except Exception:  # noqa: BLE001 - per-config failure must not block others
                logger.warning(
                    "evaluate_due: enqueue failed for config %s", config_rec.id, exc_info=True
                )
        return enqueued

    # ── Link-back sweep: connect decisions to intents/orders (idempotent) ─
    async def link_back_sweep(self) -> int:
        """For recent decisions with execution_result=SENT and intent_id=NULL,
        look up the linked execute_trade task's output and update the decision
        row with the terminal intent/order linkage.

        Returns the number of decisions updated.
        """
        from app.engine.agent_runtime import task_to_dict

        cutoff = _utcnow() - timedelta(hours=24)
        updated = 0
        async with SessionLocal() as db:
            pending = list(
                (
                    await db.execute(
                        select(AgentDecisionRecord).where(
                            AgentDecisionRecord.execution_result == "SENT",
                            AgentDecisionRecord.intent_id.is_(None),
                            AgentDecisionRecord.created_at > cutoff,
                        ).limit(_LINK_BACK_BATCH)
                    )
                ).scalars().all()
            )
            for dec in pending:
                if not dec.task_id:
                    continue
                task_row = await db.get(AgentTaskRecord, dec.task_id)
                if task_row is None:
                    continue
                if task_row.status == "RUNNING":
                    continue  # still executing
                output = _load(task_row.output_json)
                error = _load(task_row.error_json)
                if task_row.status == "SUCCEEDED" and isinstance(output, dict):
                    dec.intent_id = output.get("intent_id") or output.get("order", {}).get("agent_intent_id")
                    dec.order_id = output.get("order_id")
                    dec.execution_result = "SUCCEEDED" if dec.intent_id else "NO_INTENT"
                elif task_row.status == "FAILED":
                    dec.execution_result = "FAILED"
                    dec.error_json = _dump(error) if error else dec.error_json
                else:
                    dec.execution_result = task_row.status or "UNKNOWN"
                dec.updated_at = _utcnow()
                updated += 1
            if updated:
                await db.commit()
        return updated

    # ── Decision generation (runs inside the evaluate_market handler) ────
    async def run_evaluation(
        self,
        *,
        config_id: str,
        user_id: str,
        strategy_id: Optional[str],
        symbols: list[str],
        autonomy_level: int,
        approval_policy: dict[str, Any],
        risk_policy: dict[str, Any],
        requested_mode: str,
        task_id: str,
    ) -> list[dict[str, Any]]:
        """Deterministic market-strategy evaluation for one agent config.

        NEVER reaches a broker: TRADE / NEEDS_APPROVAL only enqueue a governed
        ``execute_trade`` task (created_by=agent) gated by the runtime
        autonomy/approval rules and routed solely through
        ``AgentIntentTriggerBridge → AgentTradingService``.
        """
        from app.engine.agent_runtime import agent_runtime_default
        from app.models.trading import StrategyRecord

        async with SessionLocal() as db:
            config_row = await db.get(AgentConfigRecord, config_id)
            if config_row is None or str(config_row.user_id) != user_id:
                return [{"decision": DECISION_FAILED, "reason": "agent config not found"}]
            if config_row.status != AGENT_STATUS_RUNNING:
                return [{"decision": DECISION_NO_TRADE, "reason": f"agent is {config_row.status}"}]

        strategy = None
        if strategy_id:
            async with SessionLocal() as db:
                srow = await db.get(StrategyRecord, strategy_id)
                if srow is not None and (srow.user_id is None or str(srow.user_id) == user_id):
                    strategy = {
                        "id": srow.id,
                        "action_json": srow.action_json,
                        "conditions_json": srow.conditions_json,
                    }

        # ONE shared, bounded evaluator (rolling indicator history across slots)
        # — mirrors the persistent evaluator the TradingEngine keeps alive.
        evaluator = get_shared_evaluator()
        decisions: list[dict[str, Any]] = []
        max_position = int(risk_policy.get("max_position_size", settings.max_position_size))
        rate_budget = int(risk_policy.get("max_orders_per_minute", settings.max_orders_per_minute))
        max_open_positions = int(risk_policy.get("max_open_positions", 1))
        open_symbols = await self._open_position_symbols(user_id, symbols)

        for symbol in symbols:
            row = AgentDecisionRecord(
                agent_config_id=config_id,
                user_id=user_id,
                task_id=task_id,
                strategy_id=strategy_id,
                decision=DECISION_NO_TRADE,
                symbol=symbol,
                mode=requested_mode or MODE_PAPER,
                reason="",
                risk_result="OK",
                approval_result="NOT_REQUIRED",
                execution_result="NOT_ATTEMPTED",
            )
            decision_row = await self._decide_symbol(
                row, symbol, strategy, evaluator, autonomy_level,
                approval_policy, max_position,
                open_symbols=open_symbols,
                max_open_positions=max_open_positions,
                orders_this_window=await self._recent_action_count(config_id, symbol),
                max_orders_per_minute=rate_budget,
            )
            async with SessionLocal() as db:
                db.add(decision_row)
                await db.commit()
                await db.refresh(decision_row)

            decisions.append(decision_to_dict(decision_row))
            if decision_row.decision in (DECISION_TRADE, DECISION_NEEDS_APPROVAL):
                await self._enqueue_execute_trade(
                    config_id=config_id,
                    user_id=user_id,
                    strategy_id=strategy_id,
                    decision_row=decision_row,
                    requested_mode=requested_mode,
                    approval_required=decision_row.decision == DECISION_NEEDS_APPROVAL,
                )
            await self._broadcast_state(
                user_id, "agent_decision", decision_row.id, status=decision_row.decision
            )
        return decisions

    async def _open_position_symbols(self, user_id: str, symbols: list[str]) -> set[str]:
        """Symbols (upper-cased) with an OPEN position owned by this tenant.

        Feeds the loop-safety ``max_open_positions`` guard so a still-matching
        strategy can never pile a second entry onto a symbol it already holds.
        """
        from app.models.trading import PositionRecord

        if not symbols:
            return set()
        syms = [str(s).strip().upper() for s in symbols]
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(PositionRecord.symbol).where(
                        PositionRecord.user_id == user_id,
                        PositionRecord.status == "OPEN",
                        PositionRecord.symbol.in_(syms),
                    )
                )
            ).scalars().all()
        return {str(s).upper() for s in rows}

    async def _recent_action_count(self, config_id: str, symbol: str) -> int:
        """TRADE/NEEDS_APPROVAL decisions persisted for (config, symbol) in the
        last minute — the per-config action-window budget.

        The decision being evaluated is NOT yet committed, so the first N
        qualifying slots are allowed and the (N+1)-th is blocked deterministically.
        """
        cutoff = _utcnow() - timedelta(seconds=60)
        async with SessionLocal() as db:
            value = (
                await db.execute(
                    select(func.count())
                    .select_from(AgentDecisionRecord)
                    .where(
                        AgentDecisionRecord.agent_config_id == config_id,
                        AgentDecisionRecord.symbol == symbol,
                        AgentDecisionRecord.decision.in_(
                            (DECISION_TRADE, DECISION_NEEDS_APPROVAL)
                        ),
                        AgentDecisionRecord.created_at >= cutoff,
                    )
                )
            ).scalar_one()
        return int(value or 0)

    async def _decide_symbol(
        self,
        row: AgentDecisionRecord,
        symbol: str,
        strategy: Optional[dict[str, Any]],
        evaluator: Any,
        autonomy_level: int,
        approval_policy: dict[str, Any],
        max_position: int,
        *,
        open_symbols: Optional[set[str]] = None,
        max_open_positions: int = 1,
        orders_this_window: int = 0,
        max_orders_per_minute: int = 30,
    ) -> AgentDecisionRecord:
        """Deterministic decision for ONE symbol (fail-closed; never guesses)."""
        from app.market_data.unified_manager import unified_market_manager

        quote = unified_market_manager.get_quote(symbol)
        price: Optional[float] = None
        if quote is not None and isinstance(quote, dict):
            for key in ("price", "ltp", "last_price", "close", "last"):
                try:
                    value = float(quote.get(key))
                    if value > 0:
                        price = value
                        break
                except (TypeError, ValueError):
                    continue
        if price is None:
            row.decision = DECISION_NO_TRADE
            row.reason = "no market quote available (feed unavailable)"
            row.risk_result = "FEED_UNAVAILABLE"
            row.risk_reason = "market data provider returned no fresh quote"
            return row

        # Fail-closed feed-state gate: a decision may ONLY be derived from a
        # quote whose data_status is genuinely usable (LIVE / DELAYED within its
        # freshness window / explicitly DEMO).  STALE/UNAVAILABLE cached prices
        # never produce a TRADE decision here — the intent boundary would reject
        # them later, but the durable decision row must stay honest.
        status = str(quote.get("data_status") or "UNKNOWN").upper()
        is_stale = quote.get("is_stale")
        age = quote.get("age_seconds")
        if status in ("STALE", "UNAVAILABLE") or is_stale is True:
            row.decision = DECISION_NO_TRADE
            row.reason = (
                f"market feed {status} (age={age}); autonomous decisions "
                "require fresh market data"
            )
            row.risk_result = (
                "FEED_STALE"
                if (status == "STALE" or is_stale is True)
                else "FEED_UNAVAILABLE"
            )
            row.risk_reason = f"feed data_status={status} is_stale={is_stale} age={age}"
            return row
        if status not in ("LIVE", "DELAYED", "DEMO"):
            row.decision = DECISION_NO_TRADE
            row.reason = f"market feed in unknown state {status!r}; decision fail-closed"
            row.risk_result = "FEED_UNKNOWN"
            row.risk_reason = f"feed data_status={status}"
            return row

        evaluator.update_price(symbol, price)
        matched = False
        note = "strategy conditions not met"
        if strategy is not None:
            conditions = _load(strategy["conditions_json"]) or []
            if isinstance(conditions, dict):
                conditions = conditions.get("conditions", [])
            if conditions:
                matched = evaluator.evaluate(
                    strategy["id"] or row.agent_config_id or "", symbol, conditions
                )
                if matched:
                    note = "strategy conditions matched"
        if not matched:
            if strategy is None:
                note = "no strategy bound (observe-only evaluation)"
            row.decision = DECISION_NO_TRADE
            row.reason = note
            return row

        action = _load(strategy.get("action_json") or {}) or {}
        if not isinstance(action, dict) or not action.get("side") or not action.get("quantity"):
            row.decision = DECISION_NO_TRADE
            row.reason = f"{note}; strategy action is incomplete"
            row.risk_result = "INVALID_ACTION"
            return row

        if autonomy_level == 0:
            row.decision = DECISION_NO_TRADE
            row.reason = f"{note}; observe-only autonomy (level 0)"
            return row

        side = _side_for_action(action)
        # Loop-safety guards (bounded autonomous action rate — never slot-rate-
        # proportional order creation): a still-matching strategy may not open a
        # second position on a symbol it already holds, and may not exceed the
        # operator's per-config order window (max_orders_per_minute).
        if (
            side == "BUY"
            and max_open_positions > 0
            and str(symbol).strip().upper() in (open_symbols or set())
        ):
            row.decision = DECISION_NO_TRADE
            row.reason = (
                f"{note}; open position already held for {symbol} "
                f"(max_open_positions={max_open_positions})"
            )
            row.risk_result = "OPEN_POSITION_LIMIT"
            return row
        if max_orders_per_minute > 0 and orders_this_window >= max_orders_per_minute:
            row.decision = DECISION_NO_TRADE
            row.reason = (
                f"{note}; per-config order window exhausted "
                f"({orders_this_window}/{max_orders_per_minute} in the last minute)"
            )
            row.risk_result = "RATE_LIMIT"
            return row

        quantity = max(
            1,
            min(int(action.get("quantity", 1)), max_position, settings.agent_max_intent_quantity),
        )
        approval_required = bool(approval_policy.get("approval_required", True)) or autonomy_level == 1
        row.decision = DECISION_NEEDS_APPROVAL if approval_required else DECISION_TRADE
        row.side = side
        row.quantity = quantity
        row.reason = note
        row.approval_result = "REQUIRED" if approval_required else "NOT_REQUIRED"
        row.execution_result = "SENT"
        return row

    async def _enqueue_execute_trade(
        self,
        *,
        config_id: str,
        user_id: str,
        strategy_id: Optional[str],
        decision_row: AgentDecisionRecord,
        requested_mode: str,
        approval_required: bool,
    ) -> None:
        """Enqueue the governed execute_trade task (created_by=agent so the
        runtime autonomy/approval gates apply at claim time)."""
        from app.engine.agent_intent_triggers import (
            AGENT_TYPE_TRADING,
            TASK_KIND_EXECUTE_TRADE,
        )
        from app.engine.agent_runtime import (
            CREATOR_AGENT,
            AgentDispatchError,
            agent_runtime_default,
        )
        from app.models.trading import StrategyRecord

        action: dict[str, Any] = {}
        if strategy_id:
            async with SessionLocal() as db:
                srow = await db.get(StrategyRecord, strategy_id)
                if srow is not None:
                    action = _load(srow.action_json) or {}

        payload = self._execute_payload(
            config_id=config_id,
            user_id=user_id,
            strategy_id=strategy_id,
            decision=decision_row,
            action=action,
            requested_mode=requested_mode,
            approval_required=approval_required,
        )
        try:
            task_dict, _created = await agent_runtime_default().create_task(
                agent_type=AGENT_TYPE_TRADING,
                task_kind=TASK_KIND_EXECUTE_TRADE,
                input_payload=payload,
                created_by=CREATOR_AGENT,
                requires_approval=approval_required,
                idempotency_key=f"cfg:{config_id}:dec:{decision_row.id}",
                timeout_seconds=settings.agent_task_max_timeout_seconds,
                requested_by=user_id,
            )
        except AgentDispatchError as exc:
            async with SessionLocal() as db:
                linked = await db.get(AgentDecisionRecord, decision_row.id)
                if linked is not None:
                    linked.execution_result = "BLOCKED"
                    linked.error_json = _dump({"code": exc.code, "message": exc.message})
                    linked.updated_at = _utcnow()
                    await db.commit()
            return
        async with SessionLocal() as db:
            linked = await db.get(AgentDecisionRecord, decision_row.id)
            if linked is not None:
                linked.task_id = task_dict["id"]
                linked.updated_at = _utcnow()
                await db.commit()

    @staticmethod
    def _execute_payload(
        *,
        config_id: str,
        user_id: str,
        strategy_id: Optional[str],
        decision: AgentDecisionRecord,
        action: dict[str, Any],
        requested_mode: str,
        approval_required: bool,
    ) -> dict[str, Any]:
        """Structured governed execute_trade envelope (never free-form AI)."""
        from app.engine.agent_intents import (
            DECISION_NEEDS_APPROVAL as _D_NA,
            DECISION_TRADE as _D_T,
        )

        return {
            "trigger_source": "scheduler",
            "user_id": user_id,
            "broker_account_id": str(action.get("broker_account_id") or ""),
            "strategy_id": strategy_id or "",
            "symbol": decision.symbol,
            "side": decision.side or "BUY",
            "quantity": int(decision.quantity or 1),
            "order_type": str(action.get("order_type", "MARKET")).upper(),
            "limit_price": action.get("limit_price"),
            "trigger_price": action.get("trigger_price"),
            "stop_loss_price": action.get("stop_loss_price") or action.get("stop_loss"),
            "take_profit_price": action.get("take_profit_price") or action.get("take_profit"),
            "confidence": float(action.get("confidence", 0.0) or 0.0),
            "reason": decision.reason or "",
            "requested_mode": requested_mode or MODE_PAPER,
            "decision": _D_NA if approval_required else _D_T,
            "approval_required": approval_required,
        }

AGENT_TYPE_TRADING = "trading_agent"
TASK_KIND_EVALUATE_MARKET = "evaluate_market"


def handler_for(task_kind: str):
    from app.engine.agent_runtime import handler_spec

    return handler_spec(AGENT_TYPE_TRADING, task_kind)


def _side_for_action(action: Any) -> str:
    """Canonical intent side from a strategy action."""
    raw = str(action.get("side", "BUY") or "BUY").upper() if isinstance(action, dict) else "BUY"
    return "SELL" if raw.startswith("SELL") else "BUY"


# ── Runtime handler: trading_agent / evaluate_market ───────────────────────
def _register_handlers() -> None:
    """Register the evaluate_market runtime handler (idempotent, test-safe).

    Importing ``app.engine.agent_intent_triggers`` also performs its module-level
    side-effect: the ``trading_agent`` AgentDefinition (single source of truth)
    is registered with the runtime.  The runtime's ``validate_registry()``
    startup check requires every handler to have a definition, so both must be
    present no matter which module is imported first.
    """
    from app.engine.agent_intent_triggers import (  # noqa: F401
        AGENT_TYPE_TRADING,
        TASK_KIND_EXECUTE_TRADE,
    )
    from app.engine.agent_runtime import (
        CAP_ANALYZE,
        AgentContext,
        AgentTaskFailure,
        register_handler,
    )

    if handler_for(TASK_KIND_EVALUATE_MARKET) is not None:
        return

    @register_handler(
        agent_type=AGENT_TYPE_TRADING,
        task_kind=TASK_KIND_EVALUATE_MARKET,
        required_capability=CAP_ANALYZE,
    )
    async def _handle_evaluate_market(ctx: AgentContext) -> dict[str, Any]:
        """Evaluate one RUNNING agent config against its strategy + universe.

        created_by=user (the owner's agent runs interactively); the handler only
        records deterministic decisions and MAY enqueue a governed execute_trade
        task.  It never reaches a broker on its own.
        """
        payload = dict(ctx.input or {})
        try:
            decisions = await agent_control_service.run_evaluation(
                config_id=str(payload.get("config_id") or ""),
                user_id=str(payload.get("user_id") or ""),
                strategy_id=str(payload.get("strategy_id") or "") or None,
                symbols=list(payload.get("symbols") or []),
                autonomy_level=int(payload.get("autonomy_level") or 0),
                approval_policy=dict(payload.get("approval_policy") or {}),
                risk_policy=dict(payload.get("risk_policy") or {}),
                requested_mode=str(payload.get("requested_mode") or MODE_PAPER),
                task_id=str(ctx.task_id),
            )
        except Exception as exc:  # noqa: BLE001 - boundary; record deterministically
            raise AgentTaskFailure(
                ERR_EVALUATION_FAILED, str(exc), retryable=False,
                details={"config_id": payload.get("config_id")},
            ) from exc
        return {"evaluated": len(decisions), "decisions": decisions}


_register_handlers()


# ── Bounded background evaluation scheduler ────────────────────────────────
class AgentEvaluationScheduler:
    """Periodically turns RUNNING configs into evaluation tasks (bounded)."""

    def __init__(
        self,
        service: Optional[AgentControlService] = None,
        interval_seconds: Optional[float] = None,
    ) -> None:
        self.service = service if service is not None else agent_control_service
        self.interval_seconds = float(
            interval_seconds
            if interval_seconds is not None
            else settings.agent_evaluation_interval
        )
        self._task = None
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            logger.info("[AgentControl] evaluation scheduler already running; start() ignored")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="agent-evaluation-loop")
        logger.info("[AgentControl] evaluation scheduler started (every %.1fs)", self.interval_seconds)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        logger.info("[AgentControl] evaluation scheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.service.evaluate_due()
                await self.service.link_back_sweep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad pass must not kill the loop
                logger.warning("[AgentControl] evaluation pass failed", exc_info=True)
            await asyncio.sleep(self.interval_seconds)


#: Process-wide singleton (mirrors agent_runtime_default / agent_trading_service).
agent_control_service = AgentControlService()


def agent_control_scheduler_default() -> AgentEvaluationScheduler:
    return AgentEvaluationScheduler(service=agent_control_service)