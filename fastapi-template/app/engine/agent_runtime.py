"""Phase 1 Step 2: autonomous-agent runtime.

Consumes the durable, SQLite/PostgreSQL-safe task queue created by migration
``0009_agent_runtime`` (tables ``agents``, ``agent_tasks``,
``agent_runtime_config``).  Every runtime event is additionally mirrored into
the canonical ``audit_logs`` trail (``app.models.audit``).

Core contracts (fail-closed, deterministic, observable)
--------------------------------------------------------
* Attempts are consumed AT CLAIM time: a claimed (RUNNING) task that crashes
  leaves an incremented ``attempts``.  A crashed worker is detected through the
  stale-``heartbeat_at`` recovery scan, which requeues it while attempts remain
  and otherwise marks it FAILED (``WORKER_LOST``).
* An ``autonomous`` task (created_by = agent/system, i.e. NOT a human "user")
  may only execute when BOTH hold:
    1. ``agent_runtime_config.autonomous_mode_enabled`` is true, and
    2. ``global_autonomy_level >= 1`` and the agent's effective autonomy
       ceiling satisfies ``1 <= ceiling <= global_autonomy_level``.
  Approval does NOT widen autonomy: a human-approved task is still provisional
  until the two rules above hold.
* Capability enforcement is an INTERSECTION, fail-closed: an agent may only
  exercise capabilities declared in BOTH the code definition and its DB
  registry row.  A ``readonly`` agent (code-derived) can NEVER execute a
  WRITE/EXECUTE handler (``CAPABILITY_DENIED``).
* Timeouts and attempt budgets are hard-clamped against settings so an
  environment misconfiguration can never widen execution.
* Postgres uses ``SELECT ... FOR UPDATE SKIP LOCKED`` for durable claiming;
  SQLite (single-writer) falls back to the compare-and-swap UPDATE which is
  safe against concurrent workers on the same file.  Both engines additionally
  guard every state transition with a status/attempts CAS (``rowcount == 1``).

Handler contract
----------------
Handlers are registered against ``(agent_type, task_kind)`` and receive a fully
materialised :class:`AgentContext` (no ORM access is ever needed):

    @register_handler(agent_type="engineering_monitor",
                      task_kind="system_health_report",
                      required_capability=CAP_READ)
    async def handler(ctx: AgentContext) -> dict: ...

Missing/disabled registry rows, unknown types, capability violations and
approval gaps are surfaced as deterministic error codes in the task record's
``error_json`` (``code``/``message``/``retryable``/``details``), and each
recording is mirrored to the ``audit_logs`` trail.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.db.session import IS_SQLITE, SessionLocal, rows_affected
from app.models.agent import (
    AgentRecord,
    AgentRuntimeConfigRecord,
    AgentTaskRecord,
)
from app.models.audit import AuditLogRecord

logger = get_logger("engine.agent_runtime")

# ── Status vocabulary (SQL check constraints in 0009)
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)

# ── Creator vocabulary (SQL check constraints in 0009)
CREATOR_USER = "user"
CREATOR_AGENT = "agent"
CREATOR_SYSTEM = "system"
CREATORS = (CREATOR_USER, CREATOR_AGENT, CREATOR_SYSTEM)

# ── Capability vocabulary; readonly agents may only ever exercise the subset
CAP_READ = "READ"
CAP_ANALYZE = "ANALYZE"
CAP_WRITE = "WRITE"
CAP_EXECUTE = "EXECUTE"
KNOWN_CAPABILITIES = (CAP_READ, CAP_ANALYZE, CAP_WRITE, CAP_EXECUTE)
READONLY_CAPABILITIES = frozenset({CAP_READ, CAP_ANALYZE})

# ── Deterministic error codes surfaced through task error_json ─────────
ERR_AGENT_NOT_REGISTERED = "AGENT_NOT_REGISTERED"
ERR_AGENT_DISABLED = "AGENT_DISABLED"
ERR_UNSUPPORTED_TASK_KIND = "UNSUPPORTED_TASK_KIND"
ERR_CAPABILITY_DENIED = "CAPABILITY_DENIED"
ERR_INVALID_TIMEOUT = "INVALID_TIMEOUT"
ERR_INVALID_MAX_ATTEMPTS = "INVALID_MAX_ATTEMPTS"
ERR_INVALID_CREATOR = "INVALID_CREATOR"
ERR_INVALID_IDEMPOTENCY_KEY = "INVALID_IDEMPOTENCY_KEY"
ERR_INVALID_INPUT = "INVALID_INPUT"
ERR_APPROVAL_NOT_APPLICABLE = "APPROVAL_NOT_APPLICABLE"
ERR_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
ERR_APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
ERR_AUTONOMOUS_MODE_DISABLED = "AUTONOMOUS_MODE_DISABLED"
ERR_AUTONOMY_LEVEL_EXCEEDED = "AUTONOMY_LEVEL_EXCEEDED"
ERR_WORKER_LOST = "WORKER_LOST"
ERR_TIMEOUT = "TIMEOUT"
ERR_HANDLER_FAILED = "HANDLER_FAILED"


class AgentRuntimeError(Exception):
    """A deterministic task failure/outcome (stored in ``error_json``)."""

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


class AgentDispatchError(AgentRuntimeError):
    """Input/reference validation failure surfaced synchronously at dispatch.

    Consumer-dispatched tasks that fail a dispatch-time validation check are
    recorded as FAILED with the error code preserved for the UI (never claimed).
    """


class AgentTaskFailure(AgentRuntimeError):
    """Deterministic failure signalled inside a handler implementation."""


@dataclass(frozen=True)
class AgentContext:
    """Read-only, fully materialised execution context handed to a handler.

    Built from a task snapshot AFTER all gates pass, so a handler never needs
    the ORM/database and cannot accidentally widen its own authority.
    """

    task_id: str
    agent_type: str
    task_kind: str
    input: dict[str, Any]
    attempt: int
    created_by: str
    readonly: bool
    max_autonomy_level: int
    autonomy_granted: bool
# ── Handler registry (code-declared behaviour) ─────────────────────────
AgentHandler = Callable[[AgentContext], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class HandlerSpec:
    agent_type: str
    task_kind: str
    required_capability: str
    handler: AgentHandler


_HANDLERS: dict[tuple[str, str], HandlerSpec] = {}


def register_handler(
    *, agent_type: str, task_kind: str, required_capability: str
) -> Callable[[AgentHandler], AgentHandler]:
    """Decorator registering an async handler for ``(agent_type, task_kind)``."""
    if required_capability not in KNOWN_CAPABILITIES:
        raise ValueError(
            f"agent:handler:{agent_type}:{task_kind} unknown required capability "
            f"{required_capability!r}"
        )
    key = (agent_type, task_kind)

    def _deco(fn: AgentHandler) -> AgentHandler:
        if key in _HANDLERS:
            raise ValueError(f"duplicate agent handler registration for {key!r}")
        _HANDLERS[key] = HandlerSpec(
            agent_type=agent_type,
            task_kind=task_kind,
            required_capability=required_capability,
            handler=fn,
        )
        return fn

    return _deco


def handler_spec(agent_type: str, task_kind: str) -> Optional[HandlerSpec]:
    return _HANDLERS.get((agent_type, task_kind))


def supported_task_kinds(agent_type: str) -> tuple[str, ...]:
    if not isinstance(agent_type, str):
        raise AgentDispatchError(ERR_INVALID_INPUT, "agent_type must be a string")
    return tuple(k for (a, k), _ in _HANDLERS.items() if a == agent_type)


def registered_agent_types() -> tuple[str, ...]:
    return tuple(sorted({a for (a, _) in _HANDLERS}))


# ── Agent definitions (code = source of truth for identity/authority) ──
@dataclass(frozen=True)
class AgentDefinition:
    agent_type: str
    name: str
    description: str
    capabilities: tuple[str, ...]
    readonly: bool = True
    max_autonomy_level: int = 0
    enabled_by_default: bool = False

    def __post_init__(self) -> None:
        if any(c not in KNOWN_CAPABILITIES for c in self.capabilities):
            raise ValueError(
                f"unknown capability in {self.agent_type}: {self.capabilities}"
            )
        if self.readonly and any(
            c not in READONLY_CAPABILITIES for c in self.capabilities
        ):
            raise ValueError(
                f"readonly agent {self.agent_type!r} may only declare "
                f"{sorted(READONLY_CAPABILITIES)} capabilities"
            )
        if self.max_autonomy_level < 0:
            raise ValueError(f"negative max_autonomy_level for {self.agent_type}")


_AGENT_DEFINITIONS: dict[str, AgentDefinition] = {}


def register_agent(definition: AgentDefinition) -> None:
    if definition.agent_type in _AGENT_DEFINITIONS:
        raise ValueError(f"duplicate agent registration {definition.agent_type!r}")
    _AGENT_DEFINITIONS[definition.agent_type] = definition


def agent_definition(agent_type: str) -> Optional[AgentDefinition]:
    return _AGENT_DEFINITIONS.get(agent_type)


def validate_registry() -> list[str]:
    """Fail-fast cross-check between definitions and handlers.

    Returns a list of problems (empty = valid).  Every registered handler must
    reference a registered agent whose declared capabilities include the
    handler's required capability.
    """
    problems: list[str] = []
    for (agent_type, task_kind), spec in sorted(_HANDLERS.items()):
        definition = _AGENT_DEFINITIONS.get(agent_type)
        if definition is None:
            problems.append(
                f"handler {agent_type}/{task_kind}: no agent definition registered"
            )
            continue
        if spec.required_capability not in definition.capabilities:
            problems.append(
                f"handler {agent_type}/{task_kind} requires {spec.required_capability}"
                f" but agent {agent_type} declares {list(definition.capabilities)}"
            )
    for agent_type in sorted(_AGENT_DEFINITIONS):
        if agent_type not in {a for (a, _) in _HANDLERS}:
            problems.append(f"agent {agent_type}: definition registered but no handlers")
    return problems


# ── Engineering monitor: the ONLY code-declared agent in Phase 1 Step 2. ─
# The migration seeds the same row (enabled=False => operator opt-in).  The
# DB row may only TIGHTEN the code envelope: effective capabilities are the
# intersection, effective readonly is the code OR DB flag, effective autonomy
# ceiling is the min of both.  No Phase-1 handler may ever write or execute.
_ENGINEERING_MONITOR = AgentDefinition(
    agent_type="engineering_monitor",
    name="Engineering Monitor",
    description=(
        "Read-only platform health and agent-queue auditing. May never write, "
        "execute or place anything; every result is snapshot-derived."
    ),
    capabilities=(CAP_READ, CAP_ANALYZE),
    readonly=True,
    max_autonomy_level=1,
    enabled_by_default=False,
)
register_agent(_ENGINEERING_MONITOR)
# ── Small helpers ─────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str, sort_keys=True)


def _load(text: Optional[str]) -> Any:
    if not text:
        return None
    return json.loads(text)


def _backoff(attempt: int) -> timedelta:
    """Exponential backoff with jitter removed (deterministic in tests)."""
    seconds = min(
        settings.agent_retry_backoff_seconds * (2 ** max(0, attempt - 1)),
        settings.agent_retry_backoff_max_seconds,
    )
    return timedelta(seconds=seconds)


async def load_runtime_config(db: AsyncSession) -> AgentRuntimeConfigRecord:
    """Read the singleton config row, idempotently creating it if absent."""
    rec = await db.get(AgentRuntimeConfigRecord, 1)
    if rec is None:
        rec = AgentRuntimeConfigRecord(
            id=1, autonomous_mode_enabled=False, global_autonomy_level=0
        )
        db.add(rec)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            rec = await db.get(AgentRuntimeConfigRecord, 1)
            if rec is None:  # pragma: no cover - logically unreachable
                raise
    return rec


async def _audit(
    db: AsyncSession,
    action: str,
    *,
    actor: str,
    target_type: str,
    target_id: str,
    details: Optional[dict[str, Any]],
) -> None:
    """Append an immutable event to the canonical ``audit_logs`` trail."""
    db.add(AuditLogRecord(
        user_id=None,
        action=action,
        resource_type=target_type,
        resource_id=target_id,
        status="SUCCESS",
        details_json=_dump({"actor": actor, **(details or {})}),
    ))


async def ensure_agent_registry() -> list[str]:
    """Idempotently provision DB registry rows for every code-declared agent.

    NEVER overwrites an existing row -- operators may only tighten a row, so
    ``enabled`` and capability restrictions are preserved across restarts.
    Returns the list of agent types that needed a row created.
    """
    problems = validate_registry()
    if problems:
        raise RuntimeError("agent registry invalid: " + "; ".join(problems))
    created: list[str] = []
    async with SessionLocal() as db:
        for definition in _AGENT_DEFINITIONS.values():
            existing = (
                await db.execute(
                    select(AgentRecord).where(
                        AgentRecord.agent_type == definition.agent_type
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(AgentRecord(
                    agent_type=definition.agent_type,
                    name=definition.name,
                    description=definition.description,
                    capabilities_json=_dump(list(definition.capabilities)),
                    readonly=definition.readonly,
                    max_autonomy_level=definition.max_autonomy_level,
                    enabled=definition.enabled_by_default,
                ))
                created.append(definition.agent_type)
        if created:
            await db.commit()
    return created
# ── Engineering monitor handlers (READ-ONLY, snapshot-derived) ────────
@register_handler(
    agent_type="engineering_monitor",
    task_kind="system_health_report",
    required_capability=CAP_READ,
)
async def _engineering_monitor_health_report(ctx: AgentContext) -> dict[str, Any]:
    """Aggregate platform health snapshot for operator audits.

    Every number is derived from persisted rows at read time -- nothing is
    fabricated, and nothing in this handler mutates the database.
    """
    from app.models.broker_account import BrokerAccountRecord
    from app.models.broker_state import BrokerStateRecord
    from app.models.protective_order import ProtectiveOrderRecord
    from app.models.trading import OrderRecord, PositionRecord, StrategyRecord
    from app.models.user import UserRecord

    now = _utcnow()
    stale_before = now - timedelta(seconds=settings.broker_state_stale_after)
    async with SessionLocal() as db:
        async def count_rows(model: Any, *clauses: Any) -> int:
            stmt = select(func.count()).select_from(model)
            for clause in clauses:
                stmt = stmt.where(clause)
            return int((await db.execute(stmt)).scalar_one() or 0)

        cfg = await load_runtime_config(db)
        tasks_by_status = {
            s: int(c)
            for s, c in (
                await db.execute(
                    select(AgentTaskRecord.status, func.count())
                    .group_by(AgentTaskRecord.status)
                )
            ).all()
        }
        orders_by_status = {
            s: int(c)
            for s, c in (
                await db.execute(
                    select(OrderRecord.status, func.count())
                    .group_by(OrderRecord.status)
                )
            ).all()
        }
        protection_by_status = {
            s: int(c)
            for s, c in (
                await db.execute(
                    select(ProtectiveOrderRecord.status, func.count())
                    .group_by(ProtectiveOrderRecord.status)
                )
            ).all()
        }

        agents_enabled = await count_rows(AgentRecord, AgentRecord.enabled.is_(True))
        users_total = await count_rows(UserRecord)
        accounts_active = await count_rows(
            BrokerAccountRecord, BrokerAccountRecord.is_active.is_(True)
        )
        open_positions = await count_rows(
            PositionRecord, PositionRecord.status == "OPEN"
        )
        strategies_enabled = await count_rows(
            StrategyRecord, StrategyRecord.enabled.is_(True)
        )
        broker_snapshots_live = await count_rows(
            BrokerStateRecord, BrokerStateRecord.status == "LIVE"
        )
        broker_snapshots_other = await count_rows(
            BrokerStateRecord,
            BrokerStateRecord.captured_at.is_not(None),
            BrokerStateRecord.status != "LIVE",
        )

    warnings: list[str] = []
    if tasks_by_status.get(STATUS_FAILED, 0) > 0:
        warnings.append(
            f"{tasks_by_status[STATUS_FAILED]} agent task(s) FAILED - review error_json"
        )
    if cfg.autonomous_mode_enabled and agents_enabled == 0:
        warnings.append("autonomous mode enabled but no agent is enabled")
    if broker_snapshots_other > broker_snapshots_live:
        warnings.append(
            "broker truth degraded: stale/unavailable snapshots outnumber LIVE ones"
        )

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "readonly": ctx.readonly,
        "autonomy_granted": ctx.autonomy_granted,
        "platform": {
            "users_total": users_total,
            "broker_accounts_active": accounts_active,
            "open_positions": open_positions,
            "strategies_enabled": strategies_enabled,
            "orders_by_status": orders_by_status,
        },
        "protection": {"protective_orders_by_status": protection_by_status},
        "broker_state": {
            "snapshots_total": broker_snapshots_live + broker_snapshots_other,
            "stale_or_unavailable": broker_snapshots_other,
            "stale_after_seconds": settings.broker_state_stale_after,
        },
        "agents": {
            "total": len(registered_agent_types()),
            "enabled": agents_enabled,
            "autonomous_mode_enabled": bool(cfg.autonomous_mode_enabled),
            "global_autonomy_level": int(cfg.global_autonomy_level),
        },
        "warnings": warnings,
    }
@register_handler(
    agent_type="engineering_monitor",
    task_kind="agent_queue_audit",
    required_capability=CAP_READ,
)
async def _engineering_monitor_queue_audit(ctx: AgentContext) -> dict[str, Any]:
    """Queue-depth and health audit for the durable agent_tasks table."""
    now = _utcnow()
    stale_before = now - timedelta(seconds=settings.agent_stale_running_seconds)
    async with SessionLocal() as db:
        cfg = await load_runtime_config(db)
        by_status = {
            s: int(c)
            for s, c in (
                await db.execute(
                    select(AgentTaskRecord.status, func.count())
                    .group_by(AgentTaskRecord.status)
                )
            ).all()
        }
        oldest_pending = (
            await db.execute(
                select(func.min(AgentTaskRecord.created_at)).where(
                    AgentTaskRecord.status == STATUS_PENDING
                )
            )
        ).scalar_one_or_none()
        oldest_running = (
            await db.execute(
                select(func.min(AgentTaskRecord.started_at)).where(
                    AgentTaskRecord.status == STATUS_RUNNING
                )
            )
        ).scalar_one_or_none()
        pending_awaiting_approval = int(
            (
                await db.execute(
                    select(func.count()).select_from(AgentTaskRecord).where(
                        AgentTaskRecord.status == STATUS_PENDING,
                        AgentTaskRecord.requires_approval.is_(True),
                        AgentTaskRecord.approved_by.is_(None),
                    )
                )
            ).scalar_one()
        )
        running_rows = (
            await db.execute(
                select(AgentTaskRecord)
                .where(AgentTaskRecord.status == STATUS_RUNNING)
                .order_by(AgentTaskRecord.started_at)
                .limit(settings.agent_max_rows_per_scan)
            )
        ).scalars().all()

    stale_running: list[dict[str, Any]] = []
    for row in running_rows:
        last_beacon = _as_utc(row.heartbeat_at) or _as_utc(row.started_at)
        if last_beacon is not None and last_beacon <= stale_before:
            stale_running.append({
                "id": row.id,
                "agent_type": row.agent_type,
                "task_kind": row.task_kind,
                "attempts": row.attempts,
                "heartbeat_at": last_beacon.isoformat(timespec="seconds"),
            })

    oldest_pending_iso: str | None = None
    if oldest_pending is not None:
        resolved = _as_utc(oldest_pending)
        if resolved is not None:
            oldest_pending_iso = resolved.isoformat(timespec="seconds")

    oldest_running_iso: str | None = None
    if oldest_running is not None:
        resolved = _as_utc(oldest_running)
        if resolved is not None:
            oldest_running_iso = resolved.isoformat(timespec="seconds")

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "readonly": ctx.readonly,
        "autonomy_granted": ctx.autonomy_granted,
        "tasks_by_status": by_status,
        "pending_awaiting_approval": pending_awaiting_approval,
        "oldest_pending_created_at": oldest_pending_iso,
        "oldest_running_started_at": oldest_running_iso,
        "stale_running_count": len(stale_running),
        "stale_running": stale_running[:20],
        "stale_running_after_seconds": settings.agent_stale_running_seconds,
        "autonomous_mode_enabled": bool(cfg.autonomous_mode_enabled),
        "global_autonomy_level": int(cfg.global_autonomy_level),
        "supported_task_kinds": {
            agent_type: list(supported_task_kinds(agent_type))
            for agent_type in registered_agent_types()
        },
    }
# ── Runtime ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _TaskSnapshot:
    id: str
    agent_type: str
    task_kind: str
    status: str
    attempts: int
    max_attempts: int
    requires_approval: bool
    approved_by: Optional[str]
    approved_at: Optional[datetime]
    approval_expires_at: Optional[datetime]
    created_by: str
    timeout_seconds: float
    input: dict[str, Any]


def task_to_dict(rec: AgentTaskRecord) -> dict[str, Any]:
    """Deterministic ISO-8601/UTC serialization for API consumers."""

    def _iso(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        resolved = _as_utc(value)
        return resolved.isoformat(timespec="seconds") if resolved is not None else None

    return {
        "id": rec.id,
        "agent_type": rec.agent_type,
        "task_kind": rec.task_kind,
        "status": rec.status,
        "attempts": rec.attempts,
        "max_attempts": rec.max_attempts,
        "timeout_seconds": rec.timeout_seconds,
        "requires_approval": bool(rec.requires_approval),
        "approved_by": rec.approved_by or None,
        "approved_at": _iso(rec.approved_at),
        "approval_expires_at": _iso(rec.approval_expires_at),
        "created_by": rec.created_by,
        "requested_by": rec.requested_by,
        "idempotency_key": rec.idempotency_key or None,
        "input": _load(rec.input_json),
        "output": _load(rec.output_json),
        "error": _load(rec.error_json),
        "created_at": _iso(rec.created_at),
        "started_at": _iso(rec.started_at),
        "completed_at": _iso(rec.completed_at),
        "heartbeat_at": _iso(rec.heartbeat_at),
        "next_retry_at": _iso(rec.next_retry_at),
    }


class AgentRuntime:
    """Durable, gated consumer of the ``agent_tasks`` queue."""

    def __init__(
        self,
        *,
        batch_size: Optional[int] = None,
        default_timeout_seconds: Optional[float] = None,
    ) -> None:
        self.batch_size = int(batch_size or settings.agent_runtime_batch_size)
        self.default_timeout_seconds = float(
            default_timeout_seconds or settings.agent_task_default_timeout_seconds
        )
        self._current: set[str] = set()

    # ── Runtime configuration (singleton row, id 1) ───────────────────
    async def update_runtime_config(
        self,
        *,
        autonomous_mode_enabled: bool,
        global_autonomy_level: int,
        updated_by: str = "admin",
    ) -> dict[str, Any]:
        async with SessionLocal() as db:
            rec = await load_runtime_config(db)
            previous = {
                "autonomous_mode_enabled": bool(rec.autonomous_mode_enabled),
                "global_autonomy_level": int(rec.global_autonomy_level),
            }
            rec.autonomous_mode_enabled = bool(autonomous_mode_enabled)
            rec.global_autonomy_level = max(0, int(global_autonomy_level))
            rec.updated_by = updated_by
            rec.updated_at = _utcnow()
            await db.commit()
            await db.refresh(rec)
            await _audit(
                db,
                "agent.runtime_config.updated",
                actor=updated_by,
                target_type="agent_runtime_config",
                target_id=str(rec.id),
                details={
                    "previous": previous,
                    "autonomous_mode_enabled": bool(rec.autonomous_mode_enabled),
                    "global_autonomy_level": int(rec.global_autonomy_level),
                },
            )
            await db.commit()
            return {
                "autonomous_mode_enabled": bool(rec.autonomous_mode_enabled),
                "global_autonomy_level": int(rec.global_autonomy_level),
                "updated_by": rec.updated_by,
                "updated_at": rec.updated_at.isoformat(timespec="seconds"),
            }
# ── Dispatch (new task / dedupe on idempotency_key) ───────────────
    async def create_task(
        self,
        *,
        agent_type: str,
        task_kind: str,
        input_payload: Optional[Mapping[str, Any]] = None,
        created_by: str = CREATOR_USER,
        requires_approval: bool = False,
        idempotency_key: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_attempts: Optional[int] = None,
        requested_by: Optional[str] = None,
    ) -> tuple[dict[str, Any], bool]:
        """Enqueue a durable task; returns ``(task_dict, created: bool)``.

        Raises :class:`AgentDispatchError` with a deterministic error code for
        every dispatch-time validation/gate failure (agent unknown/disabled,
        capability violation, invalid retry/timeout bounds, invalid creator).
        Idempotent on ``idempotency_key``: a repeat call returns the existing
        task with ``created=False``.
        """
        if not isinstance(agent_type, str) or not agent_type.strip():
            raise AgentDispatchError(ERR_INVALID_INPUT, "agent_type is required")
        if not isinstance(task_kind, str) or not task_kind.strip():
            raise AgentDispatchError(ERR_INVALID_INPUT, "task_kind is required")
        if created_by not in CREATORS:
            raise AgentDispatchError(
                ERR_INVALID_CREATOR,
                f"created_by must be one of {CREATORS}, got {created_by!r}",
            )
        if input_payload is None:
            input_payload = {}
        if not isinstance(input_payload, Mapping):
            raise AgentDispatchError(
                ERR_INVALID_INPUT, "input_payload must be a JSON object"
            )
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or len(idempotency_key) > 128
        ):
            raise AgentDispatchError(
                ERR_INVALID_IDEMPOTENCY_KEY,
                "idempotency_key must be a string of at most 128 characters",
            )

        spec = handler_spec(agent_type, task_kind)
        if spec is None:
            kinds = supported_task_kinds(agent_type)
            if not kinds:
                raise AgentDispatchError(
                    ERR_AGENT_NOT_REGISTERED,
                    f"no runtime handler registered for agent type {agent_type!r}",
                )
            raise AgentDispatchError(
                ERR_UNSUPPORTED_TASK_KIND,
                f"task_kind {task_kind!r} is not supported by agent {agent_type!r}; "
                f"supported kinds: {sorted(kinds)}",
            )
        definition = agent_definition(agent_type)
        assert definition is not None  # validated by the registry cross-check

        # Hard-clamp retry budget and timeout (env can never widen execution).
        eff_attempts = (
            settings.agent_task_default_max_attempts
            if max_attempts is None
            else int(max_attempts)
        )
        if not 1 <= eff_attempts <= settings.agent_task_max_attempts_limit:
            raise AgentDispatchError(
                ERR_INVALID_MAX_ATTEMPTS,
                f"max_attempts must be within "
                f"[1, {settings.agent_task_max_attempts_limit}], got {eff_attempts}",
            )
        eff_timeout = (
            self.default_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if eff_timeout <= 0:
            raise AgentDispatchError(
                ERR_INVALID_TIMEOUT, "timeout_seconds must be positive"
            )
        eff_timeout = min(
            max(eff_timeout, settings.agent_task_min_timeout_seconds),
            settings.agent_task_max_timeout_seconds,
        )

        async with SessionLocal() as db:
            agent = (
                await db.execute(
                    select(AgentRecord).where(AgentRecord.agent_type == agent_type)
                )
            ).scalar_one_or_none()
            if agent is None:
                raise AgentDispatchError(
                    ERR_AGENT_NOT_REGISTERED,
                    f"agent {agent_type!r} is not provisioned in the registry",
                )
            if not agent.enabled:
                raise AgentDispatchError(
                    ERR_AGENT_DISABLED,
                    f"agent {agent_type!r} is disabled by the operator",
                )
            db_caps = set(_load(agent.capabilities_json) or [])
            effective_caps = set(definition.capabilities) & db_caps
            if spec.required_capability not in effective_caps:
                raise AgentDispatchError(
                    ERR_CAPABILITY_DENIED,
                    f"agent {agent_type!r} is not permitted to run task_kind "
                    f"{task_kind!r} (requires {spec.required_capability})",
                )

            if idempotency_key:
                existing = (
                    await db.execute(
                        select(AgentTaskRecord).where(
                            AgentTaskRecord.idempotency_key == idempotency_key
                        )
                    )
                ).scalars().first()
                if existing is not None:
                    return task_to_dict(existing), False
            now = _utcnow()
            task = AgentTaskRecord(
                id=str(uuid.uuid4()),
                agent_type=agent_type,
                task_kind=task_kind,
                input_json=_dump(dict(input_payload)),
                status=STATUS_PENDING,
                attempts=0,
                max_attempts=eff_attempts,
                timeout_seconds=eff_timeout,
                requires_approval=bool(requires_approval),
                created_by=created_by,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                created_at=now,
            )
            db.add(task)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                if idempotency_key:
                    existing = (
                        await db.execute(
                            select(AgentTaskRecord).where(
                                AgentTaskRecord.idempotency_key == idempotency_key
                            )
                        )
                    ).scalars().first()
                    if existing is not None:
                        return task_to_dict(existing), False
                raise
            await db.refresh(task)
            await _audit(
                db,
                "agent.task.created",
                actor=created_by,
                target_type="agent_task",
                target_id=task.id,
                details={
                    "agent_type": agent_type,
                    "task_kind": task_kind,
                    "requires_approval": bool(requires_approval),
                },
            )
            await db.commit()
            return task_to_dict(task), True

    # ── Human approval / revocation of a requires_approval task ───────
    async def approve_task(self, task_id: str, *, approved_by: str) -> dict[str, Any]:
        """Approve (or refresh) the human approval on a task. Fails closed for
        any task that did not request approval or is already terminal."""
        async with SessionLocal() as db:
            task = await db.get(AgentTaskRecord, task_id)
            if task is None:
                raise AgentDispatchError(ERR_INVALID_INPUT, f"task {task_id!r} not found")
            if not task.requires_approval:
                raise AgentDispatchError(
                    ERR_APPROVAL_NOT_APPLICABLE,
                    "task does not require approval; nothing to approve",
                )
            if task.status in TERMINAL_STATUSES:
                raise AgentDispatchError(
                    ERR_APPROVAL_NOT_APPLICABLE,
                    f"task is already {task.status}; approval is closed",
                )
            now = _utcnow()
            task.approved_by = approved_by
            task.approved_at = now
            task.approval_expires_at = now + timedelta(
                seconds=settings.agent_approval_window_seconds
            )
            await db.commit()
            await db.refresh(task)
            await _audit(
                db,
                "agent.task.approved",
                actor=approved_by,
                target_type="agent_task",
                target_id=task.id,
                details={"agent_type": task.agent_type, "task_kind": task.task_kind},
            )
            await db.commit()
            return task_to_dict(task)

    async def revoke_approval(self, task_id: str, *, revoked_by: str) -> dict[str, Any]:
        """Clear the human approval; an unapproved required-approval task can
        never be claimed again until re-approved (fail-closed)."""
        async with SessionLocal() as db:
            task = await db.get(AgentTaskRecord, task_id)
            if task is None:
                raise AgentDispatchError(ERR_INVALID_INPUT, f"task {task_id!r} not found")
            if not task.requires_approval:
                raise AgentDispatchError(
                    ERR_APPROVAL_NOT_APPLICABLE,
                    "task does not require approval; nothing to revoke",
                )
            if task.status == STATUS_RUNNING:
                raise AgentDispatchError(
                    ERR_APPROVAL_NOT_APPLICABLE,
                    "task is RUNNING; revocation of in-flight work is unsupported",
                )
            task.approved_by = None
            task.approved_at = None
            task.approval_expires_at = None
            await db.commit()
            await db.refresh(task)
            await _audit(
                db,
                "agent.task.approval_revoked",
                actor=revoked_by,
                target_type="agent_task",
                target_id=task.id,
                details={"agent_type": task.agent_type, "task_kind": task.task_kind},
            )
            await db.commit()
            return task_to_dict(task)
# ── Gate evaluation (pure, deterministic; runs before every execution) ─
    def _gate_error(
        self,
        *,
        snapshot: _TaskSnapshot,
        definition: Optional[AgentDefinition],
        spec: HandlerSpec,
        agent_enabled: bool,
        agent_capabilities_json: Optional[str],
        agent_autonomy_level: int,
        autonomous_enabled: bool,
        global_autonomy_level: int,
    ) -> Optional[AgentRuntimeError]:
        """Return the deterministic gate failure the task must record, or None
        if the task may execute.  Every check is fail-closed and works from
        pre-materialised plain values (never the ORM)."""
        if definition is None:
            return AgentRuntimeError(
                ERR_AGENT_NOT_REGISTERED,
                "no runtime definition registered for this task",
            )
        if not agent_enabled:
            return AgentRuntimeError(
                ERR_AGENT_DISABLED, f"agent {snapshot.agent_type!r} is disabled"
            )

        # Capability intersection gate (DB row may only tighten the code set).
        db_caps = set(_load(agent_capabilities_json) or [])
        effective_caps = set(definition.capabilities) & db_caps
        if spec.required_capability not in effective_caps:
            return AgentRuntimeError(
                ERR_CAPABILITY_DENIED,
                f"{snapshot.agent_type!r} is not permitted to run "
                f"{snapshot.task_kind!r} (requires {spec.required_capability})",
            )

        # Approval gate (unreachable via the claim filter; re-checked closed).
        if snapshot.requires_approval:
            if not snapshot.approved_by or snapshot.approved_at is None:
                return AgentRuntimeError(
                    ERR_APPROVAL_REQUIRED,
                    "task requires human approval before execution",
                )
            expires = _as_utc(snapshot.approval_expires_at)
            if expires is not None and expires <= _utcnow():
                return AgentRuntimeError(
                    ERR_APPROVAL_EXPIRED,
                    "human approval has expired; re-approve before execution",
                )

        # Autonomy gate: a task created by an agent/system (not a human user)
        # requires autonomous mode AND a compatible autonomy ceiling.
        if snapshot.created_by in (CREATOR_AGENT, CREATOR_SYSTEM):
            if not autonomous_enabled:
                return AgentRuntimeError(
                    ERR_AUTONOMOUS_MODE_DISABLED,
                    "autonomous mode is disabled; dispatch via a human instead",
                )
            code_level = int(definition.max_autonomy_level or 0)
            ceiling = min(agent_autonomy_level, code_level)
            if global_autonomy_level < 1:
                return AgentRuntimeError(
                    ERR_AUTONOMY_LEVEL_EXCEEDED,
                    "global autonomy level is 0; autonomous execution disabled",
                )
            if not 1 <= ceiling <= global_autonomy_level:
                return AgentRuntimeError(
                    ERR_AUTONOMY_LEVEL_EXCEEDED,
                    f"agent autonomy ceiling {ceiling} exceeds global level "
                    f"{global_autonomy_level}",
                )
        return None

    # ── Durable claiming (FOR UPDATE SKIP LOCKED on Postgres; CAS on SQLite) ─
    def _is_eligible_now(self, rec: AgentTaskRecord, now: datetime) -> bool:
        if rec.requires_approval:
            if not rec.approved_by or rec.approved_at is None:
                return False
            expires = _as_utc(rec.approval_expires_at)
            if expires is not None and expires <= now:
                return False
        next_retry = _as_utc(rec.next_retry_at)
        if next_retry is not None and next_retry > now:
            return False
        return True

    async def _claim_batch(self) -> list[str]:
        """Claim up to ``batch_size`` eligible pending tasks (attempts consumed
        at claim time).  Returns the claimed task ids."""
        claimed: list[str] = []
        now = _utcnow()
        async with SessionLocal() as db:
            stmt = (
                select(AgentTaskRecord)
                .where(
                    AgentTaskRecord.status == STATUS_PENDING,
                    AgentTaskRecord.attempts < AgentTaskRecord.max_attempts,
                )
                .order_by(AgentTaskRecord.created_at, AgentTaskRecord.id)
                .limit(self.batch_size * 4)
            )
            if not IS_SQLITE:
                stmt = stmt.with_for_update(skip_locked=True)
            rows = (await db.execute(stmt)).scalars().all()
            for rec in rows:
                if len(claimed) >= self.batch_size:
                    break
                if not self._is_eligible_now(rec, now):
                    continue
                result = await db.execute(
                    update(AgentTaskRecord)
                    .where(
                        AgentTaskRecord.id == rec.id,
                        AgentTaskRecord.status == STATUS_PENDING,
                        AgentTaskRecord.attempts == rec.attempts,
                    )
                    .values(
                        status=STATUS_RUNNING,
                        attempts=rec.attempts + 1,
                        started_at=now,
                        heartbeat_at=now,
                        completed_at=None,
                        next_retry_at=None,
                        error_json=None,
                    )
                )
                if rows_affected(result) == 1:
                    claimed.append(rec.id)
            await db.commit()
        return claimed
# ── Execution with timeout + deterministic failure recording ──────────
    async def _finalize_succeeded(
        self, task_id: str, *, attempt: int, output: dict[str, Any]
    ) -> dict[str, Any]:
        now = _utcnow()
        async with SessionLocal() as db:
            result = await db.execute(
                update(AgentTaskRecord)
                .where(
                    AgentTaskRecord.id == task_id,
                    AgentTaskRecord.status == STATUS_RUNNING,
                )
                .values(
                    status=STATUS_SUCCEEDED,
                    output_json=_dump(output),
                    error_json=None,
                    completed_at=now,
                    heartbeat_at=now,
                    next_retry_at=None,
                )
            )
            await db.commit()
        if rows_affected(result) == 1:
            logger.info("[AgentRuntime] task %s SUCCEEDED (attempt %d)", task_id, attempt)
            return {"outcome": "succeeded", "task_id": task_id, "status": STATUS_SUCCEEDED}
        return {"outcome": "superseded", "task_id": task_id, "status": None}

    async def _finalize_failure(
        self,
        task_id: str,
        *,
        attempt: int,
        err: AgentRuntimeError,
        attempts_remaining: int = 0,
    ) -> dict[str, Any]:
        """Record a deterministic failure.  ``attempts_remaining > 0`` and an
        ``err.retryable`` flag requeue the task with exponential backoff;
        otherwise the task terminates FAILED."""
        retryable = bool(err.retryable and attempts_remaining > 0)
        now = _utcnow()
        async with SessionLocal() as db:
            values: dict[str, Any] = {
                "status": STATUS_FAILED,
                "error_json": _dump({
                    "code": err.code,
                    "message": err.message,
                    "retryable": err.retryable,
                    "details": err.details,
                }),
                "completed_at": now,
                "heartbeat_at": now,
                "next_retry_at": None,
            }
            if retryable:
                values.update({
                    "status": STATUS_PENDING,
                    "next_retry_at": now + _backoff(attempt),
                    "completed_at": None,
                    "started_at": None,
                })
            result = await db.execute(
                update(AgentTaskRecord)
                .where(
                    AgentTaskRecord.id == task_id,
                    AgentTaskRecord.status == STATUS_RUNNING,
                )
                .values(**values)
            )
            await _audit(
                db,
                "agent.task.requeued" if retryable else "agent.task.failed",
                actor="runtime",
                target_type="agent_task",
                target_id=task_id,
                details={
                    "code": err.code,
                    "attempt": attempt,
                    "retryable": err.retryable,
                    "attempts_remaining": attempts_remaining,
                },
            )
            await db.commit()
        if rows_affected(result) != 1:
            return {"outcome": "superseded", "task_id": task_id, "status": None}
        logger.warning(
            "[AgentRuntime] task %s attempt %d -> %s (%s)",
            task_id, attempt, "REQUEUED" if retryable else "FAILED", err.code,
        )
        return {
            "outcome": "requeued" if retryable else "failed",
            "task_id": task_id,
            "status": STATUS_PENDING if retryable else STATUS_FAILED,
            "error": {"code": err.code, "message": err.message},
        }
    async def execute_task(self, task_id: str) -> dict[str, Any]:
        """Execute a previously claimed RUNNING task end-to-end."""
        async with SessionLocal() as db:
            task = await db.get(AgentTaskRecord, task_id)
            if task is None:
                return {"outcome": "missing", "task_id": task_id, "status": None}
            if task.status != STATUS_RUNNING:
                return {
                    "outcome": "not_running",
                    "task_id": task_id,
                    "status": task.status,
                }
            agent = (
                await db.execute(
                    select(AgentRecord).where(
                        AgentRecord.agent_type == task.agent_type
                    )
                )
            ).scalar_one_or_none()
            # Materialise EVERY value the gate needs BEFORE any commit can
            # expire these objects (a fresh runtime-config row triggers one).
            agent_enabled = bool(agent.enabled) if agent is not None else False
            agent_capabilities_json = (
                agent.capabilities_json if agent is not None else None
            )
            agent_autonomy_level = (
                int(agent.max_autonomy_level or 0) if agent is not None else 0
            )
            snapshot = _TaskSnapshot(
                id=task.id,
                agent_type=task.agent_type,
                task_kind=task.task_kind,
                status=task.status,
                attempts=task.attempts,
                max_attempts=task.max_attempts,
                requires_approval=bool(task.requires_approval),
                approved_by=task.approved_by,
                approved_at=task.approved_at,
                approval_expires_at=task.approval_expires_at,
                created_by=task.created_by,
                timeout_seconds=task.timeout_seconds,
                input=dict(_load(task.input_json) or {}),
            )
            cfg = await load_runtime_config(db)
            autonomous_enabled = bool(cfg.autonomous_mode_enabled)
            global_autonomy_level = int(cfg.global_autonomy_level)

        definition = agent_definition(snapshot.agent_type)
        spec = handler_spec(snapshot.agent_type, snapshot.task_kind)
        if definition is None or spec is None:
            return await self._finalize_failure(
                task_id,
                attempt=snapshot.attempts,
                err=AgentRuntimeError(
                    ERR_AGENT_NOT_REGISTERED,
                    "no runtime definition/handler registered for this task",
                ),
            )
        gate = self._gate_error(
            snapshot=snapshot,
            definition=definition,
            spec=spec,
            agent_enabled=agent_enabled,
            agent_capabilities_json=agent_capabilities_json,
            agent_autonomy_level=agent_autonomy_level,
            autonomous_enabled=autonomous_enabled,
            global_autonomy_level=global_autonomy_level,
        )
        if gate is not None:
            return await self._finalize_failure(
                task_id, attempt=snapshot.attempts, err=gate
            )

        ctx = AgentContext(
            task_id=task_id,
            agent_type=snapshot.agent_type,
            task_kind=snapshot.task_kind,
            input=snapshot.input,
            attempt=snapshot.attempts,
            created_by=snapshot.created_by,
            readonly=bool(definition.readonly),
            max_autonomy_level=int(definition.max_autonomy_level),
            autonomy_granted=snapshot.created_by in (CREATOR_AGENT, CREATOR_SYSTEM),
        )
        self._current.add(task_id)
        try:
            try:
                output = await asyncio.wait_for(
                    spec.handler(ctx), timeout=snapshot.timeout_seconds
                )
                if not isinstance(output, Mapping):
                    raise AgentTaskFailure(
                        ERR_HANDLER_FAILED,
                        "handler returned a non-object result",
                        retryable=False,
                    )
            except asyncio.TimeoutError:
                raise AgentTaskFailure(
                    ERR_TIMEOUT,
                    f"execution exceeded timeout of {snapshot.timeout_seconds}s",
                    retryable=True,
                ) from None
            except AgentTaskFailure:
                raise
            except Exception as exc:  # noqa: BLE001 - boundary; record, don't crash
                raise AgentTaskFailure(
                    ERR_HANDLER_FAILED,
                    f"{type(exc).__name__}: {exc}",
                    retryable=True,
                ) from exc
            return await self._finalize_succeeded(
                task_id, attempt=snapshot.attempts, output=dict(output)
            )
        except asyncio.CancelledError:
            raise
        except AgentTaskFailure as final_err:
            return await self._finalize_failure(
                task_id,
                attempt=snapshot.attempts,
                err=final_err,
                attempts_remaining=max_attempts_remaining(snapshot),
            )
        finally:
            self._current.discard(task_id)

    # ── Stale-worker recovery + scheduler pass ─────────────────────────
    async def _recover_stale_running(self) -> int:
        """Requeue/FAIL RUNNING tasks whose heartbeat is stale (crashed
        workers).  Never touches tasks currently executing in-process."""
        now = _utcnow()
        threshold = now - timedelta(seconds=settings.agent_stale_running_seconds)
        recovered = 0
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(AgentTaskRecord)
                    .where(AgentTaskRecord.status == STATUS_RUNNING)
                    .order_by(AgentTaskRecord.started_at)
                    .limit(settings.agent_max_rows_per_scan)
                )
            ).scalars().all()
            for rec in rows:
                if rec.id in self._current:
                    continue
                last_beacon = _as_utc(rec.heartbeat_at) or _as_utc(rec.started_at)
                if last_beacon is None or last_beacon > threshold:
                    continue
                retryable = rec.attempts < rec.max_attempts
                values: dict[str, Any] = {
                    "status": STATUS_PENDING if retryable else STATUS_FAILED,
                    "heartbeat_at": now,
                    "error_json": _dump({
                        "code": ERR_WORKER_LOST,
                        "message": "worker heartbeat expired; task recovered",
                        "retryable": retryable,
                        "details": {"recovered_at": now.isoformat(timespec="seconds")},
                    }),
                }
                if retryable:
                    values["next_retry_at"] = now
                    values["started_at"] = None
                else:
                    values["completed_at"] = now
                result = await db.execute(
                    update(AgentTaskRecord)
                    .where(
                        AgentTaskRecord.id == rec.id,
                        AgentTaskRecord.status == STATUS_RUNNING,
                    )
                    .values(**values)
                )
                recovered += rows_affected(result)
            if recovered:
                await db.commit()
        return recovered

    async def run_once(self) -> dict[str, Any]:
        """One scheduler pass: recover stale workers, drain a claim batch."""
        summary: dict[str, Any] = {
            "recovered": await self._recover_stale_running(),
            "claimed": 0, "succeeded": 0, "failed": 0,
            "requeued": 0, "skipped": 0,
        }
        for task_id in await self._claim_batch():
            summary["claimed"] += 1
            outcome = await self.execute_task(task_id)
            key = outcome.get("outcome")
            if key == "succeeded":
                summary["succeeded"] += 1
            elif key == "failed":
                summary["failed"] += 1
            elif key == "requeued":
                summary["requeued"] += 1
            else:
                summary["skipped"] += 1
        return summary
def max_attempts_remaining(snapshot: _TaskSnapshot) -> int:
    """Attempts still available after this claim (0 when the budget is spent)."""
    return max(0, int(snapshot.max_attempts) - int(snapshot.attempts))


# ── Scheduler lifecycle (started/stopped from app.main lifespan) ───────
class AgentRuntimeScheduler:
    """Background loop draining the agent queue on a fixed interval."""

    def __init__(
        self,
        runtime: Optional[AgentRuntime] = None,
        interval_seconds: Optional[float] = None,
    ) -> None:
        self.runtime = runtime or agent_runtime_default()
        self.interval_seconds = float(
            interval_seconds
            if interval_seconds is not None
            else settings.agent_runtime_interval
        )
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            logger.info("[AgentRuntime] scheduler already running; start() ignored")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("[AgentRuntime] scheduler STARTED (interval=%ss)", self.interval_seconds)

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        logger.info("[AgentRuntime] scheduler STOPPED.")

    async def _run_loop(self) -> None:
        try:
            await asyncio.sleep(2.0)  # let the app lifespan finish db init
            await ensure_agent_registry()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - defensive boot guard
            logger.warning("[AgentRuntime] bootstrap registry failed: %s", exc)
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                if not self._running:
                    break
                summary = await self.runtime.run_once()
                active = any(
                    v for k, v in summary.items() if k not in ("recovered",) and v
                )
                if active or summary.get("recovered"):
                    logger.info("[AgentRuntime] pass complete: %s", summary)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - loop must survive pass errors
                logger.error("[AgentRuntime] scheduler pass failed: %s", exc)
                await asyncio.sleep(5.0)


_agent_runtime_instance: Optional[AgentRuntime] = None
_agent_runtime_scheduler_instance: Optional[AgentRuntimeScheduler] = None


def agent_runtime_default() -> AgentRuntime:
    global _agent_runtime_instance
    if _agent_runtime_instance is None:
        _agent_runtime_instance = AgentRuntime()
    return _agent_runtime_instance


def agent_runtime_scheduler_default() -> AgentRuntimeScheduler:
    global _agent_runtime_scheduler_instance
    if _agent_runtime_scheduler_instance is None:
        _agent_runtime_scheduler_instance = AgentRuntimeScheduler()
    return _agent_runtime_scheduler_instance