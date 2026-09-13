"""Governed trigger → intent bridge.

Scheduler events and webhook signals are EXTERNAL triggers.  They may never
call a broker adapter, an ``OrderManager`` or a ``place_*`` helper directly:
every triggered trade request first becomes a durable ``AgentTaskRecord``
(``trading_agent`` / ``execute_trade``) and the runtime handler for that task
turns it into a durable ``TradingIntentRecord`` through the SINGLE governed
execution path (``app.engine.agent_intents.AgentTradingService``) — the only
component that ever reaches the broker.

Fail-closed trigger contract
----------------------------
* Execution modes: only ``PAPER`` / ``DEMO`` / ``LIVE`` are accepted at the
  trigger boundary; ``DEMO`` normalizes to ``PAPER`` (canonical mode, never
  persisted).  Anything else → ``unsupported_execution_mode`` and the trigger
  is rejected BEFORE any task/intent is persisted.  ``LIVE`` is never
  downgraded to ``PAPER``.
* Entry order types: only ``MARKET`` / ``LIMIT``.  The protective
  ``SL`` / ``SL-M`` (and ``STOP_LOSS`` / ``STOP_LOSS_LIMIT``) concepts are NOT
  entry order types → ``unsupported_order_type``, never persisted.
* Ownership: server-derived only — an explicit internal owner or the strategy
  owner resolved from ``strategies.name`` → ``user_id`` + ``broker_account_id``
  + execution mode.  An unresolved owner fails closed.  A webhook payload can
  never name a broker or account.
* Durability / idempotency: the deterministic envelope-derived trigger key is
  the task ``idempotency_key``; the agent task id is the unique anchor on
  ``trading_intents.agent_task_id``; the intent
  ``CREATED→SENT_FOR_EXECUTION`` CAS plus the per-user order claim converge
  re-deliveries / retries / restarts on exactly one intent and exactly one
  broker dispatch.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import select

from app.core.audit import log_audit_event
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.engine.agent_intents import (
    DECISION_NEEDS_APPROVAL,
    DECISION_TRADE,
    INTENT_ORDER_TYPES,
    MODE_ALIASES,
    MODE_PAPER,
    PERSISTED_MODES,
    UNSUPPORTED_INTENT_ORDER_TYPES,
    AgentTradingService,
    IntentGateError,
    agent_trading_service,
)
from app.engine.agent_runtime import (
    CAP_ANALYZE,
    CAP_EXECUTE,
    CAP_READ,
    CAP_WRITE,
    CREATOR_USER,
    AgentContext,
    AgentDefinition,
    AgentTaskFailure,
    agent_runtime_default,
    register_agent,
    register_handler,
)
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import StrategyRecord

if TYPE_CHECKING:
    from app.webhooks.queue.redis_streams import QueuedWebhook

logger = get_logger("engine.agent_intent_triggers")

# ── Trigger vocabulary ────────────────────────────────────────────────
TRIGGER_WEBHOOK = "webhook"
TRIGGER_SCHEDULER = "scheduler"
TRIGGER_SOURCES = (TRIGGER_WEBHOOK, TRIGGER_SCHEDULER)

#: Agent identity for scheduler-driven order execution.  Migration 0010 seeds
#: the matching ``trading_agent`` DB row (``enabled=False`` ⇒ operator opt-in);
#: ``ensure_agent_registry`` provisions it when missing.  The DB row may only
#: TIGHTEN this code-declared envelope (capability intersection, readiness).
AGENT_TYPE_TRADING = "trading_agent"
TASK_KIND_EXECUTE_TRADE = "execute_trade"

# Deterministic error codes surfaced through task ``error_json`` / audit.
ERR_UNSUPPORTED_MODE = "unsupported_execution_mode"
ERR_UNSUPPORTED_ORDER_TYPE = "unsupported_order_type"
ERR_INVALID_TRIGGER = "invalid_trigger"
ERR_OWNER_UNRESOLVED = "owner_unresolved"

# Audit action vocabulary (mirrors app.engine.agent_intents conventions).
_AUDIT_TRIGGER_ACCEPTED = "agent.trigger.accepted"
_AUDIT_TRIGGER_REJECTED = "agent.trigger.rejected"


class TriggerIntentError(AgentTaskFailure):
    """Deterministic, non-retryable trigger rejection (fail closed).

    Raised inside the scheduler handler it surfaces as an ``AgentTaskFailure``
    stored in the task ``error_json``; raised from
    :meth:`AgentIntentTriggerBridge.submit_webhook_signal` it propagates to the
    webhook worker, which MUST NOT acknowledge the signal.
    """
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _side_for_action(action: str) -> str:
    """Map a TradeThrone action to the canonical intent side (BUY | SELL)."""
    action_upper = (action or "").strip().upper()
    if action_upper in ("BUY", "BUY_TO_OPEN", "BUY_TO_CLOSE"):
        return "BUY"
    return "SELL"


def _envelope_ts_sec(envelope: Any) -> int:
    """Coarse per-second identity of a webhook delivery.

    Derived from the ENVELOPE timestamp — stable for re-deliveries — rather
    than the processing wall-clock, so a re-delivery collapses onto the same
    durable trigger key (never a double-dispatch source).
    """
    ts = getattr(envelope, "timestamp", None)
    if ts is None:
        import time
        return int(time.time())
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp())


def webhook_trigger_key(
    *,
    provider: str,
    strategy_name: Optional[str],
    symbol: str,
    side: str,
    quantity: int,
    ts_sec: int,
    signal: str,
) -> str:
    """Deterministic, tenant-scoped trigger key (task idempotency_key).

    Stable for duplicate deliveries of the same TradeThrone signal; distinct
    signals always diverge.  Fits the agent-task idempotency_key length budget.
    """
    material = "|".join([
        str(provider or ""),
        str(strategy_name or ""),
        str(symbol or ""),
        str(side or ""),
        str(int(quantity or 0)),
        str(int(ts_sec or 0)),
        str(signal or ""),
    ])
    return "ttr-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def normalize_trigger_mode(requested_mode: Optional[str]) -> str:
    """Fail-closed canonicalization of a trigger execution mode.

    ``DEMO`` normalizes to ``PAPER`` (canonical contract, never persisted).
    Unknown modes are rejected deterministically — never guessed, never
    silently upgraded to LIVE, never persisted.
    """
    mode = (requested_mode or "").strip().upper()
    if mode in MODE_ALIASES:
        return MODE_ALIASES[mode]
    if mode in PERSISTED_MODES:
        return mode
    raise TriggerIntentError(
        ERR_UNSUPPORTED_MODE,
        f"unsupported execution mode {requested_mode!r}; allowed: "
        f"{sorted(set(MODE_ALIASES) | set(PERSISTED_MODES))}",
    )


def normalize_trigger_order_type(order_type: Optional[str]) -> str:
    """Fail-closed canonicalization of a trigger entry order type.

    ``MARKET`` / ``LIMIT`` are the only supported entry order types.
    Protective concepts (``SL``, ``SL-M``, ``STOP_LOSS``, ``STOP_LOSS_LIMIT``)
    fail closed here (before any task/intent is persisted).
    """
    raw = (order_type or "").strip().upper()
    if raw in UNSUPPORTED_INTENT_ORDER_TYPES or raw in ("SL", "SL-M", "SLM"):
        raise TriggerIntentError(
            ERR_UNSUPPORTED_ORDER_TYPE,
            f"order_type {raw!r} is a protective order concept, not an entry "
            "order type; express it via trigger_price/stop_loss_price (entry "
            f"order types: {sorted(INTENT_ORDER_TYPES)})",
        )
    if raw in INTENT_ORDER_TYPES:
        return raw
    raise TriggerIntentError(
        ERR_UNSUPPORTED_ORDER_TYPE,
        f"order_type {order_type!r} is not supported for entry dispatch; "
        f"allowed: {sorted(INTENT_ORDER_TYPES)}",
    )
async def _resolve_owner(
    *,
    strategy_name: Optional[str] = None,
    user_id: Optional[str] = None,
    broker_account_id: Optional[str] = None,
    requested_mode: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve the server-side owner for a triggered trade.

    Ownership is NEVER trusted from the trigger payload: it is derived from
    the strategy (``strategies.name`` → owner user + routed broker account +
    execution mode) or from an explicit internal caller identity.  Any
    unresolved or inconsistent owner fails closed with ``owner_unresolved``.
    """
    strategy_row = None
    async with SessionLocal() as db:
        if strategy_name and strategy_name.strip():
            result = await db.execute(
                select(StrategyRecord)
                .where(StrategyRecord.name == strategy_name.strip())
                .order_by(
                    StrategyRecord.created_at.desc(), StrategyRecord.id.desc()
                )
                .limit(1)
            )
            strategy_row = result.scalars().first()

        owner_user_id = user_id
        if strategy_row is not None and not owner_user_id:
            owner_user_id = (
                str(strategy_row.user_id) if strategy_row.user_id else None
            )
        if not owner_user_id:
            raise TriggerIntentError(
                ERR_OWNER_UNRESOLVED,
                f"no owner can be resolved for strategy {strategy_name!r}",
            )

        account_id = broker_account_id
        if account_id is None and strategy_row is not None:
            account_id = (
                str(strategy_row.broker_account_id)
                if strategy_row.broker_account_id
                else None
            )
        if account_id is None:
            account = (
                await db.execute(
                    select(BrokerAccountRecord)
                    .where(
                        BrokerAccountRecord.user_id == owner_user_id,
                        BrokerAccountRecord.is_active.is_(True),
                    )
                    .order_by(
                        BrokerAccountRecord.linked_at.asc(),
                        BrokerAccountRecord.id.asc(),
                    )
                    .limit(1)
                )
            ).scalars().first()
            if account is not None:
                account_id = str(account.id)
        if not account_id:
            raise TriggerIntentError(
                ERR_OWNER_UNRESOLVED,
                f"no active broker account for user {owner_user_id}",
            )

        # Execution mode: caller override > strategy mode > PAPER (never None).
        mode = (requested_mode or "").strip().upper()
        if not mode and strategy_row is not None and strategy_row.execution_mode:
            mode = strategy_row.execution_mode.upper()
        mode = normalize_trigger_mode(mode)

        return {
            "user_id": owner_user_id,
            "broker_account_id": account_id,
            "strategy_id": (
                str(strategy_row.id) if strategy_row is not None else None
            ),
            "requested_mode": mode,
        }
class AgentIntentTriggerBridge:
    """Governed trigger→intent bridge.

    Public surface used by every external entry point that wants to place a
    governed trade:

    * ``submit_webhook_signal`` — TradeThrone webhook enqueue (durable task,
      never a broker call).
    * ``trigger_intent`` / ``handle_scheduler_event`` — durable intent
      creation + governed execution (the ONLY path that reaches a broker).

    The bridge never imports a broker adapter and never calls ``place_*``.
    """

    def __init__(self, service: Optional[AgentTradingService] = None) -> None:
        self.service = service if service is not None else agent_trading_service

    # ── Webhook trigger: validate → normalize → resolve owner → enqueue ───
    async def submit_webhook_signal(
        self,
        webhook: "QueuedWebhook",
        *,
        user_id: Optional[str] = None,
        broker_account_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Durable, governed acceptance of a TradeThrone webhook signal.

        Normalizes the payload, resolves the owner server-side and enqueues a
        ``trading_agent`` / ``execute_trade`` task keyed on the deterministic
        envelope-derived trigger key.  NEVER dispatches to a broker.

        Raises :class:`TriggerIntentError` on fail-closed rejection
        (unsupported mode/order type, unavailable owner, invalid payload) —
        the caller MUST NOT acknowledge the signal in that case.
        """
        from app.webhooks.validation.schemas import TradeThronePayload

        envelope = webhook.envelope
        raw_payload = dict(envelope.payload or {})
        try:
            payload = TradeThronePayload(**raw_payload)
        except Exception as exc:
            raise TriggerIntentError(
                ERR_INVALID_TRIGGER,
                f"invalid TradeThrone payload: {exc}",
                details={"provider": envelope.provider},
            ) from exc

        # Symbol canonicalization + lot-size compliance (server-side; never
        # trust the payload for identity or risk approval).
        from app.compliance.lot_sizes import resolve_symbol, validate_quantity

        canonical_symbol, _exchange = resolve_symbol(payload.symbol)
        corrected = validate_quantity(
            canonical_symbol, int(payload.quantity), auto_correct=True
        ).corrected_quantity
        if corrected is None or int(corrected) <= 0:
            raise TriggerIntentError(
                ERR_INVALID_TRIGGER,
                "quantity must be a positive integer after lot-size correction",
            )
        quantity = int(corrected)

        side = _side_for_action(payload.action)
        order_type = normalize_trigger_order_type(payload.order_type)
        owner = await _resolve_owner(
            strategy_name=payload.strategy_name,
            user_id=user_id,
            broker_account_id=broker_account_id,
        )
        trigger_key = webhook_trigger_key(
            provider=envelope.provider,
            strategy_name=payload.strategy_name,
            symbol=canonical_symbol,
            side=side,
            quantity=quantity,
            ts_sec=_envelope_ts_sec(envelope),
            signal=payload.signal,
        )

        # Enqueue the durable task — the scheduler bridge handler later
        # creates + executes the intent through the service.  NEVER a broker.
        runtime = agent_runtime_default()
        task, created = await runtime.create_task(
            agent_type=AGENT_TYPE_TRADING,
            task_kind=TASK_KIND_EXECUTE_TRADE,
            input_payload={
                "trigger_source": TRIGGER_WEBHOOK,
                "trigger_key": trigger_key,
                "user_id": owner["user_id"],
                "broker_account_id": owner["broker_account_id"],
                "strategy_id": owner["strategy_id"],
                "strategy_name": payload.strategy_name,
                "symbol": canonical_symbol,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "limit_price": payload.price,
                "trigger_price": payload.trigger_price,
                "reason": f"tradethrone:{payload.signal}",
                "requested_mode": owner["requested_mode"],
                "decision": DECISION_TRADE,
                "approval_required": False,
            },
            created_by=CREATOR_USER,
            requested_by=owner["user_id"],
            idempotency_key=trigger_key,
        )
        task_id = str((task or {}).get("id") or "")
        async with SessionLocal() as db:
            await log_audit_event(
                db,
                _AUDIT_TRIGGER_ACCEPTED,
                resource_type="agent_task",
                user_id=str(owner["user_id"] or None),
                resource_id=task_id,
                details={
                    "source": TRIGGER_WEBHOOK,
                    "trigger_key": trigger_key,
                    "created": bool(created),
                    "symbol": canonical_symbol,
                    "side": side,
                    "quantity": quantity,
                    "order_type": order_type,
                    "requested_mode": owner["requested_mode"],
                    "strategy_name": payload.strategy_name,
                },
            )
        logger.info(
            "webhook trigger accepted: key=%s task=%s created=%s symbol=%s "
            "side=%s qty=%d mode=%s",
            trigger_key, task_id, created, canonical_symbol, side, quantity,
            owner["requested_mode"],
        )
        return {
            "source": TRIGGER_WEBHOOK,
            "trigger_key": trigger_key,
            "agent_task_id": task_id,
            "created": bool(created),
        }
# ── Governed intent trigger (scheduler path) ─────────────────────────
    async def trigger_intent(
        self,
        *,
        trigger_id: str,
        source: str,
        user_id: str,
        broker_account_id: str,
        strategy_id: Optional[str],
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        limit_price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        confidence: Optional[float] = None,
        reason: str = "",
        requested_mode: str = MODE_PAPER,
        decision: str = DECISION_TRADE,
        approval_required: bool = False,
        execute: bool = True,
    ) -> dict[str, Any]:
        """Create a durable intent (idempotent on trigger_id) and execute it.

        Single governed path from a trigger to a broker dispatch.  The intent
        anchors on ``trigger_id`` (the agent-task id), so racing workers /
        retries / restarts converge on exactly one intent row and the
        ``CREATED→SENT_FOR_EXECUTION`` CAS guarantees one broker dispatch.
        """
        if source not in TRIGGER_SOURCES:
            raise TriggerIntentError(
                ERR_INVALID_TRIGGER, f"unknown trigger source {source!r}"
            )
        mode = normalize_trigger_mode(requested_mode)
        otype = normalize_trigger_order_type(order_type)
        if not symbol or not symbol.strip():
            raise TriggerIntentError(ERR_INVALID_TRIGGER, "symbol is required")
        if side not in ("BUY", "SELL"):
            raise TriggerIntentError(
                ERR_INVALID_TRIGGER, f"side must be BUY or SELL, got {side}"
            )
        if quantity is None or int(quantity) <= 0:
            raise TriggerIntentError(
                ERR_INVALID_TRIGGER, "quantity must be a positive integer"
            )

        try:
            result = await self.service.evaluate(
                agent_task_id=trigger_id,
                user_id=user_id,
                broker_account_id=broker_account_id,
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                quantity=int(quantity),
                order_type=otype,
                limit_price=limit_price,
                trigger_price=trigger_price,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                confidence=confidence,
                reason=reason,
                requested_mode=mode,
                decision=decision,
                approval_required=bool(approval_required),
            )
        except IntentGateError as exc:
            # Fail closed: an intent that fails service gates must never be
            # acknowledged; surface deterministically as a trigger rejection.
            raise TriggerIntentError(
                exc.code, exc.message, retryable=False, details=exc.details
            ) from exc

        intent_id = result.get("intent_id")
        if not intent_id or not execute:
            return {
                "intent_id": intent_id,
                "decision": result.get("decision"),
                "status": result.get("status"),
                "reason": result.get("reason"),
                "mode": mode,
                "executed": False,
                "order_id": None,
            }

        outcome = await self.service.execute_intent(intent_id)
        return {
            "intent_id": intent_id,
            "decision": result.get("decision"),
            "status": result.get("status"),
            "mode": mode,
            "executed": (
                bool(outcome.get("ok")) if isinstance(outcome, dict) else False
            ),
            "order_id": (
                outcome.get("order_id") if isinstance(outcome, dict) else None
            ),
        }

    async def handle_scheduler_event(self, ctx: AgentContext) -> dict[str, Any]:
        """Registered ``trading_agent`` / ``execute_trade`` handler.

        Invoked by the ``AgentRuntimeScheduler`` when it drains an accepted
        trigger task.  Canonicalizes the payload once more (fail closed), then
        creates + executes the durable intent through the governed service.
        """
        payload = dict(ctx.input or {})
        try:
            approval_required = bool(payload.get("approval_required"))
            return await self.trigger_intent(
                trigger_id=str(ctx.task_id),
                source=str(payload.get("trigger_source") or TRIGGER_SCHEDULER),
                user_id=str(payload.get("user_id") or ""),
                broker_account_id=str(payload.get("broker_account_id") or ""),
                strategy_id=str(payload.get("strategy_id") or "") or None,
                symbol=str(payload.get("symbol") or ""),
                side=str(payload.get("side") or "SELL"),
                quantity=int(payload.get("quantity") or 0),
                order_type=str(payload.get("order_type") or ORDER_TYPE_MARKET),
                limit_price=payload.get("limit_price"),
                trigger_price=payload.get("trigger_price"),
                stop_loss_price=payload.get("stop_loss_price"),
                take_profit_price=payload.get("take_profit_price"),
                confidence=payload.get("confidence"),
                reason=str(payload.get("reason") or ""),
                requested_mode=str(payload.get("requested_mode") or MODE_PAPER),
                decision=(
                    DECISION_NEEDS_APPROVAL
                    if approval_required
                    else str(payload.get("decision") or DECISION_TRADE)
                ),
                approval_required=approval_required,
                execute=not approval_required,
            )
        except TriggerIntentError as exc:
            raise AgentTaskFailure(
                exc.code, exc.message, retryable=False, details=exc.details
            ) from exc
# ── Code-declared trading agent (operator opt-in; DB row may tighten) ──
_TRADING_AGENT = AgentDefinition(
    agent_type=AGENT_TYPE_TRADING,
    name="Trading Agent",
    description=(
        "Governed bridge between external triggers (webhook signals, scheduler "
        "events) and broker execution. Creates durable trade intents through "
        "AgentTradingService; never bypasses risk/margin/approval gates."
    ),
    capabilities=(CAP_READ, CAP_ANALYZE, CAP_WRITE, CAP_EXECUTE),
    readonly=False,
    max_autonomy_level=1,
    enabled_by_default=False,
)
register_agent(_TRADING_AGENT)


@register_handler(
    agent_type=AGENT_TYPE_TRADING,
    task_kind=TASK_KIND_EXECUTE_TRADE,
    required_capability=CAP_EXECUTE,
)
async def _handle_execute_trade(ctx: AgentContext) -> dict[str, Any]:
    """Runtime handler: governed ``execute_trade`` task → intent → dispatch."""
    return await agent_intent_trigger_bridge.handle_scheduler_event(ctx)


#: Process-wide singleton.  ``submit_webhook_signal`` and the scheduler-draining
#: handler both route through this instance.
agent_intent_trigger_bridge = AgentIntentTriggerBridge()