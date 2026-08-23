"""Dedicated mobile OTP send endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.engine.otp_service import create_and_dispatch_otp

router = APIRouter(prefix="/api/auth", tags=["mobile-otp"])


class SendMobileOTPRequest(BaseModel):
    phone_number: str = Field(..., min_length=7, max_length=30)


@router.post("/send-otp")
async def send_mobile_otp(payload: SendMobileOTPRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Generate a five-minute OTP and dispatch it through WhatsApp or Telegram."""
    record = await create_and_dispatch_otp(db, payload.phone_number)
    return {"success": True, "phone_number": record.phone_number, "message": "OTP sent. It expires in 5 minutes."}