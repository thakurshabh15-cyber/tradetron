"""TradeThrone Creator Payout API — marketplace revenue distribution settings.

Endpoints
---------
GET /api/payouts/settings   — current payout configuration (masked)
PUT /api/payouts/settings   — create/update bank or UPI destination
GET /api/payouts/summary    — revenue-split economics for the signed-in creator
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.db.session import get_db
from app.engine.subscription import CREATOR_REVENUE_SHARE, PLATFORM_REVENUE_SHARE
from app.models.marketplace import CreatorPayoutSettingsRecord
from app.models.user import UserRecord

router = APIRouter(prefix="/api/payouts", tags=["payouts"])

_UPI_RE = re.compile(r"^[\w.\-]{2,}@[a-zA-Z]{2,}$")
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


class PayoutSettingsUpdate(BaseModel):
    payout_method: str = Field(..., pattern="^(BANK|UPI)$")
    account_holder_name: str = Field("", max_length=100)
    bank_name: str = Field("", max_length=100)
    account_number: str | None = Field(None, description="Write-only — stored AES-256 encrypted")
    ifsc_code: str = Field("", max_length=15)
    upi_id: str = Field("", max_length=100)
    gstin: str = Field("", max_length=20)


def _serialize(rec: CreatorPayoutSettingsRecord) -> dict:
    return {
        "payout_method": rec.payout_method,
        "account_holder_name": rec.account_holder_name,
        "bank_name": rec.bank_name,
        "account_number_masked": rec.masked_account_number(),
        "ifsc_code": rec.ifsc_code,
        "upi_id": rec.upi_id,
        "gstin": rec.gstin,
        "is_verified": rec.is_verified,
        "is_configured": bool(
            rec.upi_id if rec.payout_method == "UPI"
            else (rec.account_number_encrypted and rec.ifsc_code)
        ),
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


@router.get("/settings")
async def get_payout_settings(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(CreatorPayoutSettingsRecord).where(
        CreatorPayoutSettingsRecord.user_id == user.id
    )
    rec = (await db.execute(stmt)).scalar_one_or_none()
    if not rec:
        return {"configured": False, "payout_method": "UPI", "is_configured": False}
    return {"configured": True, **_serialize(rec)}


@router.put("/settings")
async def update_payout_settings(
    req: PayoutSettingsUpdate,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Destination-specific validation before touching the vault
    if req.payout_method == "UPI":
        if not _UPI_RE.match(req.upi_id.strip()):
            from fastapi import HTTPException

            raise HTTPException(422, detail="Invalid UPI ID format (expected name@bank)")
    else:
        missing = [
            label for label, value in (
                ("bank_name", req.bank_name.strip()),
                ("account_number", (req.account_number or "").strip()),
                ("ifsc_code", req.ifsc_code.strip().upper()),
            ) if not value
        ]
        if missing:
            from fastapi import HTTPException

            raise HTTPException(422, detail=f"Missing required BANK fields: {', '.join(missing)}")
        if not _IFSC_RE.match(req.ifsc_code.strip().upper()):
            from fastapi import HTTPException

            raise HTTPException(422, detail="Invalid IFSC code (expected e.g. HDFC0001234)")

    stmt = select(CreatorPayoutSettingsRecord).where(
        CreatorPayoutSettingsRecord.user_id == user.id
    )
    rec = (await db.execute(stmt)).scalar_one_or_none()
    if not rec:
        rec = CreatorPayoutSettingsRecord(user_id=user.id)
        db.add(rec)

    rec.payout_method = req.payout_method
    rec.account_holder_name = req.account_holder_name.strip()
    rec.bank_name = req.bank_name.strip()
    rec.ifsc_code = req.ifsc_code.strip().upper()
    rec.upi_id = req.upi_id.strip() if req.payout_method == "UPI" else ""
    if req.payout_method == "BANK" and req.account_number:
        rec.set_account_number(req.account_number.strip())
    rec.updated_at = rec.updated_at  # onupdate handles timestamp
    await db.commit()
    await db.refresh(rec)
    return {"success": True, **_serialize(rec)}


@router.get("/summary")
async def payout_summary(user: UserRecord = Depends(get_current_user)):
    """Revenue-split economics shown on the creator dashboard."""
    return {
        "creator_share_pct": round(CREATOR_REVENUE_SHARE * 100, 1),
        "platform_fee_pct": round(PLATFORM_REVENUE_SHARE * 100, 1),
        "example": {
            "subscriber_pays_inr": 1499,
            "creator_earns_inr": round(1499 * CREATOR_REVENUE_SHARE, 2),
            "platform_fee_inr": round(1499 * PLATFORM_REVENUE_SHARE, 2),
        },
        "note": "Settlement runs monthly to your configured UPI/bank destination after KYC verification.",
    }