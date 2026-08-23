"""Production Authentication Router: JWT, Rate-Limiting, Account Lockout, 2FA, Token Revocation & Password Reset."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    check_rate_limit,
    create_access_token,
    create_password_reset_token,
    create_refresh_token,
    decode_token,
    generate_otp_for_identifier,
    generate_totp_secret,
    generate_totp_uri,
    hash_password,
    hash_token,
    verify_otp_for_identifier,
    verify_password,
    verify_password_reset_token,
    verify_totp_code,
)
from app.core.notifications import dispatch_otp
from app.db.session import get_db
from app.models.user import RevokedTokenRecord, UserRecord
from app.schemas.auth import (
    ForgotPasswordRequest,
    LogoutRequest,
    OAuthLoginRequest,
    RefreshTokenRequest,
    RequestOtpRequest,
    ResetPasswordRequest,
    TokenResponse,
    TwoFactorSetupResponse,
    TwoFactorVerifyRequest,
    UserLoginRequest,
    UserRead,
    UserRegisterRequest,
    VerifyOtpRequest,
    VerifyRegistrationOtpRequest,
)

logger = get_logger("api.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _user_to_read(u: UserRecord) -> UserRead:
    return UserRead(
        id=u.id,
        email=u.email,
        phone=u.phone,
        full_name=u.full_name,
        role=u.role,
        is_active=u.is_active,
        is_verified=getattr(u, "is_verified", True),
        two_factor_enabled=u.two_factor_enabled,
        created_at=u.created_at,
    )


def _generate_token_response(user: UserRecord, remember_me: bool = False) -> TokenResponse:
    token_payload = {"sub": user.id, "email": user.email, "role": user.role}
    access_tok = create_access_token(token_payload)
    refresh_tok = create_refresh_token(token_payload, remember_me=remember_me)
    return TokenResponse(
        access_token=access_tok,
        refresh_token=refresh_tok,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=_user_to_read(user),
        two_factor_required=False,
    )


async def get_current_user(
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> UserRecord:
    """Dependency to extract authenticated user from Bearer token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1]
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    stmt = select(UserRecord).where(UserRecord.id == user_id)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


# ── SIGNUP / REGISTRATION ────────────────────────────────────────────────────
@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    req: UserRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Register a new account with email/phone and password."""
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"reg:{client_ip}", max_requests=10, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many registration requests. Please wait.")

    email_clean = req.email.strip().lower()
    phone_clean = req.phone.strip() if req.phone else None

    # Check for existing email or phone
    stmt = select(UserRecord).where(
        or_(
            UserRecord.email == email_clean,
            (UserRecord.phone == phone_clean) if phone_clean else False,
        )
    )
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email address or phone number is already registered",
        )

    from app.config import settings

    requires_verification = settings.require_registration_verification
    user = UserRecord(
        email=email_clean,
        phone=phone_clean,
        hashed_password=hash_password(req.password),
        full_name=req.full_name.strip() or "Trader",
        is_active=True,
        is_verified=not requires_verification,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Dispatch real OTP verification email/SMS
    otp_code = generate_otp_for_identifier(user.email)
    dispatch_res = await dispatch_otp(user.email, otp_code, purpose="registration")
    logger.info(
        "New user registered: %s (%s) — Verification OTP dispatched: %s (via %s)",
        user.email,
        user.id,
        dispatch_res.get("dispatched"),
        dispatch_res.get("provider"),
    )

    return _generate_token_response(user)


@router.post("/verify-registration", response_model=TokenResponse)
async def verify_registration(
    req: VerifyRegistrationOtpRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verify registration OTP code and activate the user account."""
    ident = req.identifier.strip().lower()
    if not verify_otp_for_identifier(ident, req.otp_code.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired registration verification code",
        )

    stmt = select(UserRecord).where(
        or_(UserRecord.email == ident, UserRecord.phone == ident)
    )
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User account not found")

    user.is_verified = True
    user.is_active = True
    await db.commit()
    await db.refresh(user)

    logger.info("User account successfully verified and activated: %s", user.email)
    return _generate_token_response(user)


# ── LOGIN WITH EMAIL/PHONE, REMEMBER ME & ACCOUNT LOCKOUT ────────────────────
@router.post("/login", response_model=TokenResponse)
async def login(
    req: UserLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate with email or phone + password, with lockout protection and 2FA trigger."""
    client_ip = request.client.host if request.client else "unknown"
    ident = (req.identifier or req.email or "").strip().lower()

    # 1. Rate limiting check
    if not check_rate_limit(f"login:{client_ip}:{ident}", max_requests=8, window_seconds=60):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please wait 60 seconds.",
        )

    # 2. Look up user by email or phone
    stmt = select(UserRecord).where(
        or_(
            UserRecord.email == ident,
            UserRecord.phone == ident,
        )
    )
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    # 3. Check for Account Lockout
    now = _utcnow()
    if user and user.locked_until:
        if user.locked_until > now:
            minutes_left = int((user.locked_until - now).total_seconds() / 60) + 1
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail=f"Account temporarily locked due to repeated failed attempts. Try again in {minutes_left} minutes.",
            )
        else:
            # Lockout expired
            user.locked_until = None
            user.failed_login_attempts = 0

    # 4. Verify Password
    if not user or not user.hashed_password or not verify_password(req.password, user.hashed_password):
        if user:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= 5:
                user.locked_until = now + timedelta(minutes=15)
                await db.commit()
                logger.warning("ACCOUNT LOCKED: %s after %d failed attempts", user.email, user.failed_login_attempts)
                raise HTTPException(
                    status_code=status.HTTP_423_LOCKED,
                    detail="Account temporarily locked for 15 minutes due to 5 consecutive failed attempts.",
                )
            await db.commit()

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email/phone or password",
        )

    # Reset failed attempts on success
    user.failed_login_attempts = 0
    user.locked_until = None
    await db.commit()

    # 5. Handle 2FA Authenticator Challenge if Enabled
    if user.two_factor_enabled:
        temp_payload = {"sub": user.id, "email": user.email, "type": "2fa_pending"}
        temp_token = create_access_token(temp_payload, expires_delta=timedelta(minutes=5))
        return TokenResponse(
            access_token="",
            refresh_token="",
            token_type="bearer",
            expires_in=300,
            user=_user_to_read(user),
            two_factor_required=True,
            temp_token=temp_token,
        )

    logger.info("User logged in successfully: %s", user.email)
    return _generate_token_response(user, remember_me=req.remember_me)


# ── LOGOUT & SERVER-SIDE TOKEN INVALIDATION ──────────────────────────────────
@router.post("/logout")
async def logout(
    req: LogoutRequest,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Server-side logout invalidating refresh token and current session."""
    token_to_revoke = req.refresh_token
    if not token_to_revoke and authorization and authorization.startswith("Bearer "):
        token_to_revoke = authorization.split(" ", 1)[1]

    if token_to_revoke:
        thash = hash_token(token_to_revoke)
        payload = decode_token(token_to_revoke)
        user_id = payload.get("sub") if payload else None
        exp_timestamp = payload.get("exp", int(datetime.now(timezone.utc).timestamp()) + 86400) if payload else int(datetime.now(timezone.utc).timestamp()) + 86400
        exp_dt = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)

        revocation = RevokedTokenRecord(
            token_hash=thash,
            user_id=user_id,
            expires_at=exp_dt,
        )
        db.add(revocation)
        try:
            await db.commit()
        except Exception:
            pass  # Already revoked

    logger.info("User session logged out and token invalidated server-side")
    return {"success": True, "message": "Logged out successfully"}


# ── TOKEN ROTATION & REFRESH ─────────────────────────────────────────────────
@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    req: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a valid refresh token for a new access token with Token Rotation."""
    # 1. Check if token was revoked server-side
    thash = hash_token(req.refresh_token)
    stmt = select(RevokedTokenRecord).where(RevokedTokenRecord.token_hash == thash)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )

    # 2. Decode and verify payload
    payload = decode_token(req.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    user_id = payload.get("sub")
    stmt_user = select(UserRecord).where(UserRecord.id == user_id)
    res_user = await db.execute(stmt_user)
    user = res_user.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    # 3. Rotate Refresh Token: Revoke old one
    exp_dt = datetime.fromtimestamp(payload.get("exp", int(datetime.now(timezone.utc).timestamp()) + 86400), tz=timezone.utc)
    revocation = RevokedTokenRecord(
        token_hash=thash,
        user_id=user.id,
        expires_at=exp_dt,
    )
    db.add(revocation)
    await db.commit()

    return _generate_token_response(user)


# ── PASSWORD RESET FLOW (OTP / SIGNED TOKEN) ─────────────────────────────────
@router.post("/forgot-password")
async def forgot_password(
    req: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Initiate password reset flow generating 15-minute verification OTP code."""
    ident = req.identifier.strip().lower()
    client_ip = request.client.host if request.client else "unknown"

    if not check_rate_limit(f"pwd_reset:{client_ip}:{ident}", max_requests=4, window_seconds=300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password reset requests. Please wait 5 minutes.",
        )

    stmt = select(UserRecord).where(or_(UserRecord.email == ident, UserRecord.phone == ident))
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user:
        # Don't leak user existence; return generic message
        return {
            "success": True,
            "message": f"If an account exists for {req.identifier}, a password reset code has been sent.",
        }

    otp_code = generate_otp_for_identifier(user.email)
    reset_token = create_password_reset_token(user.id, user.email)
    dispatch_res = await dispatch_otp(user.email, otp_code, purpose="password_reset")
    if not dispatch_res.get("dispatched") and settings.resend_api_key:
        err_msg = dispatch_res.get("message") or "Failed to deliver password reset email."
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Email delivery failed: {err_msg}",
        )

    logger.info(
        "Password reset OTP generated for %s (dispatched: %s via %s)",
        user.email,
        dispatch_res.get("dispatched"),
        dispatch_res.get("provider"),
    )

    return {
        "success": True,
        "message": f"Password reset verification code sent to {req.identifier}",
        "reset_token": reset_token,
    }


@router.post("/reset-password")
async def reset_password(
    req: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Complete password reset using verified OTP or signed reset token."""
    ident = req.identifier.strip().lower()
    stmt = select(UserRecord).where(or_(UserRecord.email == ident, UserRecord.phone == ident))
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User account not found")

    is_valid = False
    if req.otp_code and verify_otp_for_identifier(user.email, req.otp_code.strip()):
        is_valid = True
    elif req.reset_token:
        payload = verify_password_reset_token(req.reset_token)
        if payload and payload.get("sub") == user.id:
            is_valid = True

    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid or expired reset code / token")

    # Update password and reset lockout
    user.hashed_password = hash_password(req.new_password)
    user.failed_login_attempts = 0
    user.locked_until = None
    await db.commit()

    logger.info("Password successfully reset for user: %s", user.email)
    return {"success": True, "message": "Password updated successfully. Please log in with your new password."}


# ── 2FA TWO-FACTOR AUTHENTICATION SETUP & VERIFY ─────────────────────────────
@router.post("/2fa/setup", response_model=TwoFactorSetupResponse)
async def setup_2fa(user: UserRecord = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Generate TOTP secret and authenticator URI for QR code pairing."""
    secret = generate_totp_secret()
    user.totp_secret = secret
    await db.commit()

    uri = generate_totp_uri(secret, user.email)
    return TwoFactorSetupResponse(secret=secret, otpauth_url=uri)


@router.post("/2fa/verify")
async def verify_and_enable_2fa(
    req: TwoFactorVerifyRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify 6-digit TOTP code and activate 2FA on the account."""
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="2FA has not been initiated. Call /2fa/setup first.")

    if not verify_totp_code(user.totp_secret, req.code):
        raise HTTPException(status_code=400, detail="Invalid 6-digit authenticator code")

    user.two_factor_enabled = True
    await db.commit()
    return {"success": True, "message": "Two-factor authentication enabled successfully"}


@router.post("/2fa/toggle")
async def toggle_2fa(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle 2FA state on or off."""
    user.two_factor_enabled = not user.two_factor_enabled
    await db.commit()
    return {"success": True, "two_factor_enabled": user.two_factor_enabled}


# ── OTP & OAUTH AUTHENTICATION (PRESERVED) ────────────────────────────────────
@router.post("/request-otp")
async def request_otp(req: RequestOtpRequest, request: Request):
    """Generate and dispatch OTP for user authentication."""
    identifier = req.identifier.strip().lower()
    if "@" not in identifier:
        raise HTTPException(status_code=400, detail="Email address is required for OTP login")
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"otp-cooldown:{client_ip}:{identifier}", max_requests=1, window_seconds=60):
        raise HTTPException(status_code=429, detail="Please wait 60 seconds before requesting another OTP")

    otp_code = generate_otp_for_identifier(identifier)
    from app.engine.email_service import send_otp_email
    dispatch_res = await send_otp_email(identifier, otp_code)
    if not dispatch_res.get("dispatched") and settings.resend_api_key:
        err_msg = dispatch_res.get("message") or "Failed to deliver OTP email via Resend."
        logger.error("OTP email delivery failure for %s: %s", identifier, err_msg)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Email delivery failed: {err_msg}",
        )

    logger.info(
        "OTP generated for %s (dispatched: %s via %s)",
        identifier,
        dispatch_res.get("dispatched"),
        dispatch_res.get("provider"),
    )

    return {
        "success": True,
        "identifier": identifier,
        "message": f"Verification code sent to {identifier}",
    }


@router.post("/resend-otp")
async def resend_otp(req: RequestOtpRequest, request: Request):
    """Resend an email OTP with a 60-second cooldown."""
    identifier = req.identifier.strip().lower()
    if "@" not in identifier:
        raise HTTPException(status_code=400, detail="Email address is required for OTP login")
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"otp-cooldown:{client_ip}:{identifier}", max_requests=1, window_seconds=60):
        raise HTTPException(status_code=429, detail="Please wait 60 seconds before requesting another OTP")
    otp_code = generate_otp_for_identifier(identifier)
    from app.engine.email_service import send_otp_email
    dispatch_res = await send_otp_email(identifier, otp_code)
    if not dispatch_res.get("dispatched") and settings.resend_api_key:
        raise HTTPException(status_code=502, detail="Email delivery failed. Please try again later.")
    return {"success": True, "identifier": identifier, "message": f"Verification code resent to {identifier}"}


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(
    req: VerifyOtpRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verify 6-digit OTP code and authenticate user."""
    identifier_clean = req.identifier.strip().lower()
    if "@" not in identifier_clean:
        raise HTTPException(status_code=400, detail="Email address is required for OTP login")
    if not verify_otp_for_identifier(identifier_clean, req.otp_code.strip()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP code")

    stmt = select(UserRecord).where(or_(UserRecord.email == identifier_clean, UserRecord.phone == identifier_clean))
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user:
        user = UserRecord(
            email=identifier_clean,
            hashed_password=None,
            full_name=req.full_name or "Trader",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return _generate_token_response(user)


@router.post("/oauth", response_model=TokenResponse)
async def oauth_login(
    req: OAuthLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate or register via Google / Apple OAuth with ID token verification."""
    provider = req.provider.lower()
    if provider not in ("google", "apple"):
        raise HTTPException(status_code=400, detail=f"Unsupported OAuth provider: {req.provider}")

    from app.config import settings as app_settings

    # ── Google OAuth: verify ID token with Google's public keys ──
    if provider == "google":
        if not app_settings.google_oauth_client_id:
            logger.warning("GOOGLE_OAUTH_CLIENT_ID not configured — OAuth login rejected")
            raise HTTPException(
                status_code=501,
                detail="Google OAuth is not configured. Set GOOGLE_OAUTH_CLIENT_ID in .env",
            )
        try:
            from google.oauth2 import id_token as google_id_token
            from google.auth.transport import requests as google_requests

            id_info = google_id_token.verify_oauth2_token(
                req.token, google_requests.Request(), app_settings.google_oauth_client_id
            )
            email = id_info["email"].lower()
            full_name = id_info.get("name", req.full_name or "Google User")
        except ImportError:
            logger.error("google-auth package not installed — run: pip install google-auth")
            raise HTTPException(status_code=501, detail="Server missing google-auth package")
        except ValueError as e:
            logger.warning("Google OAuth token verification failed: %s", e)
            raise HTTPException(status_code=401, detail="Invalid Google OAuth token")
    elif provider == "apple":
        # Apple Sign In requires server-side JWT verification with Apple's public keys
        # For now, reject until Apple credentials are configured
        logger.warning("Apple OAuth not yet configured with real verification")
        raise HTTPException(
            status_code=501,
            detail="Apple OAuth is not yet configured. Coming soon.",
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")

    stmt = select(UserRecord).where(UserRecord.email == email)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user:
        user = UserRecord(
            email=email,
            hashed_password=None,
            full_name=full_name,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    logger.info("OAuth login successful for %s via %s", email, provider)
    return _generate_token_response(user)


@router.get("/me", response_model=UserRead)
async def get_me(user: UserRecord = Depends(get_current_user)):
    """Retrieve current authenticated user profile."""
    return _user_to_read(user)
