"""Tenant-scoped HTTP surface for the Phase 1 Step 4 autonomous-agent control plane.

The Agent Console in the browser is a CONTROL PLANE, never an execution
authority.  Every mutation on this router funnels through
``app.engine.agent_control.AgentControlService`` so the fail-closed gates
(config validation, ownership, LIVE broker-mode gating, CAS lifecycle
transitions, owner-only approval) can never be bypassed by a client.

Security model:
- all routes require an authenticated, active user (``get_current_user``);
- ``user_id`` is ALWAYS taken from the verified token — never from the request
  body, so IDOR / cross-tenant writes are structurally impossible;
- the service re-verifies row ownership on every read/mutation;
- deterministic ``AgentControlError`` codes are mapped to HTTP below.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import get_current_user
from app.engine.agent_control import (
    ERR_BAD_AUTONOMY,
    ERR_BAD_MODE,
    ERR_BAD_POLICY,
    ERR_BAD_SYMBOLS,
    ERR_CONFIG_EXISTS,
    ERR_CONFIG_NOT_FOUND,
    ERR_ILLEGAL_TRANSITION,
    ERR_INVALID_STATE,
    ERR_OWNER_MISMATCH,
    ERR_STRATEGY_NOT_FOUND,
    ERR_STRATEGY_OWNERSHIP,
    ERR_TASK_NOT_APPROVABLE,
    AgentControlError,
    agent_control_service,
)
from app.models.user import UserRecord

router = APIRouter(prefix="/api/agent", tags=["agent-control"])


# ── Request schemas (server re-validates every bound; never widened) ────────
class AgentConfigCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    strategy_id: Optional[str] = None
    symbols: list[str] = Field(min_length=1, max_length=40)
    execution_mode: str = "PAPER"
    autonomy_level: int = Field(default=0, ge=0, le=3)
    approval_policy: dict[str, Any] = Field(default_factory=dict)
    risk_policy: dict[str, Any] = Field(default_factory=dict)


class AgentConfigPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    strategy_id: Optional[str] = None
    symbols: Optional[list[str]] = Field(default=None, min_length=1, max_length=40)
    execution_mode: Optional[str] = None
    autonomy_level: Optional[int] = Field(default=None, ge=0, le=3)
    approval_policy: Optional[dict[str, Any]] = None
    risk_policy: Optional[dict[str, Any]] = None
# ── Error mapping: deterministic control-plane codes -> HTTP semantics ──────
_HTTP_404 = {ERR_CONFIG_NOT_FOUND, ERR_STRATEGY_NOT_FOUND, ERR_TASK_NOT_APPROVABLE}
_HTTP_403 = {ERR_STRATEGY_OWNERSHIP, ERR_OWNER_MISMATCH}
_HTTP_409 = {ERR_CONFIG_EXISTS, ERR_ILLEGAL_TRANSITION}
_HTTP_400 = {
    ERR_BAD_SYMBOLS,
    ERR_BAD_MODE,
    ERR_BAD_AUTONOMY,
    ERR_BAD_POLICY,
    ERR_INVALID_STATE,
}


def _raise_control_error(exc: AgentControlError) -> None:
    if exc.code in _HTTP_404:
        status_code = 404
    elif exc.code in _HTTP_403:
        status_code = 403
    elif exc.code in _HTTP_409:
        status_code = 409
    else:
        status_code = 400
    raise HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


def _user_id(user: UserRecord) -> str:
    return str(user.id)


# ── Console bundle: ONE tenant-scoped request for the whole page ────────────
@router.get("/console")
async def agent_console(user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    """Everything the Agent Console needs in one snapshot (no N+1)."""
    return await agent_control_service.bundle(_user_id(user))


# ── Configuration CRUD (server-authoritative; browser is only a control) ────
@router.post("/config")
async def create_agent_config(
    req: AgentConfigCreate,
    user: UserRecord = Depends(get_current_user),
) -> dict[str, Any]:
    """Create the single per-tenant agent configuration (idempotent per user)."""
    try:
        return await agent_control_service.create_config(
            _user_id(user),
            name=req.name,
            strategy_id=req.strategy_id or None,
            symbols=req.symbols,
            execution_mode=req.execution_mode,
            autonomy_level=req.autonomy_level,
            approval_policy=req.approval_policy,
            risk_policy=req.risk_policy,
        )
    except AgentControlError as exc:
        _raise_control_error(exc)
        raise  # pragma: no cover - _raise_control_error always raises


@router.patch("/config")
async def update_agent_config(
    req: AgentConfigPatch,
    user: UserRecord = Depends(get_current_user),
) -> dict[str, Any]:
    """Patch the tenant's agent config (refused while RUNNING — fail closed)."""
    config = await agent_control_service.get_config(_user_id(user))
    if config is None:
        _raise_control_error(
            AgentControlError(ERR_CONFIG_NOT_FOUND, "agent config not found")
        )
    try:
        return await agent_control_service.patch_config(
            config.id,
            _user_id(user),
            name=req.name,
            strategy_id=req.strategy_id,
            symbols=req.symbols,
            execution_mode=req.execution_mode,
            autonomy_level=req.autonomy_level,
            approval_policy=req.approval_policy,
            risk_policy=req.risk_policy,
        )
    except AgentControlError as exc:
        _raise_control_error(exc)
        raise  # pragma: no cover
# ── Lifecycle: CAS-protected transitions handled by the engine ──────────────
async def _require_config(user_id: str):
    config = await agent_control_service.get_config(user_id)
    if config is None:
        _raise_control_error(AgentControlError(ERR_CONFIG_NOT_FOUND, "agent config not found"))
    return config


@router.post("/config/start")
async def start_agent(user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    config = await _require_config(_user_id(user))
    try:
        return await agent_control_service.start(config.id, _user_id(user))
    except AgentControlError as exc:
        _raise_control_error(exc)
        raise  # pragma: no cover


@router.post("/config/pause")
async def pause_agent(user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    config = await _require_config(_user_id(user))
    try:
        return await agent_control_service.pause(config.id, _user_id(user))
    except AgentControlError as exc:
        _raise_control_error(exc)
        raise  # pragma: no cover


@router.post("/config/resume")
async def resume_agent(user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    config = await _require_config(_user_id(user))
    try:
        return await agent_control_service.resume(config.id, _user_id(user))
    except AgentControlError as exc:
        _raise_control_error(exc)
        raise  # pragma: no cover


@router.post("/config/stop")
async def stop_agent(user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    config = await _require_config(_user_id(user))
    try:
        return await agent_control_service.stop(config.id, _user_id(user))
    except AgentControlError as exc:
        _raise_control_error(exc)
        raise  # pragma: no cover


# ── Human approval (owner-scoped; the runtime CAS gates still apply) ────────
@router.post("/tasks/{task_id}/approve")
async def approve_agent_task(
    task_id: str,
    user: UserRecord = Depends(get_current_user),
) -> dict[str, Any]:
    """Approve an agent task; only the owning tenant may approve it."""
    try:
        return await agent_control_service.approve_task(task_id, _user_id(user))
    except AgentControlError as exc:
        _raise_control_error(exc)
        raise  # pragma: no cover