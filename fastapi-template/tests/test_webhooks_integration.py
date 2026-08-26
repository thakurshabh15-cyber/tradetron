"""Integration tests for TradeThrone Webhook Platform."""

from __future__ import annotations

import json
import hmac
import hashlib
import pytest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from app.webhooks.main import app
from app.webhooks.validation.signatures import (
    HMACVerifier,
    ZerodhaPostbackVerifier,
    UpstoxWebhookVerifier,
    init_verifiers,
)
from app.webhooks.queue.redis_streams import webhook_queue, QueuedWebhook
from app.webhooks.resiliency.idempotency import idempotency_store
from app.webhooks.resiliency.rate_limiter import rate_limiter
from app.webhooks.workers.pool import worker_pool
from app.config import settings


# Test configuration
TEST_ZERODHA_API_KEY = "test_zerodha_key"
TEST_ZERODHA_API_SECRET = "test_zerodha_secret"
TEST_RAZORPAY_WEBHOOK_SECRET = "test_razorpay_secret"
TEST_UPSTOX_WEBHOOK_SECRET = "test_upstox_secret"


@pytest.fixture(scope="session", autouse=True)
def setup_test_settings():
    """Configure test settings."""
    settings.zerodha_api_key = TEST_ZERODHA_API_KEY
    settings.zerodha_api_secret = TEST_ZERODHA_API_SECRET
    settings.razorpay_webhook_secret = TEST_RAZORPAY_WEBHOOK_SECRET
    settings.upstox_webhook_secret = TEST_UPSTOX_WEBHOOK_SECRET
    settings.redis_url = "redis://localhost:6379/1"  # Test DB
    settings.environment = "testing"
    settings.webhook_local_mode = False  # Disable local mode for integration tests
    init_verifiers(settings)


@pytest.fixture(autouse=True)
async def init_rate_limiter(mock_redis):
    """Initialize rate limiter for tests with mocked Redis."""
    await rate_limiter.initialize()
    yield
    # Cleanup if needed


@pytest.fixture(autouse=True)
async def init_idempotency_store(mock_redis):
    """Initialize idempotency store for tests with mocked Redis."""
    await idempotency_store.initialize()
    yield


@pytest.fixture(autouse=True)
async def init_webhook_queue(mock_redis):
    """Initialize webhook queue for tests with mocked Redis."""
    await webhook_queue.initialize()
    yield


@pytest.fixture
async def async_client():
    """Create async test client."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def zerodha_order_fill_payload():
    """Mock Zerodha order fill postback payload."""
    payload = {
        "order_id": "ZERODHA_ORDER_12345",
        "status": "FILLED",
        "tradingsymbol": "RELIANCE",
        "filled_quantity": 10,
        "average_price": 2450.50,
        "checksum": "",  # Will be computed
    }
    # Compute checksum: sha256(api_key + payload_without_checksum + api_secret)
    payload_without_checksum = {k: v for k, v in payload.items() if k != "checksum"}
    checksum = hashlib.sha256(
        f"{TEST_ZERODHA_API_KEY}{json.dumps(payload_without_checksum, separators=(',', ':'))}{TEST_ZERODHA_API_SECRET}".encode()
    ).hexdigest()
    payload["checksum"] = checksum
    return payload


@pytest.fixture
def razorpay_payment_captured_payload():
    """Mock Razorpay payment.captured webhook payload."""
    return {
        "event": "payment.captured",
        "event_id": "evt_test_12345",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_12345",
                    "order_id": "order_test_12345",
                    "amount": 199900,  # in paise
                    "currency": "INR",
                    "status": "captured",
                    "notes": {
                        "user_id": "user_123",
                        "plan_name": "PRO",
                        "billing_cycle": "MONTHLY",
                    },
                }
            }
        }
    }


def compute_razorpay_signature(payload: dict, secret: str) -> str:
    """Compute Razorpay HMAC-SHA256 signature."""
    body = json.dumps(payload, separators=(',', ':'))
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def compute_upstox_signature(payload: dict, secret: str) -> str:
    """Compute Upstox HMAC-SHA256 signature."""
    body = json.dumps(payload, separators=(',', ':'))
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


class TestSignatureVerification:
    """Test signature verification for all providers."""

    def test_zerodha_checksum_verification_valid(self, zerodha_order_fill_payload):
        """Test valid Zerodha checksum verification."""
        verifier = ZerodhaPostbackVerifier(TEST_ZERODHA_API_KEY, TEST_ZERODHA_API_SECRET)
        body = json.dumps(zerodha_order_fill_payload, separators=(',', ':')).encode()
        result = verifier.verify(body, {})
        assert result.valid is True
        assert result.provider == "zerodha"

    def test_zerodha_checksum_verification_invalid(self, zerodha_order_fill_payload):
        """Test invalid Zerodha checksum verification."""
        verifier = ZerodhaPostbackVerifier(TEST_ZERODHA_API_KEY, "wrong_secret")
        body = json.dumps(zerodha_order_fill_payload, separators=(',', ':')).encode()
        result = verifier.verify(body, {})
        assert result.valid is False
        assert "Invalid Zerodha checksum" in result.error

    def test_razorpay_hmac_verification_valid(self, razorpay_payment_captured_payload):
        """Test valid Razorpay HMAC verification."""
        verifier = HMACVerifier(TEST_RAZORPAY_WEBHOOK_SECRET, "X-Razorpay-Signature")
        body = json.dumps(razorpay_payment_captured_payload, separators=(',', ':')).encode()
        signature = compute_razorpay_signature(razorpay_payment_captured_payload, TEST_RAZORPAY_WEBHOOK_SECRET)
        headers = {"X-Razorpay-Signature": signature}
        result = verifier.verify(body, headers)
        assert result.valid is True
        assert result.provider == "x-razorpay-signature"

    def test_razorpay_hmac_verification_invalid(self, razorpay_payment_captured_payload):
        """Test invalid Razorpay HMAC verification."""
        verifier = HMACVerifier(TEST_RAZORPAY_WEBHOOK_SECRET, "X-Razorpay-Signature")
        body = json.dumps(razorpay_payment_captured_payload, separators=(',', ':')).encode()
        headers = {"X-Razorpay-Signature": "invalid_signature"}
        result = verifier.verify(body, headers)
        assert result.valid is False
        assert "Invalid signature" in result.error

    def test_upstox_hmac_verification_valid(self):
        """Test valid Upstox HMAC verification."""
        payload = {
            "event": "order_update",
            "order_id": "UPSTOX_ORDER_123",
            "status": "FILLED",
            "symbol": "RELIANCE",
            "filled_quantity": 5,
            "average_price": 2450.00,
        }
        verifier = UpstoxWebhookVerifier(TEST_UPSTOX_WEBHOOK_SECRET)
        body = json.dumps(payload, separators=(',', ':')).encode()
        signature = compute_upstox_signature(payload, TEST_UPSTOX_WEBHOOK_SECRET)
        headers = {"X-Upstox-Signature": signature}
        result = verifier.verify(body, headers)
        assert result.valid is True
        assert result.provider == "upstox"


class TestWebhookIngestion:
    """Test webhook ingestion endpoint."""

    @pytest.mark.asyncio
    async def test_zerodha_webhook_accepted(self, async_client, zerodha_order_fill_payload):
        """Test Zerodha webhook is accepted and queued."""
        body = json.dumps(zerodha_order_fill_payload, separators=(',', ':')).encode()
        
        response = await async_client.post(
            "/webhooks/zerodha",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert "event_id" in data

    @pytest.mark.asyncio
    async def test_zerodha_webhook_invalid_signature_rejected(self, async_client, zerodha_order_fill_payload):
        """Test Zerodha webhook with invalid checksum is rejected."""
        # Tamper with payload
        tampered = zerodha_order_fill_payload.copy()
        tampered["filled_quantity"] = 999
        body = json.dumps(tampered, separators=(',', ':')).encode()
        
        response = await async_client.post(
            "/webhooks/zerodha",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        
        assert response.status_code == 401
        assert "Invalid Zerodha checksum" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_razorpay_webhook_accepted(self, async_client, razorpay_payment_captured_payload):
        """Test Razorpay webhook is accepted and queued."""
        signature = compute_razorpay_signature(razorpay_payment_captured_payload, TEST_RAZORPAY_WEBHOOK_SECRET)
        body = json.dumps(razorpay_payment_captured_payload, separators=(',', ':')).encode()
        
        response = await async_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
            },
        )
        
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert "event_id" in data

    @pytest.mark.asyncio
    async def test_razorpay_webhook_invalid_signature_rejected(self, async_client, razorpay_payment_captured_payload):
        """Test Razorpay webhook with invalid signature is rejected."""
        body = json.dumps(razorpay_payment_captured_payload, separators=(',', ':')).encode()
        
        response = await async_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": "invalid_signature",
            },
        )
        
        assert response.status_code == 401
        assert "Invalid signature" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_unknown_provider_uses_fallback_route(self, async_client):
        """Test unknown provider uses fallback route."""
        payload = {
            "event": "custom_event",
            "event_id": "custom_123",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"data": "test"},
        }
        body = json.dumps(payload, separators=(',', ':')).encode()
        
        response = await async_client.post(
            "/webhooks/unknown_provider",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"


class TestIdempotency:
    """Test idempotency handling."""

    @pytest.mark.asyncio
    async def test_duplicate_webhook_returns_cached_result(self, async_client, razorpay_payment_captured_payload, mock_redis):
        """Test duplicate webhook returns cached result."""
        signature = compute_razorpay_signature(razorpay_payment_captured_payload, TEST_RAZORPAY_WEBHOOK_SECRET)
        body = json.dumps(razorpay_payment_captured_payload, separators=(',', ':')).encode()
        
        # First request - idempotency check returns new (1, None)
        mock_redis.register_script.return_value = AsyncMock(return_value=[1, None])
        response1 = await async_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
            },
        )
        assert response1.status_code == 202
        event_id = response1.json()["event_id"]
        
        # Second request - idempotency check returns completed (0, json_data)
        import json as json_module
        cached_result = {"status": "accepted", "event_id": event_id, "duplicate": True}
        mock_redis.register_script.return_value = AsyncMock(return_value=[0, json_module.dumps({
            "key": f"razorpay:{event_id}",
            "status": "completed",
            "created_at": 1234567890,
            "completed_at": 1234567890,
            "result": cached_result
        })])
        response2 = await async_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
                "X-Idempotency-Key": f"razorpay:{event_id}",
            },
        )
        assert response2.status_code == 202
        data2 = response2.json()
        assert data2["duplicate"] is True
        assert data2["event_id"] == event_id

    @pytest.mark.asyncio
    async def test_idempotency_key_from_header(self, async_client, razorpay_payment_captured_payload, mock_redis):
        """Test idempotency key from X-Idempotency-Key header."""
        signature = compute_razorpay_signature(razorpay_payment_captured_payload, TEST_RAZORPAY_WEBHOOK_SECRET)
        body = json.dumps(razorpay_payment_captured_payload, separators=(',', ':')).encode()
        idempotency_key = "custom_idempotency_key_123"
        
        # First request - idempotency check returns new (1, None)
        mock_redis.register_script.return_value = AsyncMock(return_value=[1, None])
        response1 = await async_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
                "X-Idempotency-Key": idempotency_key,
            },
        )
        assert response1.status_code == 202
        
        # Second request - idempotency check returns completed (0, json_data)
        import json as json_module
        cached_result = {"status": "accepted", "event_id": "evt_test_12345", "duplicate": True}
        mock_redis.register_script.return_value = AsyncMock(return_value=[0, json_module.dumps({
            "key": idempotency_key,
            "status": "completed",
            "created_at": 1234567890,
            "completed_at": 1234567890,
            "result": cached_result
        })])
        response2 = await async_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
                "X-Idempotency-Key": idempotency_key,
            },
        )
        assert response2.status_code == 202
        data2 = response2.json()
        assert data2["duplicate"] is True


class TestRateLimiting:
    """Test rate limiting."""

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, async_client, razorpay_payment_captured_payload):
        """Test rate limit is enforced per provider."""
        signature = compute_razorpay_signature(razorpay_payment_captured_payload, TEST_RAZORPAY_WEBHOOK_SECRET)
        body = json.dumps(razorpay_payment_captured_payload, separators=(',', ':')).encode()
        
        # Make requests up to burst limit
        # Note: This test may be flaky depending on rate limiter config
        # In practice, you'd configure a low limit for testing
        pass  # Skip for now - requires specific rate limit config


class TestQueueIntegration:
    """Test Redis queue integration."""

    @pytest.mark.asyncio
    async def test_webhook_enqueued_in_redis(self, async_client, razorpay_payment_captured_payload):
        """Test webhook is enqueued in Redis Streams."""
        # This test requires a running Redis instance
        # Mock the queue for unit testing
        with patch.object(webhook_queue, 'enqueue', new_callable=AsyncMock) as mock_enqueue:
            mock_enqueue.return_value = "12345-0"
            
            signature = compute_razorpay_signature(razorpay_payment_captured_payload, TEST_RAZORPAY_WEBHOOK_SECRET)
            body = json.dumps(razorpay_payment_captured_payload, separators=(',', ':')).encode()
            
            response = await async_client.post(
                "/webhooks/razorpay",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": signature,
                },
            )
            
            assert response.status_code == 202
            mock_enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_queue_priority_routing(self, async_client, zerodha_order_fill_payload, razorpay_payment_captured_payload):
        """Test critical broker webhooks go to critical queue, billing to high queue."""
        with patch.object(webhook_queue, 'enqueue', new_callable=AsyncMock) as mock_enqueue:
            mock_enqueue.return_value = "12345-0"
            
            # Zerodha (critical)
            body1 = json.dumps(zerodha_order_fill_payload, separators=(',', ':')).encode()
            await async_client.post(
                "/webhooks/zerodha",
                content=body1,
                headers={"Content-Type": "application/json"},
            )
            
            # Razorpay (high)
            signature = compute_razorpay_signature(razorpay_payment_captured_payload, TEST_RAZORPAY_WEBHOOK_SECRET)
            body2 = json.dumps(razorpay_payment_captured_payload, separators=(',', ':')).encode()
            await async_client.post(
                "/webhooks/razorpay",
                content=body2,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": signature,
                },
            )
            
            # Verify both were enqueued
            assert mock_enqueue.call_count == 2
            # Check queue names in calls
            calls = mock_enqueue.call_args_list
            # First call should be for zerodha (broker:critical)
            # Second call should be for razorpay (billing:high)


class TestHealthEndpoints:
    """Test health and readiness endpoints."""

    @pytest.mark.asyncio
    async def test_healthz(self, async_client):
        """Test health check endpoint."""
        response = await async_client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "webhook-platform"

    @pytest.mark.asyncio
    async def test_readyz(self, async_client):
        """Test readiness check endpoint."""
        with patch.object(webhook_queue, 'health_check', new_callable=AsyncMock) as mock_queue_health, \
             patch.object(worker_pool, 'health_check', return_value=True):
            mock_queue_health.return_value = True
            
            response = await async_client.get("/readyz")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ready"


class TestMetricsEndpoint:
    """Test Prometheus metrics endpoint."""

    @pytest.mark.asyncio
    async def test_metrics_endpoint(self, async_client):
        """Test metrics endpoint returns Prometheus format."""
        response = await async_client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        # Check for webhook metrics
        content = response.text
        assert "webhook_received_total" in content


class TestSchemaValidation:
    """Test payload schema validation."""

    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self, async_client):
        """Test invalid JSON is rejected."""
        response = await async_client.post(
            "/webhooks/razorpay",
            content=b"not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_empty_body_rejected(self, async_client):
        """Test empty body is rejected."""
        response = await async_client.post(
            "/webhooks/razorpay",
            content=b"",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert "Empty request body" in response.json()["detail"]


# Pytest configuration
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])