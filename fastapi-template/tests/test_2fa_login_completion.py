"""RED→GREEN test for the 2FA login-completion journey (P1 product blocker).

Scenario that previously locked a real customer out permanently:
1. User registers & enables authenticator 2FA.
2. On next login, /api/auth/login returns two_factor_required=true + temp_token.
3. The ONLY correct way to finish signing in is to verify the 6-digit TOTP
   authenticator code against the pending temp_token and receive real tokens.
   Previously there was NO endpoint to complete this challenge — the
   temp_token was dead issuance and the frontend sent the TOTP code to the
   email-OTP endpoint (/verify-otp), which always failed.  A 2FA-enabled
   account therefore could never log in (P1).

This suite asserts the complete journey works end-to-end AND that the wrong
paths (email-OTP endpoint with an authenticator code, invalid TOTP) fail
closed.
"""

import asyncio
import time

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.db.session import init_db


@pytest.fixture(autouse=True)
def _force_inmemory_otp(monkeypatch):
    """Force the in-memory OTP/rate-limit path so the auth lifecycle is
    deterministic and not coupled to a live Redis."""
    from app.core import security as _sec

    monkeypatch.setattr(_sec, "_redis", lambda: None)


@pytest.mark.asyncio
async def test_2fa_login_completion_end_to_end():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uid = int(time.time() * 1000) % 1000000
        test_email = f"mfa_trader_{uid}@tradetron.io"

        # 1. Register
        reg = await client.post(
            "/api/auth/register",
            json={
                "email": test_email,
                "phone": None,
                "password": "SecurePassword123!",
                "full_name": "MFA Trader",
            },
        )
        assert reg.status_code == 201
        access = reg.json()["access_token"]
        headers = {"Authorization": f"Bearer {access}"}

        # 2. Enable authenticator 2FA
        setup = await client.post("/api/auth/2fa/setup", headers=headers)
        assert setup.status_code == 200
        totp_secret = setup.json()["secret"]

        verify_setup = await client.post(
            "/api/auth/2fa/verify",
            json={"code": pyotp.TOTP(totp_secret).now()},
            headers=headers,
        )
        assert verify_setup.status_code == 200

        # 3. Next login raises the 2FA challenge
        login = await client.post(
            "/api/auth/login",
            json={"identifier": test_email, "password": "SecurePassword123!"},
        )
        assert login.status_code == 200
        body = login.json()
        assert body["two_factor_required"] is True
        assert body["temp_token"] is not None
        # A challenge must never hand out a real access token.
        assert body["access_token"] == ""

        # 4. WRONG path must fail closed: sending the authenticator code to the
        #    email-OTP endpoint (the pre-fix frontend bug) must NOT authenticate.
        wrong = await client.post(
            "/api/auth/verify-otp",
            json={
                "identifier": test_email,
                "otp_code": pyotp.TOTP(totp_secret).now(),
                "full_name": "MFA Trader",
            },
        )
        assert wrong.status_code == 400

        # 5. Invalid TOTP code against the challenge must be rejected.
        bad = await client.post(
            "/api/auth/2fa/complete",
            json={"temp_token": body["temp_token"], "code": "000000"},
        )
        assert bad.status_code == 401

        # 6. A forged / non-2FA-pending temp token must be rejected.
        forged = await client.post(
            "/api/auth/2fa/complete",
            json={"temp_token": "not.a.jwt", "code": pyotp.TOTP(totp_secret).now()},
        )
        assert forged.status_code == 401

        # 7. CORRECT path: complete the challenge with the real TOTP code.
        complete = await client.post(
            "/api/auth/2fa/complete",
            json={"temp_token": body["temp_token"], "code": pyotp.TOTP(totp_secret).now()},
        )
        assert complete.status_code == 200
        final = complete.json()
        assert final["access_token"]
        assert final["refresh_token"]
        assert final["two_factor_required"] is False
        assert final["user"]["email"] == test_email

        # 8. The newly issued access token is valid for authenticated calls.
        me = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {final['access_token']}"}
        )
        assert me.status_code == 200
        assert me.json()["email"] == test_email
