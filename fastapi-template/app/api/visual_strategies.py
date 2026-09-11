"""Authenticated persistence API for visual option strategies."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.db.session import get_db
from app.models.user import UserRecord
from app.models.visual_strategy import VisualStrategyRecord

router = APIRouter(prefix="/api/visual-strategies", tags=["visual-strategies"])


class VisualStrategyPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    underlying: str = Field(..., min_length=1, max_length=30)
    entry_conditions: list[dict] = Field(default_factory=list)
    exit_conditions: dict = Field(default_factory=dict)
    legs: list[dict] = Field(..., min_length=1)
    is_active: bool = False
    mode: str = "PAPER"


class VisualStrategyPatch(BaseModel):
    """Optional-field update for an existing visual strategy.

    Every field is optional so a partial PATCH (e.g. toggling ``is_active``)
    updates only the caller-supplied knobs.  ``None`` means "leave unchanged",
    so ``entry_conditions``/``exit_conditions``/``legs`` default to None and
    are only written when a concrete list/dict is sent.
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    underlying: str | None = Field(default=None, min_length=1, max_length=30)
    entry_conditions: list[dict] | None = None
    exit_conditions: dict | None = None
    legs: list[dict] | None = None
    is_active: bool | None = None
    mode: str | None = None


def _serialize(row: VisualStrategyRecord) -> dict:
    return {
        "id": row.id, "name": row.name, "underlying": row.underlying,
        "entry_conditions": json.loads(row.entry_conditions or "[]"),
        "exit_conditions": json.loads(row.exit_conditions or "{}"),
        "legs": json.loads(row.legs or "[]"), "is_active": row.is_active, "mode": row.mode,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("", status_code=201)
async def create_visual_strategy(payload: VisualStrategyPayload, user: UserRecord = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = VisualStrategyRecord(user_id=user.id, name=payload.name.strip(), underlying=payload.underlying.upper().strip(), entry_conditions=json.dumps(payload.entry_conditions), exit_conditions=json.dumps(payload.exit_conditions), legs=json.dumps(payload.legs), is_active=payload.is_active, mode=payload.mode.upper())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.get("")
async def list_visual_strategies(user: UserRecord = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(VisualStrategyRecord).where(VisualStrategyRecord.user_id == user.id).order_by(VisualStrategyRecord.created_at.desc()))
    return [_serialize(row) for row in result.scalars().all()]


@router.patch("/{strategy_id}")
async def update_visual_strategy(strategy_id: str, payload: VisualStrategyPatch, user: UserRecord = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Update a saved visual strategy (fields present in the payload only).

    The row lookup is scoped to ``user.id``, so a caller can never patch
    another tenant's strategy.  Returns the updated serialized record.
    """
    row = await db.scalar(select(VisualStrategyRecord).where(VisualStrategyRecord.id == strategy_id, VisualStrategyRecord.user_id == user.id))
    if not row:
        raise HTTPException(status_code=404, detail="Visual strategy not found")

    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.underlying is not None:
        row.underlying = payload.underlying.upper().strip()
    if payload.entry_conditions is not None:
        row.entry_conditions = json.dumps(payload.entry_conditions)
    if payload.exit_conditions is not None:
        row.exit_conditions = json.dumps(payload.exit_conditions)
    if payload.legs is not None:
        row.legs = json.dumps(payload.legs)
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.mode is not None:
        row.mode = payload.mode.upper()

    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.delete("/{strategy_id}", status_code=204)
async def delete_visual_strategy(strategy_id: str, user: UserRecord = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await db.scalar(select(VisualStrategyRecord).where(VisualStrategyRecord.id == strategy_id, VisualStrategyRecord.user_id == user.id))
    if not row:
        raise HTTPException(status_code=404, detail="Visual strategy not found")
    await db.delete(row)
    await db.commit()