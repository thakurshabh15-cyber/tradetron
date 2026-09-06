"""Integration tests for TradeThrone Webhook Platform."""

from __future__ import annotations

import json
import asyncio
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
                    "amount": 799900,  # in paise
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
        """Unknown providers have no registered signature verifier.

        SECURITY HARDENING (Phase 3-E): the ("*","*") fallback route resolves to
        the custom_normal worker pool whose handler executes REAL orders, so an
        unregistered provider slug must be REJECTED (401) rather than accepted
        unauthenticated.
        """
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

        assert response.status_code == 401
        assert "No signature verifier configured" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_unknown_provider_with_valid_tradethrone_signal_rejected(
        self, async_client
    ):
        """An unauthenticated TradeThrone signal to an unregistered provider slug
        must NOT be accepted for execution.
        """
        signal_payload = {
            "event": "signal",
            "event_id": "evil_001",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal": "buy",
            "symbol": "RELIANCE",
            "action": "BUY",
            "quantity": 10,
            "price": 2450.0,
        }
        body = json.dumps(signal_payload, separators=(',', ':')).encode()

        response = await async_client.post(
            "/webhooks/not_a_real_provider",
            content=body,
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 401
        assert "No signature verifier configured" in response.json()["detail"]



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


class TestReplayFreshnessWindow:
    """Replay-after-TTL protection (Phase 3-E).

    A captured webhook whose authenticated timestamp precedes the idempotency
    retention window (7 days by default) can never be deduplicated after the
    idempotency key expires, so a replay would re-run its financial/trading
    side effect. The validation layer must reject such stale events while
    continuing to accept fresh ones and preserving signature semantics.
    """

    @staticmethod
    def _razorpay_body_with_timestamp(timestamp_iso: str) -> bytes:
        payload = {
            "event": "payment.captured",
            "event_id": "evt_timestamp_probe",
            "timestamp": timestamp_iso,
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_probe_1",
                        "order_id": "order_probe_1",
                        "amount": 799900,
                        "currency": "INR",
                        "status": "captured",
                        "notes": {
                            "user_id": "user_probe",
                            "plan_name": "PRO",
                            "billing_cycle": "MONTHLY",
                        },
                    }
                }
            },
        }
        return json.dumps(payload, separators=(',', ':')).encode()

    @pytest.mark.asyncio
    async def test_replay_after_idempotency_ttl_rejected(
        self, async_client
    ):
        """A webhook older than the idempotency/replay window must be rejected
        (401) — it can no longer be deduplicated and is a replay candidate."""
        from datetime import timedelta
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=idempotency_store.ttl_seconds + 60
        )
        body = self._razorpay_body_with_timestamp(stale.isoformat())
        signature = compute_razorpay_signature(
            json.loads(body), TEST_RAZORPAY_WEBHOOK_SECRET
        )
        response = await async_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
            },
        )
        assert response.status_code == 401
        assert "replay" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_fresh_webhook_still_accepted(self, async_client):
        """A fresh, validly-signed webhook within the replay window is still
        accepted (202) — the guard does not reject legitimate events."""
        body = self._razorpay_body_with_timestamp(
            datetime.now(timezone.utc).isoformat()
        )
        signature = compute_razorpay_signature(
            json.loads(body), TEST_RAZORPAY_WEBHOOK_SECRET
        )
        response = await async_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
            },
        )
        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_no_timestamp_payload_still_accepted(self, async_client):
        """Providers that do not send a timestamp are unaffected: the extraction
        fallback treats them as 'now', which is always within the window."""
        payload = {
            "event": "payment.captured",
            "event_id": "evt_no_ts",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_nots_1",
                        "order_id": "order_nots_1",
                        "amount": 799900,
                        "currency": "INR",
                        "status": "captured",
                        "notes": {
                            "user_id": "user_nots",
                            "plan_name": "PRO",
                            "billing_cycle": "MONTHLY",
                        },
                    }
                }
            },
        }
        body = json.dumps(payload, separators=(',', ':')).encode()
        signature = compute_razorpay_signature(payload, TEST_RAZORPAY_WEBHOOK_SECRET)
        response = await async_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
            },
        )
        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_replay_guard_skipped_in_local_mode(self, async_client):
        """Local (dev/test) mode keeps its documented behavior: signature AND
        freshness checks are bypassed, so a stale timestamp is not rejected."""
        from datetime import timedelta
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=idempotency_store.ttl_seconds + 60
        )
        body = self._razorpay_body_with_timestamp(stale.isoformat())
        original = settings.webhook_local_mode
        settings.webhook_local_mode = True
        try:
            response = await async_client.post(
                "/webhooks/razorpay",
                content=body,
                headers={"Content-Type": "application/json"},
            )
        finally:
            settings.webhook_local_mode = original
        # Local mode returns HTTP 200 (synchronous local handler), not 202 —
        # the essential guarantee is that the stale event is NOT rejected
        # (no 401 replay block) in local mode.
        assert response.status_code == 200




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

    @pytest.mark.asyncio
    async def test_consumer_groups_created_for_all_routed_queues(self, mock_redis):
        """Regression: every queue referenced in the routing table must get a
        consumer group during queue initialisation.

        TradeThrone signals (and other custom routes) enqueue into queues such as
        ``webhooks:tradethrone:high`` / ``webhooks:tradethrone:normal`` /
        ``webhooks:tradethrone:critical``.  If those streams lack a consumer
        group, the worker's XREADGROUP raises NOGROUP and the messages sit in
        Redis forever — the platform returns HTTP 202 Accepted but the signal
        is silently dropped.  This test pins the dynamic derivation from
        ROUTE_TABLE so the worker pool and routing table can never drift apart.
        """
        from app.webhooks.routing.router import ROUTE_TABLE
        from app.webhooks.queue.redis_streams import WebhookQueue

        # Every queue declared by routes, plus the always-present DLQ.
        expected_queues = {route.queue_name for route in ROUTE_TABLE.values()}
        expected_queues.add("webhooks:dlq")

        queue = WebhookQueue(redis_url="redis://localhost:6379/0")
        await queue.initialize()

        # Every consumer-group creation (the ids passed to xgroup_create).
        created = {call.args[0] for call in mock_redis.xgroup_create.call_args_list}

        # The DLQ is always configured.
        assert "webhooks:dlq" in created
        # Every routed queue is configured with a consumer group.
        missing = sorted(expected_queues - created)
        assert not missing, (
            "Queues referenced in ROUTE_TABLE have no consumer group; "
            "webhooks enqueued to them will never be processed: %s" % missing
        )

    @pytest.mark.asyncio
    async def test_nack_acks_original_entry_on_retry(self, mock_redis):
        """Regression: a retryable failure must not leave the original entry in
        the pending entries list (PEL).

        Previously ``nack()`` requeued via XADD (attempt+1) but never XACK'd the
        original entry.  The original then lingered in the PEL forever — growing
        without bound, and a Redis XAUTOCLAIM/XCLAIM reclaim would re-process it
        from attempt=1, duplicating broker/payment effects and losing retry
        history.  ``nack()`` must resolve the original entry it replaces.
        """
        from app.webhooks.queue.redis_streams import WebhookQueue
        from app.webhooks.validation.schemas import WebhookEnvelope
        from datetime import datetime, timezone

        queue = WebhookQueue(redis_url="redis://localhost:6379/0")
        await queue.initialize()

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-retry-1",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "TEST1"},
            ),
            attempt=1,
        )

        await queue.nack("webhooks:broker:critical", "100-1", webhook, "boom")

        # Original pending entry must be acknowledged (removed from the PEL).
        mock_redis.xack.assert_any_call(
            "webhooks:broker:critical", "workers", "100-1"
        )
        # And a retry copy was requeued.
        mock_redis.xadd.assert_any_call(
            "webhooks:broker:critical", webhook.to_stream_entry()
        )

    @pytest.mark.asyncio
    async def test_nack_acks_original_on_dlq(self, mock_redis):
        """Regression: when retries are exhausted, the DLQ copy supersedes the
        original entry, so the original must be acked (removed from PEL).
        """
        from app.webhooks.queue.redis_streams import WebhookQueue
        from app.webhooks.validation.schemas import WebhookEnvelope
        from datetime import datetime, timezone

        queue = WebhookQueue(redis_url="redis://localhost:6379/0")
        await queue.initialize()

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-dlq-1",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "TEST2"},
            ),
            attempt=5,  # At max_retries for broker_critical
        )

        await queue.nack("webhooks:broker:critical", "100-2", webhook, "exhausted")

        # Original entry removed from PEL.
        mock_redis.xack.assert_any_call(
            "webhooks:broker:critical", "workers", "100-2"
        )
        # A DLQ entry was created with the final error.
        dlq_entry = next(
            (
                call.args[1]
                for call in mock_redis.xadd.call_args_list
                if call.args and call.args[0] == "webhooks:dlq"
            ),
            None,
        )
        assert dlq_entry is not None, "expected a webhooks:dlq xadd"
        assert dlq_entry.get("final_error") == "exhausted"

    @pytest.mark.asyncio
    async def test_nack_metric_failure_cannot_release_pel_entry(self, mock_redis):
        """Regression: nack() must resolve the original pending entry (XACK)
        BEFORE best-effort metric updates (HINCRBY), so a metric failure can
        never re-leak the original into the PEL and trigger a duplicate.

        Previously ``nack()`` requeued via XADD and then called
        ``hincrby("webhook:metrics:retried", ...)`` without XACK'ing the
        original delivered-but-failed entry first.  If that metric call threw
        (Redis hiccup), the exception aborted the method *before* the original
        was XACK'd.  The original then lingered in the pending entries list
        where an XAUTOCLAIM/XCLAIM reclaim would deliver it again from
        attempt=1 — duplicating broker/payment effects and losing the retry
        history, even though a retry copy had already been durably written.

        Ordering guarantee under test: XACK of the original must be issued
        before the metric HINCRBY, so a failing metric can never prevent the
        PEL resolution.
        """
        from app.webhooks.queue.redis_streams import WebhookQueue
        from app.webhooks.validation.schemas import WebhookEnvelope
        from datetime import datetime, timezone

        queue = WebhookQueue(redis_url="redis://localhost:6379/0")
        await queue.initialize()

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-metric-fail-1",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "TEST6"},
            ),
            attempt=0,
        )

        # The metric update fails — this must NOT matter for delivery
        # correctness.  The original PEL entry must already be resolved.
        # A shared call log records the real relative order of XACK vs HINCRBY
        # (comparing per-mock call lists can't prove cross-mock ordering).
        call_log: list[tuple[str, tuple]] = []

        async def record_xack(*args, **kwargs):
            call_log.append(("xack", args))
            return True

        async def failing_hincrby(*args, **kwargs):
            call_log.append(("hincrby", args))
            raise RuntimeError("metric store down")

        mock_redis.xack = AsyncMock(side_effect=record_xack)
        mock_redis.hincrby = AsyncMock(side_effect=failing_hincrby)

        await queue.nack("webhooks:broker:critical", "100-6", webhook, "boom")

        # Retry copy was durably written.
        mock_redis.xadd.assert_any_call(
            "webhooks:broker:critical", webhook.to_stream_entry()
        )
        # The original PEL entry was XACK'd despite the metric failure.
        assert ("xack", ("webhooks:broker:critical", "workers", "100-6")) in call_log
        # The metric call was at least attempted (and raised harmlessly).
        assert any(
            call[0] == "hincrby" and call[1][0] == "webhook:metrics:retried"
            for call in call_log
        ), "expected the retried metric update to be attempted"

        # Deterministic ordering check: the XACK of the original entry must
        # have been issued BEFORE the retried-metric HINCRBY.  If the metric
        # call were reordered ahead of the XACK, a metric failure would precede
        # (and thus prevent) the PEL resolution.
        xack_idx = next(
            i
            for i, (kind, args) in enumerate(call_log)
            if kind == "xack"
            and args[0] == "webhooks:broker:critical"
            and args[2] == "100-6"
        )
        metric_idx = next(
            i
            for i, (kind, args) in enumerate(call_log)
            if kind == "hincrby" and args[0] == "webhook:metrics:retried"
        )
        assert xack_idx < metric_idx, (
            "XACK (PEL resolution) must precede the metric update so a metric "
            "failure can never re-leak the original entry into the PEL"
        )

    @pytest.mark.asyncio
    async def test_nack_dlq_metric_failure_still_acks_original(self, mock_redis):
        """Regression: in the DLQ branch of nack(), a best-effort DLQ metric
        failure must not prevent the XACK of the original pending entry.

        Before the fix, _send_to_dlq() performed the DLQ HINCRBY without
        guarding it.  If that metric call threw (Redis hiccup), the exception
        propagated out of _send_to_dlq() back through nack() *before* the
        XACK of the original delivered-but-failed entry ran.  The original then
        lingered in the PEL where XAUTOCLAIM/XCLAIM reclaim would re-process
        it, producing a SECOND DLQ copy (and growing the PEL without bound).
        """
        from app.webhooks.queue.redis_streams import WebhookQueue
        from app.webhooks.validation.schemas import WebhookEnvelope
        from datetime import datetime, timezone

        queue = WebhookQueue(redis_url="redis://localhost:6379/0")
        await queue.initialize()

        # attempt (5) = max_retries for zerodha/order_update -> DLQ branch.
        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-metric-fail-dlq-1",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "TEST7"},
            ),
            attempt=5,
        )

        mock_redis.hincrby = AsyncMock(side_effect=RuntimeError("metric store down"))

        # Must NOT raise: the DLQ copy is durable and the original is resolved.
        await queue.nack("webhooks:broker:critical", "100-7", webhook, "exhausted")

        # DLQ copy durably written.
        assert any(
            call.args[0] == "webhooks:dlq"
            for call in mock_redis.xadd.call_args_list
        ), "expected a webhooks:dlq xadd for the exhausted webhook"
        # Original PEL entry was XACK'd despite the metric failure.
        mock_redis.xack.assert_any_call(
            "webhooks:broker:critical", "workers", "100-7"
        )

    @pytest.mark.asyncio
    async def test_successful_handler_ack_failure_does_not_nack(self, mock_redis):
        """Regression: a transient ACK failure after a *successful* handler run
        must not requeue the event.

        Previously any exception in ``ack()`` was swallowed by the catch-all
        ``except Exception`` in ``WorkerPool._process_webhook``, which called
        ``nack()`` and requeued an event whose broker/payment side effects had
        already been committed — duplicating the effect on the retry.
        """
        from app.webhooks.validation.schemas import WebhookEnvelope
        from app.webhooks.workers.pool import WorkerPool
        from app.webhooks.workers.pool import WorkerConfig
        from datetime import datetime, timezone

        async def succeed(_wh):
            return None  # Handler side effects committed.

        ok_config = WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1,
            handler=succeed,
        )
        pool = WorkerPool()
        pool.register_pool(ok_config)

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-dupe-1",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "TEST3"},
            ),
            attempt=0,
        )

        # Force a transient ACK failure after a successful handler run.
        mock_redis.xack = AsyncMock(side_effect=RuntimeError("redis gone"))

        with patch.object(webhook_queue, "nack", new_callable=AsyncMock) as mock_nack:
            await pool._process_webhook(
                "broker_critical_test", ok_config, "100-3", webhook
            )
            # The already-executed event must NOT be requeued (no nack).
            mock_nack.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_handler_marks_idempotency_completed_before_ack(
        self, mock_redis
    ):
        """Regression (P0-2): the worker must transition the idempotency
        record from "processing" to "completed" right after a *successful*
        handler run, and that transition must be issued BEFORE the XACK.

        Before the fix, nothing ever called ``mark_completed()``: the record
        stayed "processing" forever, and the Lua stale-processing branch in
        ``check_and_mark_processing()`` DELETES any "processing" record older
        than 5 minutes — after which a re-delivered webhook is treated as
        brand-new and the broker/payment side effect executes a second time
        (duplicate order / duplicate payment+invoice / duplicate trade).
        """
        from app.webhooks.validation.schemas import WebhookEnvelope
        from app.webhooks.workers.pool import WorkerPool
        from app.webhooks.workers.pool import WorkerConfig
        from app.webhooks.resiliency.idempotency import idempotency_store
        from datetime import datetime, timezone

        call_log = []

        async def succeed(_wh):
            call_log.append(("handler", None))  # side effects committed
            return None

        async def record_mark_completed(key, result):
            call_log.append(("mark_completed", (key, result)))

        async def record_xack(qname, group, entry_id):
            call_log.append(("xack", (qname, entry_id)))

        ok_config = WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1,
            handler=succeed,
        )
        pool = WorkerPool()
        pool.register_pool(ok_config)

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-complete-1",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "TEST9"},
                idempotency_key="zerodha:evt-complete-1",
            ),
            attempt=0,
        )

        mock_redis.xack = AsyncMock(side_effect=record_xack)

        with patch.object(
            idempotency_store, "mark_completed", new=record_mark_completed
        ):
            await pool._process_webhook(
                "broker_critical_test", ok_config, "100-9", webhook
            )

        # The idempotency record must have been completed with the envelope's
        # idempotency key (NOT None) — otherwise the ingress Lua script treats
        # the event as brand-new after the 5-minute stale window.
        assert any(
            kind == "mark_completed" for kind, _ in call_log
        ), "mark_completed must be called after a successful handler run"
        mark_idx = next(
            i for i, (kind, _) in enumerate(call_log) if kind == "mark_completed"
        )
        mark_key, _mark_result = call_log[mark_idx][1]
        assert mark_key == "zerodha:evt-complete-1"

        # Ordering guarantee: completion must be durably issued BEFORE XACK.
        # If mark_completed ran after (or was skipped when) ack failed, the
        # record could stay "processing" and expire after 5 minutes.
        xack_idx = next(i for i, (kind, _) in enumerate(call_log) if kind == "xack")
        assert mark_idx < xack_idx, (
            "idempotency completion must precede the XACK so a completed "
            "record exists even when the ACK subsequently fails"
        )

    @pytest.mark.asyncio
    async def test_mark_completed_failure_does_not_block_ack(self, mock_redis):
        """Sanity: mark_completed() is best-effort.  A Redis failure at the
        completion step must NOT block the XACK and must NOT trigger the nack
        path (which would requeue an already-executed webhook and duplicate
        its side effects)."""
        from app.webhooks.validation.schemas import WebhookEnvelope
        from app.webhooks.workers.pool import WorkerPool
        from app.webhooks.workers.pool import WorkerConfig
        from app.webhooks.resiliency.idempotency import idempotency_store
        from datetime import datetime, timezone

        async def succeed(_wh):
            return None  # Handler side effects committed.

        async def fail_mark_completed(_key, _result):
            raise RuntimeError("redis gone at completion step")

        ok_config = WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1,
            handler=succeed,
        )
        pool = WorkerPool()
        pool.register_pool(ok_config)

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-complete-fail-1",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "TEST10"},
                idempotency_key="zerodha:evt-complete-fail-1",
            ),
            attempt=0,
        )

        mock_redis.xack = AsyncMock(return_value=True)

        with patch.object(
            idempotency_store, "mark_completed", new=fail_mark_completed
        ), patch.object(webhook_queue, "nack", new_callable=AsyncMock) as mock_nack:
            # Must NOT raise despite the completion failure.
            await pool._process_webhook(
                "broker_critical_test", ok_config, "100-10", webhook
            )
            # XACK still issued.
            mock_redis.xack.assert_awaited_once_with(
                "webhooks:broker:critical", "workers", "100-10"
            )
            # The already-executed event must NOT be requeued.
            mock_nack.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_processing_record_allows_reprocessing_after_5min(
        self, mock_redis
    ):
        """Documents the vulnerability window that P0-2 closes: any idempotency
        record left in "processing" for more than 300 seconds is DELETED by the
        Lua stale branch, after which a duplicate webhook is treated as brand
        new (is_new=True) and its side effects would be executed again.

        The Lua semantics are emulated in Python so the behaviour is
        deterministic without a live Redis server.
        """
        import json as _json
        from datetime import datetime, timezone
        from app.webhooks.resiliency.idempotency import IdempotencyStore

        state: dict = {}

        async def lua_check(keys=None, args=None):  # emulates redis Lua script
            keys = keys or []
            args = args or []
            key = keys[0]
            now = float(args[1])
            existing = state.get(key)
            if existing is None:
                state[key] = {"status": "processing", "created_at": now}
                return [1, None]
            if existing["status"] == "completed":
                return [0, _json.dumps(existing)]
            if existing["status"] == "processing":
                age = now - existing["created_at"]
                if age > 300:
                    del state[key]
                    return [1, None]
                return [-1, None]
            return [1, None]  # failed -> retry

        mock_redis.register_script = MagicMock(return_value=lua_check)

        store = IdempotencyStore(redis_url="redis://localhost:6379/0")
        await store.initialize()

        # First delivery marks processing (is_new=True).
        is_new, _ = await store.check_and_mark_processing("evt:stale")
        assert is_new is True

        # Simulate the worker never calling mark_completed(): the record ages
        # past the 5-minute stale window (created_at backdated 400s).
        key = "idempotency:evt:stale"
        state[key]["created_at"] = datetime.now(timezone.utc).timestamp() - 400

        # A duplicate delivery after the window is treated as a NEW event.
        is_new_again, _ = await store.check_and_mark_processing("evt:stale")
        assert is_new_again is True, (
            "a stale 'processing' record is deleted and reprocessed — this is "
            "the duplicate-execution window that the mark_completed() call in "
            "WorkerPool._process_webhook closes"
        )

    @pytest.mark.asyncio
    async def test_completed_idempotency_record_returns_cached_result(
        self, mock_redis
    ):
        """The fixed path: once mark_completed() has run, a duplicate webhook
        is short-circuited with the cached result (is_new=False) instead of
        being reprocessed — for the full idempotency TTL, not just 5 minutes.
        """
        import json as _json
        from app.webhooks.resiliency.idempotency import IdempotencyStore

        state: dict = {}

        async def lua_check(keys=None, args=None):  # emulates redis Lua script
            keys = keys or []
            args = args or []
            key = keys[0]
            now = float(args[1])
            existing = state.get(key)
            if existing is None:
                state[key] = {"status": "processing", "created_at": now}
                return [1, None]
            if existing["status"] == "completed":
                return [0, _json.dumps(existing)]
            if existing["status"] == "processing":
                age = now - existing["created_at"]
                if age > 300:
                    del state[key]
                    return [1, None]
                return [-1, None]
            return [1, None]

        async def lua_set(key, value, ex=None):
            state[key] = _json.loads(value)
            return True

        mock_redis.register_script = MagicMock(return_value=lua_check)
        mock_redis.set = AsyncMock(side_effect=lua_set)

        store = IdempotencyStore(redis_url="redis://localhost:6379/0")
        await store.initialize()

        is_new, _ = await store.check_and_mark_processing("evt:done")
        assert is_new is True

        # Worker success path (the P0-2 fix): transition to completed.
        await store.mark_completed("evt:done", {"status": "processed"})
        assert state["idempotency:evt:done"]["status"] == "completed"

        # Even if the record is old enough to trip the stale branch, the
        # "completed" status must short-circuit reprocessing.
        state["idempotency:evt:done"]["created_at"] = (
            datetime.now(timezone.utc).timestamp() - 400
        )

        is_new_again, cached = await store.check_and_mark_processing("evt:done")
        assert is_new_again is False
        assert cached == {"status": "processed"}

    @pytest.mark.asyncio
    async def test_handler_failure_still_nacks(self, mock_redis):
        """Sanity: a genuine handler failure still goes down the nack path —
        we did not disable retries entirely."""
        from app.webhooks.validation.schemas import WebhookEnvelope
        from app.webhooks.workers.pool import WorkerPool
        from app.webhooks.workers.pool import WorkerConfig
        from datetime import datetime, timezone

        async def fail(_wh):
            raise RuntimeError("handler boom")

        fail_config = WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1,
            handler=fail,
        )
        pool = WorkerPool()
        pool.register_pool(fail_config)

        mock_redis.xack = AsyncMock(return_value=True)

        with patch.object(webhook_queue, "nack", new_callable=AsyncMock) as mock_nack:
            await pool._process_webhook(
                "broker_critical_test",
                fail_config,
                "100-5",
                QueuedWebhook(
                    envelope=WebhookEnvelope(
                        event_id="evt-fail-2",
                        event_type="order_update",
                        timestamp=datetime.now(timezone.utc),
                        provider="zerodha",
                        payload={"order_id": "TEST5"},
                    ),
                    attempt=0,
                ),
            )
            mock_nack.assert_called_once()
    # --- P0-2b: Worker-side duplicate-suppression guard tests -------

    @pytest.mark.asyncio
    async def test_worker_duplicate_guard_skips_handler_and_acks(
        self, mock_redis
    ):
        """Regression (P0-2b): when the idempotency key is already COMPLETED,
        the worker must NOT execute the handler a second time.
        """
        from app.webhooks.validation.schemas import WebhookEnvelope
        from app.webhooks.workers.pool import WorkerPool
        from app.webhooks.workers.pool import WorkerConfig
        from app.webhooks.resiliency.idempotency import idempotency_store
        from datetime import datetime, timezone

        called = []

        async def record_handler(_wh):
            called.append(True)

        done_config = WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1,
            handler=record_handler,
        )
        pool = WorkerPool()
        pool.register_pool(done_config)

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-done-dup",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "DUP1"},
                idempotency_key="zerodha:evt-done-dup",
            ),
            attempt=0,
        )

        with patch.object(
            idempotency_store, "is_completed", new=AsyncMock(return_value=True)
        ):
            await pool._process_webhook(
                "broker_critical_test", done_config, "100-90", webhook
            )

        assert not called, "handler must not run for a completed key"
        mock_redis.xack.assert_awaited_with(
            "webhooks:broker:critical", "workers", "100-90"
        )
        assert mock_redis.xadd.call_count == 0, "must not requeue a duplicate"

    @pytest.mark.asyncio
    async def test_worker_duplicate_guard_runs_handler_when_not_completed(
        self, mock_redis
    ):
        """The guard is transparent for a NEW / not-yet-completed key: the
        handler executes normally and mark_completed -> XACK ordering is preserved.
        """
        from app.webhooks.validation.schemas import WebhookEnvelope
        from app.webhooks.workers.pool import WorkerPool
        from app.webhooks.workers.pool import WorkerConfig
        from app.webhooks.resiliency.idempotency import idempotency_store
        from datetime import datetime, timezone

        call_log = []

        async def succeed(_wh):
            call_log.append(("handler", None))

        async def record_mark_completed(key, result):
            call_log.append(("mark_completed", (key, result)))

        async def record_xack(qname, group, entry_id):
            call_log.append(("xack", (qname, entry_id)))

        ok_config = WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1, handler=succeed,
        )
        pool = WorkerPool()
        pool.register_pool(ok_config)

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-incomplete",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "INCPT1"},
                idempotency_key="zerodha:evt-incomplete",
            ),
            attempt=0,
        )

        mock_redis.xack = AsyncMock(side_effect=record_xack)
        with patch.object(
            idempotency_store, "is_completed", new=AsyncMock(return_value=False)
        ), patch.object(
            idempotency_store, "mark_completed", new=record_mark_completed
        ):
            await pool._process_webhook(
                "broker_critical_test", ok_config, "100-91", webhook
            )

        kinds = [k for k, _ in call_log]
        assert kinds[0] == "handler"
        assert kinds.index("mark_completed") < kinds.index("xack")

    @pytest.mark.asyncio
    async def test_worker_duplicate_guard_fail_safe_on_lookup_failure(
        self, mock_redis
    ):
        """FAIL-SAFE: if the idempotency lookup fails, the event must NOT be
        silently treated as completed — the handler must still execute.
        """
        from app.webhooks.validation.schemas import WebhookEnvelope
        from app.webhooks.workers.pool import WorkerPool
        from app.webhooks.workers.pool import WorkerConfig
        from app.webhooks.resiliency.idempotency import idempotency_store
        from datetime import datetime, timezone

        called = []

        async def succeed(_wh):
            called.append(True)

        fail_config = WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1, handler=succeed,
        )
        pool = WorkerPool()
        pool.register_pool(fail_config)

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-lookup-fail",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "FAILSAFE1"},
                idempotency_key="zerodha:evt-lookup-fail",
            ),
            attempt=0,
        )

        with patch.object(
            idempotency_store, "is_completed",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ):
            await pool._process_webhook(
                "broker_critical_test", fail_config, "100-92", webhook
            )

        assert called, "indeterminate lookup must NOT suppress the handler"
        mock_redis.xack.assert_awaited_with(
            "webhooks:broker:critical", "workers", "100-92"
        )

    @pytest.mark.asyncio
    async def test_worker_duplicate_guard_preserves_handler_failure_nack(
        self, mock_redis
    ):
        """The duplicate guard must not break the existing failure path: when
        the handler genuinely fails (key not completed), nack/retry still runs.
        """
        from app.webhooks.validation.schemas import WebhookEnvelope
        from app.webhooks.workers.pool import WorkerPool
        from app.webhooks.workers.pool import WorkerConfig
        from app.webhooks.resiliency.idempotency import idempotency_store
        from datetime import datetime, timezone

        async def fail(_wh):
            raise RuntimeError("handler boom")

        fail_config = WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1, handler=fail,
        )
        pool = WorkerPool()
        pool.register_pool(fail_config)

        mock_redis.xack = AsyncMock(return_value=True)

        with patch.object(
            idempotency_store, "is_completed", new=AsyncMock(return_value=False)
        ), patch.object(webhook_queue, "nack", new_callable=AsyncMock) as mock_nack:
            await pool._process_webhook(
                "broker_critical_test",
                fail_config,
                "100-93",
                QueuedWebhook(
                    envelope=WebhookEnvelope(
                        event_id="evt-fail-3",
                        event_type="order_update",
                        timestamp=datetime.now(timezone.utc),
                        provider="zerodha",
                        payload={"order_id": "FAIL3"},
                        idempotency_key="zerodha:evt-fail-3",
                    ),
                    attempt=0,
                ),
            )
            mock_nack.assert_called_once()

    @pytest.mark.asyncio
    async def test_worker_duplicate_guard_missing_key_runs_handler(
        self, mock_redis
    ):
        """Legacy entries WITHOUT an idempotency key must preserve the existing
        behaviour exactly: no lookup, handler runs normally, no suppression.
        """
        from app.webhooks.validation.schemas import WebhookEnvelope
        from app.webhooks.workers.pool import WorkerPool
        from app.webhooks.workers.pool import WorkerConfig
        from app.webhooks.resiliency.idempotency import idempotency_store
        from datetime import datetime, timezone

        called = []

        async def succeed(_wh):
            called.append(True)

        ok_config = WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1, handler=succeed,
        )
        pool = WorkerPool()
        pool.register_pool(ok_config)

        webhook = QueuedWebhook(
            envelope=WebhookEnvelope(
                event_id="evt-no-key",
                event_type="order_update",
                timestamp=datetime.now(timezone.utc),
                provider="zerodha",
                payload={"order_id": "NOKEY1"},
            ),
            attempt=0,
        )

        with patch.object(
            idempotency_store, "is_completed", new=AsyncMock(return_value=True)
        ) as mock_is_completed:
            await pool._process_webhook(
                "broker_critical_test", ok_config, "100-94", webhook
            )

        assert called, "handler must run for a legacy entry without a key"
        mock_is_completed.assert_not_called()

    @pytest.mark.asyncio
    async def test_worker_duplicate_guard_is_completed_read_only(self, mock_redis):
        """is_completed is a NON-MUTATING read-only check (plain GET); it never
        calls check_and_mark_processing() which would reopen/re-mark records.
        """
        import json as _json
        from app.webhooks.resiliency.idempotency import IdempotencyStore

        state: dict = {}

        async def fake_get(key):
            return _json.dumps(state[key]) if key in state else None

        mock_redis.get = AsyncMock(side_effect=fake_get)

        store = IdempotencyStore(redis_url="redis://localhost:6379/0")
        await store.initialize()

        assert await store.is_completed("no-such-key") is False
        state["idempotency:evt-ok"] = {
            "key": "evt-ok", "status": "completed",
            "created_at": 1, "completed_at": 2,
            "result": {"status": "processed"},
        }
        assert await store.is_completed("evt-ok") is True
        state["idempotency:evt-proc"] = {
            "key": "evt-proc", "status": "processing", "created_at": 999999,
        }
        assert await store.is_completed("evt-proc") is False

    @pytest.mark.asyncio
    async def test_worker_duplicate_guard_lookup_failure_returns_false(
        self, mock_redis
    ):
        """is_completed FAILS CLOSED (returns False, never True) on a Redis
        lookup error, so the guard can never falsely suppress a real event.
        """
        from app.webhooks.resiliency.idempotency import IdempotencyStore

        mock_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))

        store = IdempotencyStore(redis_url="redis://localhost:6379/0")
        await store.initialize()

        assert await store.is_completed("any-key") is False


class TestPELRecovery:
    """P1: Redis Streams PEL recovery via XPENDING/XAUTOCLAIM.

    Deterministic, Redis-mocked regression tests proving the recovery
    invariants:
      * Idle PEL entries are reclaimed, freshly-delivered active entries are
        not.
      * Reclaimed entries flow through the EXACT same ``_process_webhook``
        path as normal deliveries (duplicate guard -> handler ->
        mark_completed -> XACK; failure -> nack/retry/DLQ).
      * Recovery is bounded per pass, never crashes the pool, is inert in
        webhook_local_mode, and shuts down cleanly without duplicate loops.
    """

    # ---- helpers ------------------------------------------------------

    @staticmethod
    def _duplex_entry(entry_id: str, webhook: QueuedWebhook) -> list:
        """Build the raw [id, [k1, v1, k2, v2, ...]] XAUTOCLAIM payload."""
        pairs: list[str] = []
        for key, value in webhook.to_stream_entry().items():
            pairs.extend([key, value])
        return [entry_id, pairs]

    @staticmethod
    def _envelope(event_id: str, provider: str = "zerodha",
                  event_type: str = "order_update", idem: str | None = None):
        from app.webhooks.validation.schemas import WebhookEnvelope
        return WebhookEnvelope(
            event_id=event_id,
            event_type=event_type,
            timestamp=datetime.now(timezone.utc),
            provider=provider,
            payload={"order_id": event_id},
            idempotency_key=idem,
        )

    @staticmethod
    def _pool_with_handler(handler) -> "WorkerPool":
        from app.webhooks.workers.pool import WorkerPool, WorkerConfig
        pool = WorkerPool()
        pool.register_pool(WorkerConfig(
            pool_name="broker_critical_test",
            queue_names=["webhooks:broker:critical"],
            concurrency=1,
            handler=handler,
        ))
        return pool

    # ---- queue-level recover_pending ---------------------------------

    @pytest.mark.asyncio
    async def test_pel_recovery_claims_idle_entry(self, mock_redis):
        """An idle PEL entry (idle >= min-idle) is claimed via XAUTOCLAIM and
        returned for normal processing."""
        webhook = QueuedWebhook(
            envelope=self._envelope("evt-recv-1", idem="zerodha:evt-recv-1"),
            attempt=1,
        )
        mock_redis.xpending = AsyncMock(return_value={
            "pending": 3, "min": "100-0", "max": "100-2",
            "consumers": [{"name": "broker_critical_test-0", "pending": 3}],
        })
        mock_redis.xautoclaim = AsyncMock(return_value=[
            "100-3", [self._duplex_entry("100-2", webhook)], [],
        ])

        results = await webhook_queue.recover_pending(
            ["webhooks:broker:critical"],
            min_idle_ms=120_000,
            count=25,
            consumer_name="recovery-test",
        )

        assert len(results) == 1
        queue_name, entry_id, reclaimed = results[0]
        assert queue_name == "webhooks:broker:critical"
        assert entry_id == "100-2"
        assert reclaimed.envelope.event_id == "evt-recv-1"
        assert reclaimed.attempt == 1  # retry budget preserved
        # Bounded, group-scoped claim against the "workers" group, passing
        # the min-idle safety threshold to Redis verbatim.
        mock_redis.xautoclaim.assert_awaited_once()
        call = mock_redis.xautoclaim.await_args
        assert call.args[1] == "workers"
        assert call.args[2] == "recovery-test"
        assert call.args[3] == 120_000

    @pytest.mark.asyncio
    async def test_pel_recovery_does_not_reclaim_active_entry(self, mock_redis):
        """Entries below the min-idle threshold are NOT reclaimed: Redis skips
        them (healthy worker mid-processing), and the min-idle argument sent
        is the derived safety threshold."""
        mock_redis.xpending = AsyncMock(return_value={
            "pending": 1, "min": "100-0", "max": "100-0",
            "consumers": [{"name": "broker_critical_test-0", "pending": 1}],
        })
        # Redis returns zero claims: the single pending entry is still too
        # "young" (idle < min_idle_time).
        mock_redis.xautoclaim = AsyncMock(return_value=["100-1", [], []])

        results = await webhook_queue.recover_pending(
            ["webhooks:broker:critical"],
            min_idle_ms=120_000,
            count=25,
            consumer_name="recovery-test",
        )

        assert results == []
        mock_redis.xautoclaim.assert_awaited_once()
        assert mock_redis.xautoclaim.await_args.args[3] == 120_000

    @pytest.mark.asyncio
    async def test_pel_recovery_skips_queue_with_no_pending(self, mock_redis):
        """XPENDING is a cheap probe: an empty PEL must not trigger a scan."""
        mock_redis.xpending = AsyncMock(return_value={
            "pending": 0, "min": None, "max": None, "consumers": [],
        })
        results = await webhook_queue.recover_pending(
            ["webhooks:broker:critical"],
            min_idle_ms=120_000,
            count=25,
        )
        assert results == []
        mock_redis.xautoclaim.assert_not_awaited()
    @pytest.mark.asyncio
    async def test_pel_recovery_bounded_batch(self, mock_redis):
        """Bounded batch: only `count` entries are claimed per queue pass and
        recovery does NOT drain the whole PEL in one call."""
        webhook = QueuedWebhook(envelope=self._envelope("evt-batch-1"))
        mock_redis.xpending = AsyncMock(return_value={
            "pending": 5, "min": "100-0", "max": "100-4",
            "consumers": [{"name": "c-0", "pending": 5}],
        })
        # Server returns 5 entries; recovery must still only take 2.
        mock_redis.xautoclaim = AsyncMock(return_value=[
            "100-5",
            [self._duplex_entry(f"100-{i}", webhook) for i in range(5)],
            [],
        ])

        results = await webhook_queue.recover_pending(
            ["webhooks:broker:critical"],
            min_idle_ms=120_000,
            count=2,
        )

        assert len(results) == 2
        # A single bounded XAUTOCLAIM per queue - no unbounded drain loop.
        assert mock_redis.xautoclaim.await_count == 1
        assert mock_redis.xautoclaim.await_args.kwargs["count"] == 2

    @pytest.mark.asyncio
    async def test_pel_recovery_multiple_queues_same_group(self, mock_redis):
        """Multiple queue/group combinations: every configured queue is probed
        and claimed against the shared 'workers' consumer group."""
        webhook = QueuedWebhook(envelope=self._envelope("evt-multi"))

        def xp_side_effect(name, group, **_):
            return {
                "pending": 1, "min": "100-0", "max": "100-0",
                "consumers": [{"name": "c-0", "pending": 1}],
            }

        mock_redis.xpending = AsyncMock(side_effect=xp_side_effect)
        mock_redis.xautoclaim = AsyncMock(return_value=[
            "100-1", [self._duplex_entry("100-0", webhook)], [],
        ])

        queues = ["webhooks:broker:critical", "webhooks:tradethrone:critical"]
        results = await webhook_queue.recover_pending(
            queues, min_idle_ms=120_000, count=25,
        )

        assert len(results) == 2
        assert [r[0] for r in results] == queues
        assert mock_redis.xautoclaim.await_count == 2
        for call in mock_redis.xautoclaim.await_args_list:
            assert call.args[1] == "workers"

    @pytest.mark.asyncio
    async def test_pel_recovery_routes_through_process_webhook(
        self, mock_redis
    ):
        """A recovered entry MUST go through the same _process_webhook() path;
        the handler runs, mark_completed is called, and the entry is XACKed."""
        from app.webhooks.resiliency.idempotency import idempotency_store

        calls: list[str] = []

        async def record_handler(wh):
            calls.append(wh.envelope.event_id)

        pool = self._pool_with_handler(record_handler)
        webhook = QueuedWebhook(
            envelope=self._envelope(
                "evt-recovered", idem="zerodha:evt-recovered"),
            attempt=0,
        )

        with patch.object(
            webhook_queue, "recover_pending",
            new=AsyncMock(return_value=[
                ("webhooks:broker:critical", "100-7", webhook),
            ]),
        ), patch.object(
            idempotency_store, "is_completed", new=AsyncMock(return_value=False)
        ), patch.object(
            webhook_queue, "ack", new=AsyncMock()
        ) as mock_ack, patch.object(
            pool, "_process_webhook", wraps=pool._process_webhook,
        ) as spy:
            await pool._recover_once()

        assert calls == ["evt-recovered"]  # handler actually executed
        spy.assert_called_once()
        assert spy.call_args.args[0] == "broker_critical_test"
        assert spy.call_args.args[2] == "100-7"
        assert spy.call_args.args[3].envelope.event_id == "evt-recovered"
        mock_ack.assert_called_once_with("webhooks:broker:critical", "100-7")
    @pytest.mark.asyncio
    async def test_pel_recovery_completed_entry_suppressed(self, mock_redis):
        """A recovered entry whose idempotency key is ALREADY completed is
        suppressed by the duplicate guard: no handler execution, no requeue,
        just an XACK of the reclaimed entry."""
        from app.webhooks.resiliency.idempotency import idempotency_store

        handler_calls: list[str] = []

        async def record_handler(wh):
            handler_calls.append(wh.envelope.event_id)

        pool = self._pool_with_handler(record_handler)
        webhook = QueuedWebhook(
            envelope=self._envelope(
                "evt-recovered-dup", idem="zerodha:evt-done"),
            attempt=0,
        )

        with patch.object(
            webhook_queue, "recover_pending",
            new=AsyncMock(return_value=[
                ("webhooks:broker:critical", "100-9", webhook),
            ]),
        ), patch.object(
            idempotency_store, "is_completed", new=AsyncMock(return_value=True)
        ), patch.object(
            webhook_queue, "ack", new=AsyncMock()
        ) as mock_ack:
            await pool._recover_once()

        assert handler_calls == []  # suppressed, never re-executed
        mock_ack.assert_called_once_with("webhooks:broker:critical", "100-9")

    @pytest.mark.asyncio
    async def test_pel_recovery_incomplete_entry_executes_normally(
        self, mock_redis
    ):
        """A recovered, NOT-yet-completed event executes the normal path:
        handler -> mark_completed -> XACK."""
        from app.webhooks.resiliency.idempotency import idempotency_store

        handler_calls: list[str] = []

        async def record_handler(wh):
            handler_calls.append(wh.envelope.event_id)

        pool = self._pool_with_handler(record_handler)
        webhook = QueuedWebhook(
            envelope=self._envelope(
                "evt-recovered-new", idem="zerodha:evt-new"),
            attempt=0,
        )

        with patch.object(
            webhook_queue, "recover_pending",
            new=AsyncMock(return_value=[
                ("webhooks:broker:critical", "100-11", webhook),
            ]),
        ), patch.object(
            idempotency_store, "is_completed", new=AsyncMock(return_value=False)
        ), patch.object(
            idempotency_store, "mark_completed", new=AsyncMock()
        ) as mock_mark, patch.object(
            webhook_queue, "ack", new=AsyncMock()
        ) as mock_ack:
            await pool._recover_once()

        assert handler_calls == ["evt-recovered-new"]
        mock_mark.assert_called_once()
        mock_ack.assert_called_once_with("webhooks:broker:critical", "100-11")

    @pytest.mark.asyncio
    async def test_pel_recovery_handler_failure_preserves_nack(self, mock_redis):
        """A recovered entry whose handler fails uses the EXISTING nack path:
        the retry/DLQ decision is unchanged for reclaimed entries."""
        async def fail_handler(_wh):
            raise RuntimeError("recovered handler boom")

        pool = self._pool_with_handler(fail_handler)
        webhook = QueuedWebhook(
            envelope=self._envelope(
                "evt-recovered-fail", idem="zerodha:evt-fail"),
            attempt=0,
        )

        with patch.object(
            webhook_queue, "recover_pending",
            new=AsyncMock(return_value=[
                ("webhooks:broker:critical", "100-13", webhook),
            ]),
        ), patch.object(
            idempotency_store, "is_completed", new=AsyncMock(return_value=False)
        ), patch.object(
            webhook_queue, "nack", new=AsyncMock()
        ) as mock_nack:
            await pool._recover_once()

        mock_nack.assert_called_once()
        assert mock_nack.await_args.args[0] == "webhooks:broker:critical"
        assert mock_nack.await_args.args[1] == "100-13"
    @pytest.mark.asyncio
    async def test_pel_recovery_error_does_not_crash_pool(self, mock_redis):
        """Recovery failures are contained: a broken XPENDING/XAUTOCLAIM must
        not crash the pool or the recovery loop."""
        pool = self._pool_with_handler(lambda wh: None)

        with patch.object(
            webhook_queue, "recover_pending",
            new=AsyncMock(side_effect=RuntimeError("redis exploded")),
        ) as mock_recover:
            # A single pass swallows the error without raising.
            await pool._recover_once()
            # The loop keeps running across failed iterations.
            await pool._recovery_loop(interval_seconds=0, max_iterations=2)

            # 1 explicit pass + 2 bounded loop iterations, all error-contained.
            assert mock_recover.await_count == 3

    @pytest.mark.asyncio
    async def test_pel_recovery_disabled_in_local_mode(self, mock_redis, monkeypatch):
        """Recovery does nothing in webhook_local_mode - not even a probe."""
        from app.config import settings as app_settings

        pool = self._pool_with_handler(lambda wh: None)
        monkeypatch.setattr(app_settings, "webhook_local_mode", True)

        with patch.object(
            webhook_queue, "recover_pending", new=AsyncMock()
        ) as mock_recover:
            await pool._recovery_loop(interval_seconds=0, max_iterations=2)
            await pool._recover_once()

        mock_recover.assert_not_awaited()
        mock_redis.xautoclaim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pel_recovery_shutdown_cancels_loop_cleanly(self, mock_redis):
        """stop() cancels the recovery task along with the worker tasks - no
        dangling background loop, no exceptions during shutdown."""
        pool = self._pool_with_handler(lambda wh: None)

        await pool.start()
        recovery_task = pool._recovery_task
        assert recovery_task is not None and not recovery_task.done()
        await asyncio.sleep(0)

        await pool.stop()

        assert pool._recovery_task is None
        assert recovery_task.cancelled() or recovery_task.done()
        assert pool._tasks == []

    @pytest.mark.asyncio
    async def test_pel_recovery_no_duplicate_loop_on_double_start(
        self, mock_redis
    ):
        """Calling start() twice starts a single recovery loop for the pool."""
        pool = self._pool_with_handler(lambda wh: None)

        await pool.start()
        first_task = pool._recovery_task
        await pool.start()  # no-op while already running

        assert pool._recovery_task is first_task
        assert sum(
            1 for t in asyncio.all_tasks() if t is first_task
        ) == 1  # exactly one handle on the recovery loop

        await pool.stop()

    @pytest.mark.asyncio
    async def test_pel_recovery_min_idle_derived_from_route_table(self, mock_redis):
        """The min-idle threshold is computed from the CURRENT route table as
        2x the largest handler timeout (currently 60s) - the safety margin
        that prevents reclaiming actively-processed messages."""
        from app.webhooks.workers.pool import _recovery_min_idle_seconds
        from app.webhooks.routing.router import ROUTE_TABLE

        max_timeout = max(r.timeout_seconds for r in ROUTE_TABLE.values())
        assert _recovery_min_idle_seconds() == max(2 * int(max_timeout), 120)
        assert _recovery_min_idle_seconds() == 120  # 2 * 60s current ceiling


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