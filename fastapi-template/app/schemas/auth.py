"""Pydantic schemas for Authentication."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class UserRegisterRequest(BaseModel):
    email: str = Field(..., description="Email address")
    phone: Optional[str] = Field(None, description="Phone number with country code")
    password: str = Field(..., min_length=6)
    full_name: str = "Trader"


class UserLoginRequest(BaseModel):
    email: Optional[str] = None
    identifier: Optional[str] = None  # Accepts either email or phone
    password: str
    remember_me: bool = False


class RequestOtpRequest(BaseModel):
    identifier: str = Field(..., description="Email or phone number")


class VerifyOtpRequest(BaseModel):
    identifier: str
    otp_code: str = Field(..., min_length=4, max_length=8)
    full_name: Optional[str] = "Trader"


class OAuthLoginRequest(BaseModel):
    provider: str = Field(..., description="google or apple")
    token: str = Field(..., description="OAuth ID token or access token")
    email: Optional[str] = None
    full_name: Optional[str] = None


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


class ForgotPasswordRequest(BaseModel):
    identifier: str = Field(..., description="Email or phone number")


class ResetPasswordRequest(BaseModel):
    identifier: str
    otp_code: Optional[str] = None
    reset_token: Optional[str] = None
    new_password: str = Field(..., min_length=6)


class TwoFactorSetupResponse(BaseModel):
    secret: str
    otpauth_url: str


class TwoFactorVerifyRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)


class VerifyRegistrationOtpRequest(BaseModel):
    identifier: str
    otp_code: str = Field(..., min_length=4, max_length=8)


class RegisterResponse(BaseModel):
    success: bool = True
    message: str
    requires_verification: bool = True
    identifier: str
    token_response: Optional[TokenResponse] = None


class UserRead(BaseModel):
    id: str
    email: str
    phone: Optional[str] = None
    full_name: str
    role: str
    is_active: bool
    is_verified: bool = False
    two_factor_enabled: bool = False
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900  # 15 min in seconds
    user: UserRead
    two_factor_required: bool = False
    temp_token: Optional[str] = None
