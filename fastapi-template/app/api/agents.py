"""Admin-gated HTTP surface for the Phase 1 Step 2 autonomous-agent runtime.

All routes require an authenticated administrator (``get_current_admin_user``).
The FastAPI layer is a thin envelope: every mutation funnels through
``app.engine.agent_runtime`` so the fail-closed gates (registry, capability
intersection, approval window, autonomy kill-switch) can never be bypassed.
Deterministic ``AgentDispatchError`` codes are mapped onto HTTP semantics below.
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.admin import get_current_admin_user
from app.config import settings
from app.db.session import SessionLocal
from app.engine.agent_runtime import (
    ERR_AGENT_DISABLED,
    ERR_AGENT_NOT_REGISTERED,
    ERR_APPROVAL_EXPIRED,
    ERR_APPROVAL_NOT_APPLICABLE,
    ERR_APPROVAL_REQUIRED,
    ERR_AUTONOMOUS_MODE_DISABLED,
    ERR_AUTONOMY_LEVEL_EXCEEDED,
    ERR_CAPABILITY_DENIED,
    ERR_INVALID_INPUT,
    ERR_UNSUPPORTED_TASK_KIND,
    AgentDispatchError,
    AgentRuntime,
    agent_definition,
    agent_runtime_default,
    ensure_agent_registry,
    load_runtime_config,
    registered_agent_types,
    supported_task_kinds,
)
from app.models.agent import AgentRecord, AgentTaskRecord
from app.models.user import UserRecord

router = APIRouter(prefix="/api/agents", tags=["agents"])


# ── Request schemas (widen-at-dispatch is impossible; DB stays king) ────────
class RuntimeConfigPatch(BaseModel):
    """Operators may flip the master kill-switch and the global autonomy level."""

    autonomous_mode_enabled: bool = False
    global_autonomy_level: int = Field(default=0, ge=0, le=10)


class TaskDispatchRequest(BaseModel):
    """Durable-task dispatch envelope.

    ``created_by`` is admin-only and defaults to a human admin dispatch
    (``"user"``); the ``"agent"``/``"system"`` creators exist for operator
    verification of the autonomous-mode gate.
    """

    agent_type: str
    task_kind: str
    input: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    timeout_seconds: Optional[float] = Field(default=None, gt=0)
    max_attempts: Optional[int] = Field(default=None, ge=1, le=10)
    created_by: str = "user"


# ── Error mapping: deterministic runtime codes -> HTTP semantics ────────────
_HTTP_404 = {ERR_AGENT_NOT_REGISTERED, ERR_UNSUPPORTED_TASK_KIND}
_HTTP_403 = {
    ERR_AGENT_DISABLED,
    ERR_CAPABILITY_DENIED,
    ERR_AUTONOMOUS_MODE_DISABLED,
    ERR_AUTONOMY_LEVEL_EXCEEDED,
}
_HTTP_409 = {
    ERR_APPROVAL_NOT_APPLICABLE,
    ERR_APPROVAL_REQUIRED,
    ERR_APPROVAL_EXPIRED,
}


def _raise_dispatch_error(exc: AgentDispatchError) -> None:
    """Translate an :class:`AgentDispatchError` into an HTTPException."""
    code = exc.code
    if code == ERR_INVALID_INPUT and "not found" in exc.message:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": exc.message},
        ) from exc
    if code in _HTTP_404:
        status_code = 404
    elif code in _HTTP_403:
        status_code = 403
    elif code in _HTTP_409:
        status_code = 409
    else:
        status_code = 400
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": exc.message},
    ) from exc


# ── Shared helpers ──────────────────────────────────────────────────────────
def _runtime() -> AgentRuntime:
    return agent_runtime_default()


async def _config_snapshot() -> dict[str, Any]:
    """Materialise the singleton runtime-config row as plain JSON values."""
    async with SessionLocal() as db:
        rec = await load_runtime_config(db)
        return {
            "autonomous_mode_enabled": bool(rec.autonomous_mode_enabled),
            "global_autonomy_level": int(rec.global_autonomy_level),
            "updated_by": rec.updated_by,
            "updated_at": (
                rec.updated_at.isoformat(timespec="seconds")
                if rec.updated_at is not None
                else None
            ),
        }


async def _registry_snapshot() -> dict[str, Any]:
    """Merge code definitions with their (never widened) DB registry rows."""
    from sqlalchemy import select

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(AgentRecord).order_by(AgentRecord.agent_type)
        )).scalars().all()
        agents: list[dict[str, Any]] = []
        for row in rows:
            definition = agent_definition(row.agent_type)
            agents.append({
                "agent_type": row.agent_type,
                "name": row.name,
                "description": row.description,
                "capabilities": __import__("json").loads(row.capabilities_json or "[]"),
                "readonly": bool(row.readonly),
                "max_autonomy_level": int(row.max_autonomy_level),
                "enabled": bool(row.enabled),
                "code_definition_present": definition is not None,
                "supported_task_kinds": list(supported_task_kinds(row.agent_type)),
            })
        return {"agents": agents, "total": len(agents)}
# ── Runtime config ──────────────────────────────────────────────────────────
@router.get("/config")
async def get_runtime_config(
    _admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Read the singleton runtime config (kill-switch + global autonomy level)."""
    return await _config_snapshot()


@router.patch("/config")
async def set_runtime_config(
    patch: RuntimeConfigPatch,
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Update the runtime config. The DB row may only TIGHTEN the code envelope,
    so this endpoint can NEVER widen any agent's authority."""
    return await _runtime().update_runtime_config(
        autonomous_mode_enabled=patch.autonomous_mode_enabled,
        global_autonomy_level=int(patch.global_autonomy_level),
        updated_by=str(admin.id),
    )


# ── Registry/agent inventory ────────────────────────────────────────────────
@router.get("")
async def list_agents(
    _admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """List the code-declared agents with their DB registry rows and kinds."""
    return await _registry_snapshot()


@router.post("/registry/sync")
async def sync_registry(
    _admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Idempotently provision DB rows for any code-declared agent missing one."""
    created = await ensure_agent_registry()
    return {"created": created, "total": len(registered_agent_types())}


# ── Task dispatch ───────────────────────────────────────────────────────────
@router.post("/tasks", status_code=201)
async def dispatch_task(
    req: TaskDispatchRequest,
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Enqueue a durable task. Idempotent when ``idempotency_key`` is supplied
    (repeat calls return the existing task with ``created=False``)."""
    try:
        task, created = await _runtime().create_task(
            agent_type=req.agent_type,
            task_kind=req.task_kind,
            input_payload=req.input or {},
            created_by=req.created_by or "user",
            requires_approval=req.requires_approval,
            idempotency_key=req.idempotency_key,
            timeout_seconds=req.timeout_seconds,
            max_attempts=req.max_attempts,
            requested_by=str(admin.id),
        )
    except AgentDispatchError as exc:
        _raise_dispatch_error(exc)
        raise  # pragma: no cover - _raise_dispatch_error always raises
    return {**task, "created": created}


# ── Task read/approval ──────────────────────────────────────────────────────
@router.get("/tasks")
async def list_tasks(
    status_filter: Optional[str] = Query(
        default=None, alias="status", description="PENDING/RUNNING/SUCCEEDED/FAILED"
    ),
    limit: int = Query(default=50, ge=1, le=settings.agent_max_rows_per_scan),
    _admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """List tasks (bounded scan; never an unbounded read)."""
    from sqlalchemy import select

    from app.engine.agent_runtime import task_to_dict

    stmt = select(AgentTaskRecord).order_by(
        AgentTaskRecord.created_at.desc(), AgentTaskRecord.id
    ).limit(limit)
    if status_filter:
        stmt = stmt.where(AgentTaskRecord.status == status_filter.upper())
    async with SessionLocal() as db:
        rows = (await db.execute(stmt)).scalars().all()
    return {"tasks": [task_to_dict(row) for row in rows], "count": len(rows)}


@router.get("/tasks/{task_id}")
async def read_task(
    task_id: str,
    _admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Read a single task by id (includes input/output/error snapshots)."""
    from app.engine.agent_runtime import task_to_dict

    async with SessionLocal() as db:
        row = await db.get(AgentTaskRecord, task_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "task not found"}
            )
        return task_to_dict(row)


@router.post("/tasks/{task_id}/approve")
async def approve_task(
    task_id: str,
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Grant/refresh the human approval on a ``requires_approval`` task."""
    try:
        return await _runtime().approve_task(task_id, approved_by=str(admin.id))
    except AgentDispatchError as exc:
        _raise_dispatch_error(exc)
        raise  # pragma: no cover


@router.post("/tasks/{task_id}/revoke-approval")
async def revoke_task_approval(
    task_id: str,
    admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Clear the human approval; an unapproved approval-required task can never
    be claimed again until re-approved (fail-closed)."""
    try:
        return await _runtime().revoke_approval(task_id, revoked_by=str(admin.id))
    except AgentDispatchError as exc:
        _raise_dispatch_error(exc)
        raise  # pragma: no cover


# ── Operator smoke-pass ─────────────────────────────────────────────────────
@router.post("/run")
async def run_scheduler_pass(
    _admin: UserRecord = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Execute ONE scheduler pass on demand (idempotent; bounded work)."""
    return await _runtime().run_once()