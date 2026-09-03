"""Unit tests for Production Authentication System: JWT, Lockout, Token Rotation, 2FA, & Reset."""

import asyncio
import time
import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.db.session import init_db
from app.core.security import _IN_MEMORY_OTP_STORE


@pytest.fixture(autouse=True)
def _force_inmemory_otp(monkeypatch):
    """This auth suite reads generated OTPs from the in-memory store and relies
    on per-process cooldown.  When a live staging Redis is reachable, OTPs and
    the cooldown rate limit move to Redis, breaking these assumptions.  Force
    the in-memory OTP/rate-limit path for a deterministic auth lifecycle test.
    """
    from app.core import security as _sec
    monkeypatch.setattr(_sec, "_redis", lambda: None)


@pytest.mark.asyncio
async def test_production_auth_suite():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uid = int(time.time() * 1000) % 1000000
        test_email = f"alpha_trader_{uid}@tradetron.io"
        test_phone = f"+919876{uid:06d}"

        # 1. Signup
        reg_payload = {
            "email": test_email,
            "phone": test_phone,
            "password": "SecurePassword123!",
            "full_name": "Alpha Trader",
        }
        res = await client.post("/api/auth/register", json=reg_payload)
        assert res.status_code == 201
        data = res.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["user"]["email"] == test_email
        assert data["user"]["phone"] == test_phone

        # 2. Login with email & remember_me
        login_res = await client.post("/api/auth/login", json={
            "identifier": test_email,
            "password": "SecurePassword123!",
            "remember_me": True,
        })
        assert login_res.status_code == 200
        tokens = login_res.json()
        access_tok = tokens["access_token"]
        refresh_tok = tokens["refresh_token"]

        # 3. Login with phone number
        phone_login = await client.post("/api/auth/login", json={
            "identifier": test_phone,
            "password": "SecurePassword123!",
        })
        assert phone_login.status_code == 200

        # 4. Failed login attempts & Account Lockout
        for _ in range(4):
            bad_login = await client.post("/api/auth/login", json={
                "identifier": test_email,
                "password": "WrongPassword!",
            })
            assert bad_login.status_code == 401

        # 5th attempt locks account
        lockout_res = await client.post("/api/auth/login", json={
            "identifier": test_email,
            "password": "WrongPassword!",
        })
        assert lockout_res.status_code == 423
        assert "locked" in lockout_res.json()["detail"].lower()

        # 5. Password Reset Flow (un-locks account upon reset)
        forgot_res = await client.post("/api/auth/forgot-password", json={"identifier": test_email})
        assert forgot_res.status_code == 200
        reset_otp = _IN_MEMORY_OTP_STORE.get(test_email)["code"]

        reset_res = await client.post("/api/auth/reset-password", json={
            "identifier": test_email,
            "otp_code": reset_otp,
            "new_password": "NewUltraSecurePass2026!",
        })
        assert reset_res.status_code == 200

        # 6. Login with new password
        new_login = await client.post("/api/auth/login", json={
            "identifier": test_email,
            "password": "NewUltraSecurePass2026!",
        })
        assert new_login.status_code == 200
        new_access_tok = new_login.json()["access_token"]
        new_refresh_tok = new_login.json()["refresh_token"]

        # 7. Token Refresh and Token Rotation
        ref_res = await client.post("/api/auth/refresh", json={"refresh_token": new_refresh_tok})
        assert ref_res.status_code == 200
        rotated_refresh_tok = ref_res.json()["refresh_token"]

        # Re-using the old refresh token must fail (Token Rotation security)
        reuse_res = await client.post("/api/auth/refresh", json={"refresh_token": new_refresh_tok})
        assert reuse_res.status_code == 401
        assert "revoked" in reuse_res.json()["detail"].lower()

        # 8. Server-Side Logout
        logout_res = await client.post("/api/auth/logout", json={"refresh_token": rotated_refresh_tok})
        assert logout_res.status_code == 200

        # Refreshed token is now revoked in DB
        revoked_check = await client.post("/api/auth/refresh", json={"refresh_token": rotated_refresh_tok})
        assert revoked_check.status_code == 401

        # 9. 2FA Setup and Verification with real TOTP code
        auth_headers = {"Authorization": f"Bearer {new_access_tok}"}
        setup_2fa_res = await client.post("/api/auth/2fa/setup", headers=auth_headers)
        assert setup_2fa_res.status_code == 200
        totp_secret = setup_2fa_res.json()["secret"]
        assert "otpauth://" in setup_2fa_res.json()["otpauth_url"]

        # Generate real TOTP code using RFC 6238
        real_totp_code = pyotp.TOTP(totp_secret).now()

        verify_2fa_res = await client.post("/api/auth/2fa/verify", json={"code": real_totp_code}, headers=auth_headers)
        assert verify_2fa_res.status_code == 200

        # Next login triggers 2FA requirement
        two_factor_login = await client.post("/api/auth/login", json={
            "identifier": test_email,
            "password": "NewUltraSecurePass2026!",
        })
        assert two_factor_login.status_code == 200
        assert two_factor_login.json()["two_factor_required"] is True
        assert two_factor_login.json()["temp_token"] is not None
