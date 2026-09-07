"""Regression tests: Razorpay payment verification must fail closed in production.

Prior behaviour exposed a self-grant entitlement forge:

- ``RazorpayGateway.verify_payment_signature()`` accepted ANY ``mock_sig_*``
  string whenever ``_is_live`` was false, and ``_is_live`` was derived ONLY
  from the Razorpay key-id prefix — never from ``ENVIRONMENT``.
- A production instance booted without (or with a stale/mock) Razorpay key was
  therefore in "mock mode": a caller POSTing ``razorpay_signature="mock_sig_x"``
  to ``/api/billing/verify-payment`` gained a PAID PRO/ELITE subscription for
  free. Worse, when ``RAZORPAY_KEY_SECRET`` was unset the verifier fell back to
  a publicly-known constant, so any attacker could reproduce a "valid" HMAC.

These tests lock in the fail-closed contract: in production, a simulated
``mock_sig_*`` signature is ALWAYS rejected and an unconfigured gateway never
trusts a public-default HMAC. The dev/test sandbox (real-HMAC mock signature)
continues to work.
"""
from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.core.payment_gateway import RazorpayGateway, razorpay_gateway
from app.db.session import init_db
from app.main import app


# ── Unit: gateway signature verification ────────────────────────────────────

class TestVerifySignatureFailClosed:

    def test_mock_sig_rejected_in_production(self):
        """A `mock_sig_*` forged signature must be rejected when ENV=production,
        even if the gateway is running with mock/unconfigured (non-live) keys."""
        original_env = settings.environment
        try:
            settings.environment = "production"
            gw = RazorpayGateway(
                key_id="rzp_test_tradetron_mock_key",
                key_secret="rzp_test_tradetron_mock_secret",
            )
            assert gw._is_live is False  # mirror of a no-real-keys deploy
            assert gw.verify_payment_signature(
                "order_123", "pay_456", "mock_sig_anything"
            ) is False
        finally:
            settings.environment = original_env

    def test_mock_sig_rejected_in_production_without_keys(self):
        """Even with no keys configured at all (empty secret), production must
        never accept a public-default HMAC or a mock signature."""
        original_env = settings.environment
        try:
            settings.environment = "production"
            gw = RazorpayGateway(key_id="", key_secret="")
            assert gw.verify_payment_signature(
                "order_123", "pay_456", "mock_sig_anything"
            ) is False
            # A HMAC computed with the PUBLIC fallback constant must also fail.
            import hashlib
            import hmac

            forged = hmac.new(
                b"rzp_test_tradetron_mock_secret",
                b"order_123|pay_456",
                hashlib.sha256,
            ).hexdigest()
            assert gw.verify_payment_signature("order_123", "pay_456", forged) is False
        finally:
            settings.environment = original_env

    def test_real_hmac_still_accepted_in_dev(self):
        """The sandbox dev/test flow (a REAL HMAC with the mock secret) must
        continue to validate — this is how create-order → verify-payment works
        in tests."""
        original_env = settings.environment
        try:
            settings.environment = "testing"
            gw = RazorpayGateway(
                key_id="rzp_test_tradetron_mock_key",
                key_secret="rzp_test_tradetron_mock_secret",
            )
            sig = gw.generate_mock_signature("order_abc", "pay_xyz")
            assert gw.verify_payment_signature("order_abc", "pay_xyz", sig) is True
        finally:
            settings.environment = original_env

    def test_forged_signature_rejected_when_real_secret_configured(self):
        """With a REAL (non-public) secret configured, a correct-looking HMAC
        over the wrong secret must be rejected."""
        original_env = settings.environment
        try:
            settings.environment = "production"
            gw = RazorpayGateway(
                key_id="rzp_live_abc123",
                key_secret="a_real_secret_that_attacker_cannot_know",
            )
            assert gw._is_live is True
            # Attacker can only produce a HMAC with the PUBLIC mock constant.
            import hashlib
            import hmac

            forged = hmac.new(
                b"rzp_test_tradetron_mock_secret",
                b"order_123|pay_456",
                hashlib.sha256,
            ).hexdigest()
            assert gw.verify_payment_signature("order_123", "pay_456", forged) is False
        finally:
            settings.environment = original_env
# ── API-level: forged checkout must not upgrade a user in production ────────

@pytest.mark.asyncio
async def test_forged_mock_signature_does_not_upgrade_in_production(monkeypatch):
    """End-to-end: with ENV=production, a `mock_sig_*` payload to verify-payment
    must be rejected and the user must remain FREE."""
    original_env = settings.environment
    try:
        settings.environment = "production"
        # Simulate a production deploy that forgot real keys (or has only the
        # mock placeholders) — the exact dangerous scenario this fix closes.
        razorpay_gateway.key_id = "rzp_test_tradetron_mock_key"
        razorpay_gateway.key_secret = "rzp_test_tradetron_mock_secret"
        razorpay_gateway.webhook_secret = "rzp_test_tradethrone_webhook_secret"
        razorpay_gateway._is_live = False

        await init_db()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            uid = int(time.time() * 1000) % 1000000
            reg = await client.post(
                "/api/auth/register",
                json={
                    "email": f"forge_prod_{uid}@tradetron.io",
                    "password": "SecurePassword123!",
                },
            )
            assert reg.status_code == 201, reg.text
            headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

            order = await client.post(
                "/api/billing/create-order",
                json={"plan_name": "PRO", "billing_cycle": "MONTHLY"},
                headers=headers,
            )
            assert order.status_code == 200, order.text
            order_id = order.json()["order_id"]

            # Attacker submits a mock-prefixed signature (the historical bypass).
            forged = await client.post(
                "/api/billing/verify-payment",
                json={
                    "razorpay_order_id": order_id,
                    "razorpay_payment_id": "pay_forge_prod",
                    "razorpay_signature": "mock_sig_anything",
                    "plan_name": "PRO",
                    "billing_cycle": "MONTHLY",
                },
                headers=headers,
            )
            assert forged.status_code == 400, forged.text
            assert "Invalid transaction signature" in forged.json()["detail"]

            # User must remain on FREE.
            sub = (await client.get("/api/billing/subscription", headers=headers)).json()
            assert sub["plan_name"] == "FREE"
    finally:
        settings.environment = original_env
        razorpay_gateway.key_id = settings.razorpay_key_id
        razorpay_gateway.key_secret = settings.razorpay_key_secret
        razorpay_gateway.webhook_secret = settings.razorpay_webhook_secret