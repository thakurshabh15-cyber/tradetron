"""User Onboarding and Setup Status Endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/user", tags=["user"])


class SetupTask(BaseModel):
    """Status details for a single onboarding setup step."""

    id: str
    title: str
    status: Literal["Complete", "Pending"]
    description: str
    completed_at: Optional[str] = None


class SetupStatusResponse(BaseModel):
    """Aggregate setup status response."""

    marketplace_setup: SetupTask
    broker_setup: SetupTask
    subscription_setup: SetupTask
    tasks: list[SetupTask]
    completed_count: int
    total_count: int
    overall_progress_pct: int


class UpdateSetupTaskRequest(BaseModel):
    """Payload to toggle or update a specific setup task."""

    task_id: Literal["marketplace_setup", "broker_setup", "subscription_setup"]
    status: Literal["Complete", "Pending"]


# In-memory persistent state for onboarding tasks
_SETUP_STATE = {
    "marketplace_setup": {
        "id": "marketplace_setup",
        "title": "Marketplace Setup",
        "status": "Complete",
        "description": "Subscribe to pre-built algo trading strategies from the community marketplace.",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    },
    "broker_setup": {
        "id": "broker_setup",
        "title": "Broker Setup",
        "status": "Complete",
        "description": "Connect Angel One or Simulated paper trading account with API credentials.",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    },
    "subscription_setup": {
        "id": "subscription_setup",
        "title": "Subscription Setup",
        "status": "Pending",
        "description": "Activate Tradetron Pro membership for high-frequency execution and live deployment.",
        "completed_at": None,
    },
}


def _build_response() -> SetupStatusResponse:
    tasks = [SetupTask(**_SETUP_STATE[key]) for key in ["marketplace_setup", "broker_setup", "subscription_setup"]]
    completed = sum(1 for t in tasks if t.status == "Complete")
    total = len(tasks)
    pct = int((completed / total) * 100) if total > 0 else 0

    return SetupStatusResponse(
        marketplace_setup=SetupTask(**_SETUP_STATE["marketplace_setup"]),
        broker_setup=SetupTask(**_SETUP_STATE["broker_setup"]),
        subscription_setup=SetupTask(**_SETUP_STATE["subscription_setup"]),
        tasks=tasks,
        completed_count=completed,
        total_count=total,
        overall_progress_pct=pct,
    )


@router.get("/setup-status", response_model=SetupStatusResponse)
async def get_setup_status():
    """Retrieve the user's setup status for Marketplace, Broker, and Subscription."""
    return _build_response()


@router.patch("/setup-status", response_model=SetupStatusResponse)
async def update_setup_status(req: UpdateSetupTaskRequest):
    """Update or toggle a setup task status (Pending <-> Complete)."""
    if req.task_id not in _SETUP_STATE:
        raise HTTPException(status_code=404, detail="Task not found")

    _SETUP_STATE[req.task_id]["status"] = req.status
    if req.status == "Complete":
        _SETUP_STATE[req.task_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
    else:
        _SETUP_STATE[req.task_id]["completed_at"] = None

    return _build_response()


# ── User Profile & Notification Preferences ──────────────────────────────────
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from app.api.auth import get_current_user
from app.db.session import get_db
from app.models.user import UserRecord
from app.models.notification import NotificationPreferenceRecord


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = None
    profile_photo: Optional[str] = None  # Base64 data URI or image string


class NotificationPreferencesSchema(BaseModel):
    email_enabled: bool = True
    email_address: Optional[str] = None
    telegram_enabled: bool = False
    telegram_chat_id: Optional[str] = None
    push_enabled: bool = True
    order_executed_notify: bool = True
    trade_closed_notify: bool = True
    sl_tp_trigger_notify: bool = True
    price_alert_notify: bool = True


@router.get("/profile")
async def get_profile(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch authenticated user profile information directly from database."""
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name or user.email.split("@")[0].capitalize(),
        "role": user.role,
        "profile_photo": user.profile_photo,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.put("/profile")
async def update_profile(
    req: UpdateProfileRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update authenticated user profile attributes including full name and base64 profile photo."""
    if req.full_name is not None:
        user.full_name = req.full_name
    if req.profile_photo is not None:
        user.profile_photo = req.profile_photo

    await db.commit()
    await db.refresh(user)

    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "profile_photo": user.profile_photo,
    }


@router.get("/notifications")
async def get_notifications(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch authenticated user notification preferences (Telegram, email, push)."""
    stmt = select(NotificationPreferenceRecord).where(
        NotificationPreferenceRecord.user_id == user.id
    )
    res = await db.execute(stmt)
    prefs = res.scalar_one_or_none()

    if not prefs:
        prefs = NotificationPreferenceRecord(
            user_id=user.id,
            email_enabled=True,
            email_address=user.email,
            telegram_enabled=False,
            telegram_chat_id="",
            push_enabled=True,
        )
        db.add(prefs)
        await db.commit()
        await db.refresh(prefs)

    return {
        "id": prefs.id,
        "email_enabled": prefs.email_enabled,
        "email_address": prefs.email_address or user.email,
        "telegram_enabled": prefs.telegram_enabled,
        "telegram_chat_id": prefs.telegram_chat_id,
        "push_enabled": prefs.push_enabled,
        "order_executed_notify": prefs.order_executed_notify,
        "trade_closed_notify": prefs.trade_closed_notify,
        "sl_tp_trigger_notify": prefs.sl_tp_trigger_notify,
        "price_alert_notify": prefs.price_alert_notify,
    }


@router.put("/notifications")
async def update_notifications(
    req: NotificationPreferencesSchema,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update authenticated user notification preferences for Telegram, Email, and Web Push."""
    stmt = select(NotificationPreferenceRecord).where(
        NotificationPreferenceRecord.user_id == user.id
    )
    res = await db.execute(stmt)
    prefs = res.scalar_one_or_none()

    if not prefs:
        prefs = NotificationPreferenceRecord(
            user_id=user.id,
            **req.model_dump(),
        )
        db.add(prefs)
    else:
        for k, v in req.model_dump().items():
            setattr(prefs, k, v)

    await db.commit()
    await db.refresh(prefs)

    return {
        "id": prefs.id,
        "email_enabled": prefs.email_enabled,
        "email_address": prefs.email_address,
        "telegram_enabled": prefs.telegram_enabled,
        "telegram_chat_id": prefs.telegram_chat_id,
        "push_enabled": prefs.push_enabled,
        "order_executed_notify": prefs.order_executed_notify,
        "trade_closed_notify": prefs.trade_closed_notify,
        "sl_tp_trigger_notify": prefs.sl_tp_trigger_notify,
        "price_alert_notify": prefs.price_alert_notify,
    }


# ── KYC Compliance & Document Verification ───────────────────────────────────
import re


class KYCSubmissionRequest(BaseModel):
    pan_number: str = Field(..., description="Indian Income Tax PAN (e.g. ABCDE1234F)")
    id_proof_type: str = Field("PAN_CARD", description="PAN_CARD, AADHAAR, PASSPORT, VOTER_ID, DRIVING_LICENSE")
    id_proof_doc: str = Field(..., description="Base64 encoded image or document URI")


@router.get("/kyc")
async def get_kyc_status(
    user: UserRecord = Depends(get_current_user),
):
    """Retrieve the authenticated user's real KYC verification status and documents."""
    return {
        "user_id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "kyc_status": user.kyc_status,
        "pan_number": user.pan_number,
        "id_proof_type": user.id_proof_type,
        "has_id_proof_doc": bool(user.id_proof_doc),
        "kyc_submitted_at": user.kyc_submitted_at.isoformat() if user.kyc_submitted_at else None,
        "kyc_rejection_reason": user.kyc_rejection_reason,
    }


@router.post("/kyc/submit")
async def submit_kyc(
    req: KYCSubmissionRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit PAN and ID document proof for regulatory SEBI KYC compliance."""
    pan_clean = req.pan_number.strip().upper()
    pan_regex = r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$"
    if not re.match(pan_regex, pan_clean):
        raise HTTPException(
            status_code=400,
            detail="Invalid PAN format. PAN must be exactly 10 alphanumeric characters (e.g. ABCDE1234F).",
        )

    if not req.id_proof_doc or len(req.id_proof_doc) < 20:
        raise HTTPException(
            status_code=400,
            detail="Invalid ID document proof. Please upload a valid document or image.",
        )

    user.pan_number = pan_clean
    user.id_proof_type = req.id_proof_type.strip().upper()
    user.id_proof_doc = req.id_proof_doc
    user.kyc_status = "PENDING"
    user.kyc_submitted_at = datetime.now(timezone.utc)
    user.kyc_rejection_reason = None

    await db.commit()
    await db.refresh(user)

    from app.core.audit import log_audit_event
    await log_audit_event(
        db=db,
        action="KYC_SUBMITTED",
        resource_type="USER_KYC",
        user_id=user.id,
        status="PENDING",
        details={"pan_number": f"{pan_clean[:2]}***{pan_clean[-2:]}", "id_proof_type": user.id_proof_type},
    )

    return {
        "success": True,
        "message": "KYC documents submitted successfully. Status is now PENDING review.",
        "kyc_status": "PENDING",
        "pan_number": pan_clean,
        "id_proof_type": user.id_proof_type,
        "kyc_submitted_at": user.kyc_submitted_at.isoformat(),
    }

