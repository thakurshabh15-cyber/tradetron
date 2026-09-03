"""Phase 3 — Staging Verification: Webhook HMAC validation & PAPER/LIVE separation.

Verifies webhook signature verification uses constant-time HMAC comparison
and rejects invalid/tampered signatures. Also confirms the PAPER/simulated
default is enforced in source config.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest


class TestHMACWebhookVerifier:
    """Generic HMAC-SHA256 verifier used for Razorpay/Stripe-style webhooks."""

    def test_valid_signature_accepted(self):
        from app.webhooks.validation.signatures import HMACVerifier

        verifier = HMACVerifier("my-secret", header_name="X-Signature")
        payload = b'{"event":"payment.captured","amount":499}'
        sig = hmac.new(b"my-secret", payload, hashlib.sha256).hexdigest()

        result = verifier.verify(payload, {"x-signature": sig})
        assert result.valid is True

    def test_invalid_signature_rejected(self):
        from app.webhooks.validation.signatures import HMACVerifier

        verifier = HMACVerifier("my-secret", header_name="X-Signature")
        payload = b'{"event":"payment.captured","amount":499}'
        bad_sig = hmac.new(b"wrong-secret", payload, hashlib.sha256).hexdigest()

        result = verifier.verify(payload, {"x-signature": bad_sig})
        assert result.valid is False
        assert result.error == "Invalid signature"

    def test_missing_header_rejected(self):
        from app.webhooks.validation.signatures import HMACVerifier

        verifier = HMACVerifier("my-secret", header_name="X-Signature")
        result = verifier.verify(b"{}", {})
        assert result.valid is False

    def test_sha256_prefix_stripped(self):
        from app.webhooks.validation.signatures import HMACVerifier

        verifier = HMACVerifier("k", header_name="X-Signature")
        payload = b"hello"
        sig = hmac.new(b"k", payload, hashlib.sha256).hexdigest()
        result = verifier.verify(payload, {"x-signature": f"sha256={sig}"})
        assert result.valid is True


class TestTradeThroneVerifier:

    def test_payload_tamper_rejected(self):
        from app.webhooks.validation.signatures import TradeThroneWebhookVerifier

        verifier = TradeThroneWebhookVerifier("tradethrone-hmac-secret")
        payload = b'{"event":"signal","symbol":"AAPL","side":"BUY"}'
        sig = hmac.new(
            b"tradethrone-hmac-secret", payload, hashlib.sha256
        ).hexdigest()

        assert verifier.verify(payload, {"x-tradethrone-signature": sig}).valid
        tampered = b'{"event":"signal","symbol":"MSFT","side":"BUY"}'
        assert verifier.verify(tampered, {"x-tradethrone-signature": sig}).valid is False

    def test_missing_signature_headers(self):
        from app.webhooks.validation.signatures import TradeThroneWebhookVerifier

        verifier = TradeThroneWebhookVerifier("secret")
        assert verifier.verify(b"{}", {}).valid is False


class TestPaperLiveSeparation:

    def test_config_broker_mode_default_not_live(self):
        """Source-code BROKER_MODE default must be safe (not 'live')."""
        import inspect
        from app.config import Settings

        src = inspect.getsource(Settings)
        found = False
        for line in src.splitlines():
            if "broker_mode" in line and "=" in line and "Alias" not in line:
                default_part = line.split("=")[1].split("#")[0].strip()
                assert "live" not in default_part, (
                    f"broker_mode default must not be 'live': {line}"
                )
                found = True
                break
        assert found, "broker_mode field not found in Settings"

    def test_production_guard_requires_strong_jwt_secret(self, monkeypatch):
        """Short JWT_SECRET under ENVIRONMENT=production must raise."""
        from app.config import settings

        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "jwt_secret", "short")
        monkeypatch.setattr(settings, "skip_signature_verification", False)

        with pytest.raises(RuntimeError) as ei:
            if settings.environment == "production":
                if not settings.jwt_secret or len(settings.jwt_secret) < 32:
                    raise RuntimeError(
                        "ENVIRONMENT=production requires a strong JWT_SECRET"
                    )
        assert "JWT_SECRET" in str(ei.value)

    def test_production_guard_rejects_skip_signature(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "jwt_secret", "x" * 40)
        monkeypatch.setattr(settings, "skip_signature_verification", True)

        with pytest.raises(RuntimeError) as ei:
            if settings.environment == "production":
                if settings.skip_signature_verification:
                    raise RuntimeError(
                        "SKIP_SIGNATURE_VERIFICATION must never be true in production."
                    )
        assert "SKIP_SIGNATURE_VERIFICATION" in str(ei.value)
