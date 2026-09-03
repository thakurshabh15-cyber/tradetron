"""Test Authentication: JWT, Password, OTP, OAuth, and Token Refresh."""

import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.db.session import init_db
from app.core.security import _IN_MEMORY_OTP_STORE


@pytest.fixture(autouse=True)
def _force_inmemory_otp(monkeypatch):
    """These auth-lifecycle tests read generated OTPs from the in-memory store.

    When a live staging Redis is reachable, OTPs (and the OTP-cooldown rate
    limit) move to Redis, which breaks these tests' assumptions.  Force the
    in-memory OTP/rate-limit path so the auth lifecycle is deterministic
    regardless of whether Redis is running.
    """
    from app.core import security as _sec
    monkeypatch.setattr(_sec, "_redis", lambda: None)


@pytest.mark.asyncio
async def test_auth_full_lifecycle():
    """Test register, login, me, otp, oauth, and token refresh."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        test_email = f"trader_{int(__import__('time').time())}@tradetron.io"
        test_password = "SecurePassword123!"

        # 1. Register
        reg_res = await client.post(
            "/api/auth/register",
            json={"email": test_email, "password": test_password, "full_name": "Test Algo Trader"},
        )
        assert reg_res.status_code == 201, reg_res.text
        reg_data = reg_res.json()
        assert "access_token" in reg_data
        assert "refresh_token" in reg_data
        assert reg_data["expires_in"] == 900  # 15 min = 900s
        assert reg_data["user"]["email"] == test_email

        # 2. Login
        login_res = await client.post(
            "/api/auth/login",
            json={"email": test_email, "password": test_password},
        )
        assert login_res.status_code == 200, login_res.text
        login_data = login_res.json()
        access_token = login_data["access_token"]
        refresh_token = login_data["refresh_token"]
        assert access_token is not None

        # 3. GET /api/auth/me with Bearer token
        me_res = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert me_res.status_code == 200, me_res.text
        me_data = me_res.json()
        assert me_data["email"] == test_email
        assert me_data["full_name"] == "Test Algo Trader"

        # 4. OTP Request & Verification
        otp_identifier = "otp_user@tradetron.io"
        otp_req_res = await client.post(
            "/api/auth/request-otp",
            json={"identifier": otp_identifier},
        )
        assert otp_req_res.status_code == 200, otp_req_res.text
        # Fetch real generated OTP from secure memory store
        otp_code = _IN_MEMORY_OTP_STORE.get(otp_identifier)["code"]

        otp_verify_res = await client.post(
            "/api/auth/verify-otp",
            json={"identifier": otp_identifier, "otp_code": otp_code, "full_name": "OTP Trader"},
        )
        assert otp_verify_res.status_code == 200, otp_verify_res.text
        otp_user_data = otp_verify_res.json()
        assert otp_user_data["user"]["email"] == otp_identifier

        # 5. Refresh Token
        refresh_res = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert refresh_res.status_code == 200, refresh_res.text
        new_token_data = refresh_res.json()
        assert "access_token" in new_token_data
        assert new_token_data["expires_in"] == 900
