"""Phase 1 Step 3: governed agent → trading-intent service.

This module is the ONE bridge between the autonomous-agent runtime and the
existing TradeThrone execution pipeline.  An agent NEVER calls a broker
directly: it produces a structured :class:`TradingIntentRecord` (durable,
migration 0010) which this service routes through the SAME canonical
primitives every other execution path uses — NO second order engine:

  feed freshness     ``unified_market_manager.get_quote()`` (honest
                     data_status; STALE/UNAVAILABLE fail closed)
  broker mode        ``assert_live_dispatch_allowed()`` (BROKER_MODE=live) —
                     LIVE never silently falls back to PAPER/DEMO
  broker truth       ``broker_state_sync_engine`` LIVE-fresh snapshot gate
                     (only for requested_mode=LIVE)
  margin             ``compute_margin_required`` + broker snapshot / paper
                     balance (PAPER floor)
  risk               the engine's own ``RiskManager`` (kill switch, circuit
                     breaker, position limits, rate limits)
  idempotency        the shared ``durable_claims`` kernel (per-user claimed
                     ``client_order_id``) so retries/restarts/concurrent
                     workers can never double-place an order
  execution          ``get_broker_adapter(...)`` + ``place_order`` + the
                     2-phase ``finalize_order_claim`` (Trade + Position)
  protective orders  Phase 15C ``protection_engine`` (LIVE broker-side; PAPER
                     keeps engine-simulated SL/TP with protection_state=PAPER)
  paper accounting   ``credit_paper_pnl`` (P1-1 owner-scoped)
  journal/audit      canonical ``audit_logs`` with the full chain
                     agent_task → intent → order → position → protection

Deterministic decision contract (Section 7, persisted, never free-form):
  DECISION_NO_TRADE / DECISION_TRADE / DECISION_NEEDS_APPROVAL /
  DECISION_REJECTED / DECISION_FAILED
An intent row is created ONLY for TRADE / NEEDS_APPROVAL — the other decisions
are recorded in the task output and the audit trail.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, select, update

from app.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.engine.durable_claims import (
    claim_order_record,
    fetch_claim_by_key,
    finalize_order_claim,
    reject_order_claim,
)
from app.engine.risk_manager import RiskManager
from app.models.agent_intent import TradingIntentRecord
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord
from app.schemas.trading import OrderRequest, Side

logger = get_logger("engine.agent_intents")

# ── Decision contract (Section 7) ───────────────────────────────────────────
DECISION_NO_TRADE = "NO_TRADE"
DECISION_TRADE = "TRADE"
DECISION_NEEDS_APPROVAL = "NEEDS_APPROVAL"
DECISION_REJECTED = "REJECTED"
DECISION_FAILED = "FAILED"

# ── Intent lifecycle statuses (migration 0010 CHECK) ────────────────────────
INTENT_CREATED = "CREATED"
INTENT_SENT = "SENT_FOR_EXECUTION"
INTENT_EXECUTED = "EXECUTED"
INTENT_REJECTED = "REJECTED"
INTENT_FAILED = "FAILED"
INTENT_CLOSED = "CLOSED"

# Audit action vocabulary (canonical audit_logs trail).
_AUDIT_DECISION_REJECTED = "agent.intent.decision_rejected"
_AUDIT_DECISION_NO_TRADE = "agent.intent.decision_no_trade"
_AUDIT_DECISION_FAILED = "agent.intent.decision_failed"
_AUDIT_CREATED = "agent.intent.created"
_AUDIT_SENT = "agent.intent.sent_for_execution"
_AUDIT_EXECUTED = "agent.intent.executed"
_AUDIT_REJECTED = "agent.intent.rejected"
_AUDIT_CLOSE_DEFERRED = "agent.intent.close_deferred"
_AUDIT_CLOSED = "agent.intent.closed"


class IntentGateError(Exception):
    """Deterministic fail-closed gate rejection (persisted, never silent)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_intent_id() -> str:
    """Deterministic intent id: ``int_`` + 32 hex chars (index-safe)."""
    return "int_" + uuid.uuid4().hex


def intent_order_key(intent_id: str) -> str:
    """Stable per-user idempotency key derived from the intent.

    ``agent-`` + sha256(intent_id)[:40] fits the ``[A-Za-z0-9._-]{8,64}``
    charset and the per-user partial unique index on orders.client_order_id.
    """
    return "agent-" + hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:40]
def intent_to_dict(rec: TradingIntentRecord) -> dict[str, Any]:
    """Deterministic ISO-8601/UTC serialization for API consumers."""

    def _iso(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        ts = value
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat(timespec="seconds")

    def _load(text: Optional[str]) -> Any:
        if not text:
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    return {
        "id": rec.id,
        "agent_task_id": rec.agent_task_id,
        "user_id": rec.user_id,
        "strategy_id": rec.strategy_id,
        "broker_account_id": rec.broker_account_id,
        "symbol": rec.symbol,
        "side": rec.side,
        "quantity": rec.quantity,
        "order_type": rec.order_type,
        "limit_price": rec.limit_price,
        "trigger_price": rec.trigger_price,
        "stop_loss_price": rec.stop_loss_price,
        "take_profit_price": rec.take_profit_price,
        "confidence": rec.confidence,
        "reason": rec.reason,
        "decision": rec.decision,
        "status": rec.status,
        "requested_mode": rec.requested_mode,
        "approval_required": bool(rec.approval_required),
        "approved_by": rec.approved_by,
        "approved_at": _iso(rec.approved_at),
        "market_data_status": rec.market_data_status,
        "market_data_age_seconds": rec.market_data_age_seconds,
        "risk_status": rec.risk_status,
        "risk_reason": rec.risk_reason,
        "margin_required": rec.margin_required,
        "execution_reason": rec.execution_reason,
        "order_id": rec.order_id,
        "position_id": rec.position_id,
        "protective_order_id": rec.protective_order_id,
        "error": _load(rec.error_json),
        "created_at": _iso(rec.created_at),
        "updated_at": _iso(rec.updated_at),
    }


def _quote_price(quote: Optional[dict[str, Any]]) -> Optional[float]:
    if not quote:
        return None
    try:
        raw = quote.get("price") or quote.get("last") or quote.get("last_price")
        if raw is None:
            return None
        price = float(raw)
        return round(price, 4) if price > 0 else None
    except (TypeError, ValueError):
        return None


def _quote_fresh(
    quote: Optional[dict[str, Any]],
) -> tuple[bool, str, Optional[float], Optional[float]]:
    """Fail-closed feed check. Returns (fresh, data_status, age_seconds, price).

    Only LIVE (or genuinely-sourced DELAYED within its freshness window, or an
    explicitly DEMO/SIMULATED feed) may feed an autonomous action; STALE /
    UNAVAILABLE / UNKNOWN / unparsable ages all fail closed.
    """
    if quote is None:
        return False, "UNAVAILABLE", None, None
    status = quote.get("data_status") or "UNKNOWN"
    is_stale = quote.get("is_stale")
    age = quote.get("age_seconds")
    price = _quote_price(quote)
    if status in ("STALE", "UNAVAILABLE") or is_stale is True:
        return False, status, age, price
    if status in ("LIVE", "DELAYED", "DEMO"):
        if price is None or price <= 0:
            return False, status, age, None
        return True, status, age, price
    return False, status, age, price


_engine_risk_getter: Optional[Any] = None


def set_engine_risk_getter(getter: Any) -> None:
    """Wire the process-global RiskManager source (the runtime / tests)."""
    global _engine_risk_getter
    _engine_risk_getter = getter


_fallback_risk_manager = RiskManager()


def _engine_risk_manager() -> Optional[RiskManager]:
    """Resolve the process-global RiskManager (the single shared risk source)."""
    if _engine_risk_getter is not None:
        try:
            rm = _engine_risk_getter()
            if rm is not None:
                return rm
        except Exception as exc:  # defensive; the gate must never raise
            logger.debug("engine risk getter failed: %s", exc)
    try:
        from app.main import get_engine

        eng = get_engine()
        if eng is not None:
            rm = getattr(eng, "risk_manager", None) or getattr(eng, "_risk", None)
            if rm is None:
                om = getattr(eng, "order_manager", None)
                if om is not None:
                    rm = getattr(om, "risk_manager", None)
            if rm is not None:
                return rm
    except Exception as exc:  # noqa: BLE001 - gate must never raise
        logger.debug("engine risk manager unavailable: %s", exc)
    return None


def _risk_check(order: OrderRequest) -> tuple[bool, str]:
    """Consult the engine's own RiskManager (kill switch / circuit breaker /
    position limits / rate limits)."""
    rm = _engine_risk_manager() or _fallback_risk_manager
    try:
        return rm.check(order)
    except Exception as exc:  # noqa: BLE001 - fail closed on risk errors
        return False, f"risk gate unavailable: {exc}"


def _margin_required_for(symbol: str, quantity: int, price: float) -> float:
    """Reuse the canonical DMA margin model (fail-closed => full notional)."""
    try:
        from app.api.dma_engine import classify_asset, compute_margin_required

        product = "CNC" if classify_asset(symbol, "CNC") == "EQUITY_CNC" else "MIS"
        margin, _asset = compute_margin_required(symbol, product, quantity, price)
        return round(float(margin), 2)
    except Exception:  # defensive default: full notional (fail closed)
        return round(float(quantity) * float(price), 2)
async def _write_audit(
    action: str,
    *,
    user_id: Optional[str],
    resource_type: str,
    resource_id: Optional[str],
    details: Optional[dict[str, Any]],
) -> None:
    """Append an immutable event to the canonical audit_logs trail."""
    try:
        from app.core.audit import log_audit_event

        async with SessionLocal() as db:
            await log_audit_event(
                db=db,
                action=action,
                resource_type=resource_type,
                user_id=user_id,
                resource_id=resource_id,
                status="SUCCESS",
                details=details,
            )
    except Exception as exc:  # never let audit failures break execution
        logger.error("audit write failed for %s: %s", action, exc)


def _intent_payload(intent: TradingIntentRecord) -> dict[str, Any]:
    """Extract column values for durable inserts (never raw __dict__)."""
    cols = {
        "id", "agent_task_id", "user_id", "broker_account_id", "strategy_id",
        "symbol", "side", "quantity", "order_type", "limit_price",
        "trigger_price", "stop_loss_price", "take_profit_price", "confidence",
        "reason", "decision", "status", "requested_mode", "approval_required",
        "approved_by", "approved_at", "market_data_status",
        "market_data_age_seconds", "risk_status", "risk_reason",
        "margin_required", "execution_reason", "order_id", "position_id",
        "protective_order_id", "error_json", "created_at", "updated_at",
    }
    return {name: getattr(intent, name) for name in cols}

async def _insert_intent(
    intent: TradingIntentRecord,
) -> tuple[str, bool]:
    """Atomic insert; returns (intent_id, created_flag).

    The ``agent_task_id`` partial unique constraint makes collision detection
    deterministic: a concurrent worker that wins the race yields
    (existing_id, False).
    """
    from sqlalchemy.exc import IntegrityError

    payload = _intent_payload(intent)
    async with SessionLocal() as db:
        try:
            db.add(TradingIntentRecord(**payload))
            await db.commit()
            return intent.id, True
        except IntegrityError:
            await db.rollback()
            row = (
                await db.execute(
                    select(TradingIntentRecord).where(
                        TradingIntentRecord.agent_task_id == intent.agent_task_id
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                return row.id, False
            raise


async def _persist_intent(intent_id: str, fields: dict[str, Any]) -> None:
    """Durably update an intent row (bounded fields).

    Rejection reasons and gate outcomes are applied synchronously so the
    persisted record is always honest before a response is returned.
    """
    async with SessionLocal() as db:
        rec = await db.get(TradingIntentRecord, intent_id)
        if rec is None:
            return
        for key, value in fields.items():
            if value is not None or key == "execution_reason":
                setattr(rec, key, value)
        rec.updated_at = _utcnow()
        await db.commit()


class AgentTradingService:
    """Governed bridge: agent intent → canonical execution pipeline.

    Thin service — owns NO order-engine logic.  Every step delegates to an
    existing primitive (durable_claims kernel, RiskManager, broker adapter,
    protection_engine, credit_paper_pnl, audit_logs / journal).
    """

    def __init__(self) -> None:
        self.max_intent_quantity = int(settings.agent_max_intent_quantity)
        self.max_approval_hold_seconds = int(
            settings.agent_approval_max_hold_seconds
        )

    # ── evaluate(): deterministic decision → durable intent ───────────────
    async def evaluate(
        self,
        *,
        agent_task_id: Optional[str],
        user_id: Optional[str],
        broker_account_id: Optional[str],
        strategy_id: Optional[str],
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        limit_price: Optional[float],
        trigger_price: Optional[float],
        stop_loss_price: Optional[float],
        take_profit_price: Optional[float],
        confidence: Optional[float],
        reason: str,
        requested_mode: str,
        decision: str,
        approval_required: bool,
    ) -> dict[str, Any]:
        """Run every gate and persist a durable intent for TRADE /
        NEEDS_APPROVAL decisions.  All other decisions are journaled and
        return ``intent_id=None``.  NEVER raises on gate rejection."""
        # 1. Schema/ownership gates — never trust the agent payload.
        await self._validate_payload(
            user_id=user_id,
            broker_account_id=broker_account_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            requested_mode=requested_mode,
        )

        # 2. Fresh market data (fail-closed): STALE/UNAVAILABLE → NO_TRADE.
        fresh, mkt_status, mkt_age, mkt_price = await self._checked_market_price(
            symbol
        )
        if not fresh:
            await _write_audit(
                _AUDIT_DECISION_NO_TRADE,
                user_id=user_id,
                resource_type="agent_intent",
                resource_id=agent_task_id,
                details={
                    "agent_task_id": agent_task_id,
                    "symbol": symbol,
                    "reason": f"market data {mkt_status} (age={mkt_age})",
                    "decision": DECISION_NO_TRADE,
                },
            )
            return {
                "intent_id": None,
                "decision": DECISION_NO_TRADE,
                "status": None,
                "reason": f"market data {mkt_status} (age={mkt_age})",
            }
        price = mkt_price
        order = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=int(quantity),
            order_type=(
                "MARKET" if decision == DECISION_NEEDS_APPROVAL else order_type
            ),
            price=limit_price,
            trigger_price=trigger_price,
        )

        # 3. Delivery gate: LIVE dispatch capability + LIVE-fresh broker
        #    state snapshot (only for requested_mode=LIVE).  LIVE NEVER
        #    silently falls back to PAPER/DEMO.
        if requested_mode == "LIVE":
            live_reason = await self._live_dispatch_gate(broker_account_id)
            if live_reason is not None:
                await _write_audit(
                    _AUDIT_DECISION_REJECTED,
                    user_id=user_id,
                    resource_type="agent_intent",
                    resource_id=agent_task_id,
                    details={
                        "agent_task_id": agent_task_id,
                        "symbol": symbol,
                        "reason": f"live dispatch disallowed: {live_reason}",
                        "decision": DECISION_REJECTED,
                    },
                )
                return {
                    "intent_id": None,
                    "decision": DECISION_REJECTED,
                    "status": None,
                    "reason": f"live dispatch disallowed: {live_reason}",
                }

        # 4. Risk gate — the engine's own RiskManager (single shared source):
        #    kill switch, circuit breaker, position limits, rate limits.
        risk_ok, risk_reason = _risk_check(order)

        # 5. Margin gate — DMA canonical model.  LIVE uses the broker
        #    snapshot as the authority; PAPER uses the scaled paper balance.
        margin_required = _margin_required_for(symbol, int(quantity), price)
        margin_reason: Optional[str] = None
        if requested_mode == "LIVE":
            margin_reason = await self._require_live_margin(
                broker_account_id, symbol, int(quantity), price
            )
        elif requested_mode == "PAPER":
            if not await self._paper_has_margin(user_id, margin_required):
                margin_reason = "insufficient paper balance"

        if not risk_ok:
            await _write_audit(
                _AUDIT_DECISION_REJECTED,
                user_id=user_id,
                resource_type="agent_intent",
                resource_id=agent_task_id,
                details={
                    "agent_task_id": agent_task_id,
                    "symbol": symbol,
                    "reason": f"risk gate rejected: {risk_reason}",
                    "decision": DECISION_REJECTED,
                },
            )
            return {
                "intent_id": None,
                "decision": DECISION_REJECTED,
                "status": None,
                "reason": f"risk gate rejected: {risk_reason}",
            }
        if margin_reason is not None:
            await _write_audit(
                _AUDIT_DECISION_REJECTED,
                user_id=user_id,
                resource_type="agent_intent",
                resource_id=agent_task_id,
                details={
                    "agent_task_id": agent_task_id,
                    "symbol": symbol,
                    "reason": f"margin gate rejected: {margin_reason}",
                    "decision": DECISION_REJECTED,
                },
            )
            return {
                "intent_id": None,
                "decision": DECISION_REJECTED,
                "status": None,
                "reason": f"margin gate rejected: {margin_reason}",
            }

        # 6. Deterministic decision contract.  Only TRADE / NEEDS_APPROVAL
        #    produce a durable intent; everything else is journaled.
        if decision in (DECISION_NO_TRADE, DECISION_REJECTED):
            await _write_audit(
                _AUDIT_DECISION_REJECTED
                if decision == DECISION_REJECTED
                else _AUDIT_DECISION_NO_TRADE,
                user_id=user_id,
                resource_type="agent_intent",
                resource_id=agent_task_id,
                details={
                    "agent_task_id": agent_task_id,
                    "symbol": symbol,
                    "reason": reason or f"agent decision {decision}",
                    "decision": decision,
                },
            )
            return {
                "intent_id": None,
                "decision": decision,
                "status": None,
                "reason": reason or f"agent decision {decision}",
            }
        if decision not in (DECISION_TRADE, DECISION_NEEDS_APPROVAL):
            await _write_audit(
                _AUDIT_DECISION_FAILED,
                user_id=user_id,
                resource_type="agent_intent",
                resource_id=agent_task_id,
                details={
                    "agent_task_id": agent_task_id,
                    "symbol": symbol,
                    "reason": f"invalid decision value {decision}",
                    "decision": DECISION_FAILED,
                },
            )
            return {
                "intent_id": None,
                "decision": DECISION_FAILED,
                "status": None,
                "reason": f"invalid decision value {decision}",
            }

        # 7. Persist the durable intent — one per agent_task (atomic INSERT
        #    so racing workers converge on a single row; idempotent result).
        now = _utcnow()
        intent = TradingIntentRecord(
            id=_make_intent_id(),
            agent_task_id=agent_task_id,
            user_id=user_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            quantity=int(quantity),
            order_type=(
                "MARKET" if decision == DECISION_NEEDS_APPROVAL else order_type
            ),
            limit_price=limit_price if decision == DECISION_TRADE else None,
            trigger_price=trigger_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            confidence=confidence,
            reason=reason,
            decision=decision,
            status=INTENT_CREATED,
            requested_mode=requested_mode,
            approval_required=bool(approval_required),
            approved_by=None,
            approved_at=None,
            market_data_status=mkt_status,
            market_data_age_seconds=mkt_age,
            risk_status="PASSED",
            risk_reason=risk_reason or None,
            margin_required=margin_required,
            execution_reason=None,
            order_id=None,
            position_id=None,
            protective_order_id=None,
            error_json=None,
            created_at=now,
            updated_at=now,
        )
        intent_id, created = await _insert_intent(intent)
        await _write_audit(
            _AUDIT_CREATED if created else _AUDIT_DECISION_REJECTED,
            user_id=user_id,
            resource_type="agent_intent",
            resource_id=intent_id,
            details={
                "agent_task_id": agent_task_id,
                "symbol": symbol,
                "decision": decision,
                "created": created,
                "reason": (
                    "duplicate task intent returned (idempotent)"
                    if not created
                    else reason
                ),
            },
        )
        return {
            "intent_id": intent_id,
            "decision": decision,
            "status": INTENT_CREATED,
            "reason": reason,
        }

    async def _validate_payload(
        self,
        *,
        user_id: Optional[str],
        broker_account_id: Optional[str],
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        requested_mode: str,
    ) -> None:
        """Schema/ownership gates — fail closed, never trust the agent."""
        if not symbol or not symbol.strip():
            raise IntentGateError("symbol_required", "symbol is required")
        if side not in ("BUY", "SELL"):
            raise IntentGateError(
                "invalid_side", f"side must be BUY or SELL, got {side}"
            )
        if order_type not in ("MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_LIMIT"):
            raise IntentGateError(
                "invalid_order_type",
                f"order_type must be MARKET/LIMIT/STOP_LOSS/STOP_LOSS_LIMIT, got {order_type}",
            )
        if requested_mode not in ("PAPER", "DEMO", "LIVE"):
            raise IntentGateError(
                "invalid_mode",
                f"requested_mode must be PAPER/DEMO/LIVE, got {requested_mode}",
            )
        if not user_id:
            raise IntentGateError("user_required", "user_id is required")
        if not broker_account_id:
            raise IntentGateError("broker_required", "broker_account_id is required")
        if quantity is None or int(quantity) <= 0:
            raise IntentGateError(
                "quantity_required", "quantity must be a positive integer"
            )
        if int(quantity) > self.max_intent_quantity:
            raise IntentGateError(
                "quantity_limit",
                f"quantity {quantity} exceeds max {self.max_intent_quantity}",
            )
        async with SessionLocal() as db:
            acc = await db.get(BrokerAccountRecord, broker_account_id)
            if acc is None:
                raise IntentGateError(
                    "broker_unknown",
                    f"broker account {broker_account_id} not found",
                )
            if acc.user_id != user_id:
                raise IntentGateError(
                    "broker_not_owner",
                    "broker account does not belong to the calling user",
                )

    async def _checked_market_price(self, symbol: str) -> tuple[bool, str, Optional[float], Optional[float]]:
        """Fresh-price gate (fail closed on STALE/UNAVAILABLE)."""
        from app.market_data.unified_manager import unified_market_manager

        quote = unified_market_manager.get_quote(symbol)  # synchronous cache read
        return _quote_fresh(quote)

    async def _live_dispatch_gate(
        self, broker_account_id: Optional[str]
    ) -> Optional[str]:
        """assert_live_dispatch_allowed + LIVE-fresh broker snapshot gate.

        Returns None when the gate passes, else a human-readable reason.
        """
        live_reason = None
        try:
            from app.brokers import assert_live_dispatch_allowed

            assert_live_dispatch_allowed()
        except RuntimeError as exc:
            # BrokerModeBlockedError is a RuntimeError subclass.
            live_reason = str(exc)
        if live_reason is not None:
            return live_reason
        try:
            from app.engine.broker_state_sync import broker_state_sync_engine

            snap = await broker_state_sync_engine.get_snapshot(
                broker_account_id
            )
            if snap is None or snap.updated_at is None:
                return "broker state snapshot missing"
            age = (_utcnow() - snap.updated_at).total_seconds()
            max_age = float(settings.broker_snapshot_max_age_seconds)
            if age > max_age:
                return f"broker state snapshot stale ({age:.0f}s > {max_age}s)"
        except RuntimeError:
            return "strategy-to-broker binding missing"
        except Exception as exc:  # noqa: BLE001 - fail closed
            return f"broker state check failed: {exc}"
        return None

    async def _require_live_margin(
        self,
        broker_account_id: Optional[str],
        symbol: str,
        quantity: int,
        price: float,
    ) -> Optional[str]:
        """LIVE margin gate — broker snapshot is the authority (DMA model)."""
        required = _margin_required_for(symbol, quantity, price)
        try:
            from app.engine.broker_state_sync import broker_state_sync_engine

            snap = await broker_state_sync_engine.get_snapshot(
                broker_account_id
            )
        except Exception as exc:  # noqa: BLE001 - fail closed
            return f"broker snapshot unavailable for margin: {exc}"
        available = float(snap.available_margin or 0.0) if snap else 0.0
        if required > available:
            return (
                f"margin required {required:.2f} exceeds available {available:.2f}"
            )
        return None

    async def _paper_has_margin(
        self, user_id: Optional[str], required: float
    ) -> bool:
        """PAPER margin gate — canonical owner balance vs DMA margin required.

        The invariant ``paper_balance == 1_000_000 + sum(realized_pnl)`` makes
        the stored float the single source of truth (P1-1 owner-scoped).
        """
        if not user_id:
            return False
        from app.engine.paper_account import PAPER_STARTING_BALANCE

        try:
            async with SessionLocal() as db:
                user = await db.get(UserRecord, user_id)
                if user is None:
                    return False
                raw = getattr(user, "paper_balance", None)
                balance = (
                    float(raw) if raw is not None else PAPER_STARTING_BALANCE
                )
                return balance >= float(required)
        except Exception:  # noqa: BLE001 - fail closed
            return False

    async def _gate_decision(
        self,
        *,
        required: bool,
        approved_by: Optional[str],
        approved_at: Optional[datetime],
        intent_created_at: Optional[datetime],
        decision: str,
    ) -> Optional[str]:
        """Approval gate — needed for DECISION_NEEDS_APPROVAL intents.

        Returns None when the gate passes, else the rejection reason.
        """
        if not required:
            return None
        if decision != DECISION_NEEDS_APPROVAL:
            return None
        if approved_by is None or approved_at is None:
            return "approval required but not yet granted"
        if intent_created_at is not None:
            # SQLite returns naive datetimes; treat them as UTC so the
            # approval-hold arithmetic is always timezone-correct.
            created = intent_created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            hold = (_utcnow() - created).total_seconds()
            if hold > self.max_approval_hold_seconds:
                return (
                    f"approval expired ({hold:.0f}s hold > "
                    f"{self.max_approval_hold_seconds}s)"
                )
        return None

    # ── execute_intent: CREATED → SENT → claim → gates → dispatch ─────────
    async def execute_intent(
        self,
        intent_id: str,
        *,
        authorized_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Governed execution of a durable intent (idempotent, fail-closed).

        Concurrency model: the ``CREATED→SENT_FOR_EXECUTION`` CAS is the sole
        entry.  A concurrent worker that loses the race never reaches the
        broker.  The per-user ``client_order_id`` durable claim then guards
        replay across process restarts and duplicate deliveries.
        """
        async with SessionLocal() as db:
            row = await db.get(TradingIntentRecord, intent_id)
            if row is None:
                return {"ok": False, "reason": f"intent {intent_id} not found"}
            snapshot = intent_to_dict(row)

        if row.status not in (INTENT_CREATED, INTENT_SENT):
            # Already executed / rejected / failed — replay the durable state
            # exactly; never dispatch a second time.
            return {
                "ok": True,
                "idempotent": True,
                "reason": f"already {row.status}",
                "intent": snapshot,
            }

        # CAS transition CREATED → SENT_FOR_EXECUTION (single winner).
        async with SessionLocal() as db:
            claimed = await db.execute(
                update(TradingIntentRecord)
                .where(
                    TradingIntentRecord.id == intent_id,
                    TradingIntentRecord.status == INTENT_CREATED,
                )
                .values(status=INTENT_SENT, updated_at=_utcnow())
            )
            await db.commit()
            if claimed.rowcount != 1:
                async with SessionLocal() as db2:
                    row2 = await db2.get(TradingIntentRecord, intent_id)
                return {
                    "ok": True,
                    "idempotent": True,
                    "reason": "already in flight (concurrent executor won)",
                    "intent": intent_to_dict(row2) if row2 else snapshot,
                }
        await _write_audit(
            _AUDIT_SENT,
            user_id=row.user_id,
            resource_type="agent_intent",
            resource_id=intent_id,
            details={"symbol": row.symbol, "side": row.side},
        )

        # ── Re-run every gate at execution time (fresh price, delivery
        #    capability, broker truth, approval, risk, margin) ──────────────
        book_mode = "LIVE" if row.requested_mode == "LIVE" else "PAPER"

        fresh, mkt_status, mkt_age, mkt_price = await self._checked_market_price(
            row.symbol
        )
        if not fresh:
            return await self._mark_rejected(
                row, f"market data {mkt_status} at execution (age={mkt_age})"
            )
        price = float(row.limit_price or mkt_price or 0.0)

        if book_mode == "LIVE":
            live_reason = await self._live_dispatch_gate(row.broker_account_id)
            if live_reason is not None:
                return await self._mark_rejected(
                    row, f"live dispatch disallowed: {live_reason}"
                )
            margin_reason = await self._require_live_margin(
                row.broker_account_id, row.symbol, int(row.quantity), price
            )
            if margin_reason is not None:
                return await self._mark_rejected(row, f"margin: {margin_reason}")
        else:
            margin_required = _margin_required_for(
                row.symbol, int(row.quantity), price
            )
            if not await self._paper_has_margin(row.user_id, margin_required):
                return await self._mark_rejected(row, "insufficient paper balance")

        approval_reason = await self._gate_decision(
            required=bool(row.approval_required),
            approved_by=row.approved_by,
            approved_at=row.approved_at,
            intent_created_at=row.created_at,
            decision=row.decision or DECISION_TRADE,
        )
        if approval_reason is not None:
            return await self._mark_rejected(row, approval_reason)

        risk_order = OrderRequest(
            symbol=row.symbol,
            side=Side(row.side),
            quantity=int(row.quantity),
            order_type=row.order_type or "MARKET",
            price=price,
            trigger_price=row.trigger_price,
        )
        risk_ok, risk_reason = _risk_check(risk_order)
        if not risk_ok:
            return await self._mark_rejected(row, f"risk gate: {risk_reason}")

        # All gates passed — dispatch through the canonical pipeline.
        return await self._dispatch(
            row,
            book_mode=book_mode,
            execution_price=price,
            authorized_user_id=authorized_user_id,
        )
    async def _mark_rejected(
        self, row: TradingIntentRecord, reason: str
    ) -> dict[str, Any]:
        """Fail-closed gate rejection — persisted + audited, never silent."""
        await _persist_intent(
            row.id,
            {
                "status": INTENT_REJECTED,
                "execution_reason": reason,
                "error_json": json.dumps({"stage": "gate", "reason": reason}),
            },
        )
        await _write_audit(
            _AUDIT_REJECTED,
            user_id=row.user_id,
            resource_type="agent_intent",
            resource_id=row.id,
            details={"reason": reason, "status": INTENT_REJECTED},
        )
        return {
            "ok": False,
            "intent_id": row.id,
            "reason": reason,
            "intent": intent_to_dict(row),
        }

    async def _mark_failed(
        self, row: TradingIntentRecord, reason: str
    ) -> dict[str, Any]:
        """Dispatch-stage failure — the durable claim was already rejected."""
        await _persist_intent(
            row.id,
            {
                "status": INTENT_FAILED,
                "execution_reason": reason,
                "error_json": json.dumps({"stage": "dispatch", "reason": reason}),
            },
        )
        await _write_audit(
            _AUDIT_REJECTED,
            user_id=row.user_id,
            resource_type="agent_intent",
            resource_id=row.id,
            details={"reason": reason, "status": INTENT_FAILED},
        )
        return {"ok": False, "intent_id": row.id, "reason": reason}


    async def _dispatch(
        self,
        row: TradingIntentRecord,
        *,
        book_mode: str,
        execution_price: float,
        authorized_user_id: Optional[str],
    ) -> dict[str, Any]:
        """Canonical dispatch: durable claim → broker → finalize → protect.

        The protective engine authorizes by position ownership, so
        ``authorized_user_id`` is intentionally not forwarded.
        """
        del authorized_user_id
        client_order_id = intent_order_key(row.id)
        key_predicate = and_(
            OrderRecord.user_id == row.user_id,
            OrderRecord.client_order_id == client_order_id,
        )
        claim_values: dict[str, Any] = {
            "user_id": row.user_id,
            "client_order_id": client_order_id,
            "broker_account_id": row.broker_account_id,
            "strategy_id": row.strategy_id,
            "agent_intent_id": row.id,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": int(row.quantity),
            "price": execution_price,
            "order_type": row.order_type or "MARKET",
            "mode": book_mode,
        }
        claim_id = await claim_order_record(
            key_predicate=key_predicate,
            claim_values=claim_values,
        )
        if claim_id is None:
            # Already FILLED / in-flight / previously rejected for this key.
            existing = await fetch_claim_by_key(key_predicate)
            if existing is not None and existing.status == "FILLED":
                await _persist_intent(
                    row.id,
                    {
                        "status": INTENT_EXECUTED,
                        "order_id": existing.id,
                        "position_id": existing.position_id,
                        "execution_reason": None,
                    },
                )
                return {
                    "ok": True,
                    "idempotent": True,
                    "reason": "already executed (claim FILLED)",
                    "intent_id": row.id,
                    "order_id": existing.id,
                    "position_id": existing.position_id,
                }
            if existing is not None and existing.status == "PENDING":
                return {
                    "ok": True,
                    "idempotent": True,
                    "reason": "claim in flight (PENDING)",
                    "intent_id": row.id,
                    "order_id": existing.id,
                }
            return {
                "ok": False,
                "intent_id": row.id,
                "reason": (
                    f"claim blocked (duplicate key, "
                    f"status={existing.status if existing else 'unknown'})"
                ),
            }

        # Broker dispatch (guarded by BROKER_MODE inside the adapters).
        broker_acc: Optional[BrokerAccountRecord] = None
        if row.broker_account_id:
            async with SessionLocal() as db:
                broker_acc = await db.get(BrokerAccountRecord, row.broker_account_id)
        from app.brokers import get_broker_adapter

        adapter = get_broker_adapter(broker_acc)
        order = OrderRequest(
            symbol=row.symbol,
            side=Side(row.side),
            quantity=int(row.quantity),
            order_type=row.order_type or "MARKET",
            price=(execution_price if row.limit_price else None),
            trigger_price=row.trigger_price,
            strategy_id=row.strategy_id,
        )
        fill_result: dict[str, Any]
        try:
            fill_result = await adapter.place_order(order)
        except Exception as exc:  # noqa: BLE001 - broker failures are channeled
            await reject_order_claim(
                claim_id, f"agent intent dispatch failed: {exc}"
            )
            return await self._mark_failed(row, f"broker dispatch failed: {exc}")

        filled_price = float(
            fill_result.get("filled_price")
            or fill_result.get("price")
            or execution_price
            or 0.0
        )
        broker_order_id = (
            fill_result.get("broker_order_id")
            or fill_result.get("order_id")
            or f"AGENT_{row.id[:8]}"
        )
        try:
            finalized = await finalize_order_claim(
                claim_id=claim_id,
                strategy_id=row.strategy_id,
                strategy_name="AgentIntent",
                broker_order_id=str(broker_order_id),
                symbol=row.symbol,
                side=row.side,
                quantity=int(row.quantity),
                filled_price=filled_price,
                user_id=row.user_id,
                broker_account_id=row.broker_account_id,
                mode=book_mode,
                create_position=True,
            )
        except Exception as exc:  # noqa: BLE001
            return await self._mark_failed(row, f"finalize failed: {exc}")

        # Resolve the created position and apply the intent's SL/TP targets.
        position_id: Optional[str] = None
        async with SessionLocal() as db:
            order_rec = await db.get(OrderRecord, claim_id)
            if order_rec is not None:
                position_id = order_rec.position_id
                if position_id:
                    pos = await db.get(PositionRecord, position_id)
                    if pos is not None:
                        pos.strategy_id = row.strategy_id
                        pos.stop_loss_price = row.stop_loss_price
                        pos.take_profit_price = row.take_profit_price
                        from app.engine.protective_orders import (
                            PROTECTION_STATE_PAPER,
                            PROTECTION_STATE_PENDING,
                        )

                        if book_mode == "LIVE" and (
                            row.stop_loss_price or row.take_profit_price
                        ):
                            pos.protection_state = PROTECTION_STATE_PENDING
                        elif book_mode == "PAPER" and (
                            row.stop_loss_price or row.take_profit_price
                        ):
                            pos.protection_state = PROTECTION_STATE_PAPER
                        await db.commit()
                        del PROTECTION_STATE_PAPER, PROTECTION_STATE_PENDING

        # Phase 15C exchange-level protection (LIVE only; PAPER keeps the
        # engine-simulated SL/TP represented by protection_state=PAPER).
        protection_state: Optional[str] = None
        if (
            book_mode == "LIVE"
            and position_id is not None
            and (row.stop_loss_price or row.take_profit_price)
        ):
            from app.engine.protective_orders import protection_engine

            outcome = await protection_engine.ensure_position_protection(
                position_id,
                authorized_user_id=row.user_id,
            )
            protection_state = getattr(outcome, "state", None)
            if not outcome.ok and getattr(outcome, "fail_closed", False):
                try:
                    await protection_engine.cancel_position_protection(
                        position_id,
                        authorized_user_id=row.user_id,
                        reason="agent fail-closed entry teardown",
                    )
                except Exception:  # noqa: BLE001 - reconcile sweep catches leftovers
                    pass
                async with SessionLocal() as db:
                    pos = await db.get(PositionRecord, position_id)
                    if pos is not None:
                        pos.status = "CLOSED"
                        pos.closed_at = _utcnow()
                        pos.protection_state = "PROTECTION_FAILED"
                        pos.protection_error = (
                            outcome.error
                            or "protective placement failed (fail-closed)"
                        )
                        await db.commit()
                return await self._mark_rejected(
                    row,
                    f"fail-closed protection: {outcome.error or 'unprotected position'}",
                )

        await _persist_intent(
            row.id,
            {
                "status": INTENT_EXECUTED,
                "order_id": claim_id,
                "position_id": position_id,
                "execution_reason": None,
                "error_json": None,
            },
        )
        await _write_audit(
            _AUDIT_EXECUTED,
            user_id=row.user_id,
            resource_type="agent_intent",
            resource_id=row.id,
            details={
                "symbol": row.symbol,
                "side": row.side,
                "quantity": int(row.quantity),
                "order_id": claim_id,
                "position_id": position_id,
                "broker_order_id": str(broker_order_id),
                "filled_price": filled_price,
                "protection_state": protection_state,
            },
        )
        return {
            "ok": True,
            "intent_id": row.id,
            "reason": None,
            "intent": intent_to_dict(row),
            "order_id": claim_id,
            "position_id": position_id,
            "broker_order_id": str(broker_order_id),
            "filled_price": filled_price,
            "protection_state": protection_state,
            "finalized": finalized,
        }

    async def _revert_open(self, position_id: str) -> None:
        """Revert a committed CLOSED claim back to OPEN after LIVE dispatch
        failure — mirrors the manual/DMA close path (trades.py)."""
        async with SessionLocal() as db:
            await db.execute(
                update(PositionRecord)
                .where(PositionRecord.id == position_id)
                .values(status="OPEN", closed_at=None)
            )
            await db.commit()

    # ── close_position: canonical CAS close → PnL → protection teardown ───
    async def close_position(
        self,
        intent_id: str,
        position_id: str,
        *,
        authorized_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Close the position booked by an intent at the live market price.

        Idempotent: the OPEN→CLOSED CAS is the sole gate; replays/concurrent
        callers observe ``ok=True, idempotent=True`` and never double-close.
        """
        del authorized_user_id  # ownership is enforced against the intent
        async with SessionLocal() as db:
            intent_row = await db.get(TradingIntentRecord, intent_id)
            if intent_row is None:
                return {"ok": False, "reason": f"intent {intent_id} not found"}
            pos = await db.get(PositionRecord, position_id)
            if pos is None:
                return {"ok": False, "reason": f"position {position_id} not found"}
            pos_mode = pos.mode or "PAPER"
        if pos.status != "OPEN":
            return {
                "ok": True,
                "idempotent": True,
                "reason": f"position already {pos.status}",
            }
        if (intent_row.user_id or "") != (pos.user_id or ""):
            return {
                "ok": False,
                "reason": "position does not belong to this intent's owner",
            }

        # Exit price: fresh unified market tape, fail-closed to entry price.
        _fresh, _st, _age, px = await self._checked_market_price(pos.symbol)
        exit_price = float(px or 0.0) or float(pos.entry_price)

        # Atomic CAS claim OPEN → CLOSED, committed BEFORE any broker dispatch
        # (crash between dispatch and PnL commit can never double-close).
        async with SessionLocal() as db:
            result = await db.execute(
                update(PositionRecord)
                .where(
                    PositionRecord.id == position_id,
                    PositionRecord.status == "OPEN",
                )
                .values(status="CLOSED", closed_at=_utcnow())
            )
            await db.commit()
            if result.rowcount != 1:
                return {
                    "ok": True,
                    "idempotent": True,
                    "reason": "already closed (concurrent CAS won)",
                }

        is_long = pos.side in ("LONG", "BUY")
        closing_side: str = "SELL" if is_long else "BUY"
        close_ref: Optional[str] = None

        if pos_mode == "LIVE":
            # Tear down exchange-level protection first, then dispatch the
            # real closing order (never fabricate a LIVE close).
            from app.engine.protective_orders import protection_engine

            try:
                await protection_engine.cancel_position_protection(
                    position_id,
                    authorized_user_id=pos.user_id,
                    reason="agent intent close",
                )
            except Exception:  # noqa: BLE001 - reconcile sweep is the backstop
                pass
            broker_acc: Optional[BrokerAccountRecord] = None
            if pos.broker_account_id:
                async with SessionLocal() as db:
                    broker_acc = await db.get(
                        BrokerAccountRecord, pos.broker_account_id
                    )
            if broker_acc is None:
                await self._revert_open(position_id)
                return {
                    "ok": False,
                    "reason": "broker account missing — refusing to fabricate a LIVE close",
                }
            from app.brokers import assert_live_dispatch_allowed, get_broker_adapter

            try:
                assert_live_dispatch_allowed()
            except RuntimeError as exc:
                await self._revert_open(position_id)
                return {"ok": False, "reason": f"live close blocked: {exc}"}
            adapter = get_broker_adapter(broker_acc)
            close_order_req = OrderRequest(
                symbol=pos.symbol,
                side=Side(closing_side),
                quantity=int(pos.quantity),
                order_type="MARKET",
                strategy_id=pos.strategy_id,
            )
            try:
                resp = await adapter.place_order(close_order_req)
                filled_ref = resp.get("filled_price") or resp.get("price")
                if filled_ref:
                    exit_price = float(filled_ref)
                close_ref = resp.get("order_id") or resp.get("broker_order_id")
            except Exception as exc:  # noqa: BLE001
                await self._revert_open(position_id)
                return {"ok": False, "reason": f"broker close dispatch failed: {exc}"}

        # ── Book realized PnL + offsetting trade (single transaction) ───────
        delta = (
            (exit_price - float(pos.entry_price))
            if is_long
            else (float(pos.entry_price) - exit_price)
        )
        realized_pnl = round(delta * int(pos.quantity), 2)
        pnl_pct = (
            round((delta / float(pos.entry_price)) * 100, 2)
            if pos.entry_price
            else 0.0
        )
        cas_recheck = False
        async with SessionLocal() as db:
            pos2 = await db.get(PositionRecord, position_id)
            if pos2 is None or pos2.status != "CLOSED":
                cas_recheck = True
            else:
                pos2.current_price = exit_price
                pos2.realized_pnl = realized_pnl
                pos2.unrealized_pnl = 0.0
                close_order = OrderRecord(
                    id=str(uuid.uuid4()),
                    user_id=pos2.user_id,
                    broker_account_id=pos2.broker_account_id,
                    broker_order_id=close_ref,
                    strategy_id=pos2.strategy_id,
                    agent_intent_id=intent_id,
                    symbol=pos2.symbol,
                    side=closing_side,
                    quantity=int(pos2.quantity),
                    order_type="MARKET",
                    price=exit_price,
                    filled_price=exit_price,
                    filled_quantity=int(pos2.quantity),
                    status="FILLED",
                    mode=pos2.mode,
                )
                db.add(close_order)
                trade = TradeRecord(
                    id=str(uuid.uuid4()),
                    order_id=close_order.id,
                    strategy_id=pos2.strategy_id,
                    strategy_name="AgentIntent Close",
                    symbol=pos2.symbol,
                    side=closing_side,
                    quantity=int(pos2.quantity),
                    price=exit_price,
                    entry_price=pos2.entry_price,
                    exit_price=exit_price,
                    pnl=realized_pnl,
                    pnl_pct=pnl_pct,
                    exit_reason="AGENT_CLOSE",
                    mode=pos2.mode,
                    user_id=pos2.user_id,
                )
                db.add(trade)
                if pos2.mode == "PAPER":
                    # P1-1 owner-scoped credit — the position OWNER's balance.
                    from app.engine.paper_account import credit_paper_pnl

                    await credit_paper_pnl(db, pos2.user_id, realized_pnl)
                await db.commit()
        if cas_recheck:
            return {
                "ok": True,
                "idempotent": True,
                "reason": "position not CLOSED in a fresh read (concurrent close)",
            }

        await _persist_intent(
            intent_id,
            {
                "status": INTENT_CLOSED,
                "position_id": position_id,
                "execution_reason": f"closed@ {exit_price:.2f} pnl={realized_pnl:.2f}",
            },
        )
        await _write_audit(
            _AUDIT_CLOSED,
            user_id=pos.user_id,
            resource_type="agent_intent",
            resource_id=intent_id,
            details={
                "symbol": pos.symbol,
                "side": pos.side,
                "position_id": position_id,
                "exit_price": exit_price,
                "realized_pnl": realized_pnl,
                "close_ref": close_ref,
            },
        )
        return {
            "ok": True,
            "intent_id": intent_id,
            "position_id": position_id,
            "exit_price": exit_price,
            "realized_pnl": realized_pnl,
            "close_ref": close_ref,
        }

    # ── Read helpers ─────────────────────────────────────────────────────────
    async def get_intent(self, intent_id: str) -> Optional[dict[str, Any]]:
        async with SessionLocal() as db:
            row = await db.get(TradingIntentRecord, intent_id)
            return intent_to_dict(row) if row else None

    async def list_intents(
        self,
        user_id: Optional[str] = None,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with SessionLocal() as db:
            stmt = (
                select(TradingIntentRecord)
                .where(TradingIntentRecord.user_id == user_id)
                .order_by(TradingIntentRecord.created_at.desc())
                .limit(limit)
            )
            if not user_id:
                stmt = (
                    select(TradingIntentRecord)
                    .order_by(TradingIntentRecord.created_at.desc())
                    .limit(limit)
                )
            rows = (await db.execute(stmt)).scalars().all()
            return [intent_to_dict(r) for r in rows]


# Shared process-wide instance (mirrors ``protection_engine`` / webhook runtime).
agent_trading_service = AgentTradingService()
