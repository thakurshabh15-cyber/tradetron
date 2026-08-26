"""Webhook routing - route resolution and envelope enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.webhooks.validation.schemas import WebhookEnvelope
from app.core.logging import get_logger

logger = get_logger("webhook.routing")


class WebhookPriority(Enum):
    CRITICAL = 0    # Order fills, payment captured - process immediately
    HIGH = 1        # Order updates, subscription changes
    NORMAL = 2      # Market data, notifications
    LOW = 3         # Analytics, audit logs


class WebhookCategory(Enum):
    BROKER_POSTBACK = "broker_postback"
    BILLING = "billing"
    MARKET_DATA = "market_data"
    CUSTOM = "custom"


@dataclass(frozen=True)
class RouteConfig:
    category: WebhookCategory
    priority: WebhookPriority
    queue_name: str
    worker_pool: str
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    timeout_seconds: float = 30.0
    requires_idempotency: bool = True


# Route mapping table
ROUTE_TABLE: dict[tuple[str, str], RouteConfig] = {
    # Broker postbacks - CRITICAL priority
    ("zerodha", "order_update"): RouteConfig(
        category=WebhookCategory.BROKER_POSTBACK,
        priority=WebhookPriority.CRITICAL,
        queue_name="webhooks:broker:critical",
        worker_pool="broker_critical",
        max_retries=5,
        retry_delay_seconds=0.5,
        timeout_seconds=10.0,
    ),
    ("upstox", "order_update"): RouteConfig(
        category=WebhookCategory.BROKER_POSTBACK,
        priority=WebhookPriority.CRITICAL,
        queue_name="webhooks:broker:critical",
        worker_pool="broker_critical",
        max_retries=5,
        retry_delay_seconds=0.5,
        timeout_seconds=10.0,
    ),
    ("angel_one", "order_update"): RouteConfig(
        category=WebhookCategory.BROKER_POSTBACK,
        priority=WebhookPriority.CRITICAL,
        queue_name="webhooks:broker:critical",
        worker_pool="broker_critical",
        max_retries=5,
        retry_delay_seconds=0.5,
        timeout_seconds=10.0,
    ),
    ("binance", "order_update"): RouteConfig(
        category=WebhookCategory.BROKER_POSTBACK,
        priority=WebhookPriority.CRITICAL,
        queue_name="webhooks:broker:critical",
        worker_pool="broker_critical",
        max_retries=5,
        retry_delay_seconds=0.5,
        timeout_seconds=10.0,
    ),
    
    # TradeThrone signals - HIGH priority (signals), NORMAL (position updates)
    ("tradethrone", "signal"): RouteConfig(
        category=WebhookCategory.CUSTOM,
        priority=WebhookPriority.HIGH,
        queue_name="webhooks:tradethrone:high",
        worker_pool="tradethrone_high",
        max_retries=3,
        retry_delay_seconds=1.0,
        timeout_seconds=30.0,
    ),
    ("tradethrone", "position_update"): RouteConfig(
        category=WebhookCategory.CUSTOM,
        priority=WebhookPriority.NORMAL,
        queue_name="webhooks:tradethrone:normal",
        worker_pool="tradethrone_normal",
        max_retries=3,
        retry_delay_seconds=2.0,
        timeout_seconds=60.0,
    ),
    ("tradethrone", "strategy_status"): RouteConfig(
        category=WebhookCategory.CUSTOM,
        priority=WebhookPriority.NORMAL,
        queue_name="webhooks:tradethrone:normal",
        worker_pool="tradethrone_normal",
        max_retries=3,
        retry_delay_seconds=2.0,
        timeout_seconds=60.0,
    ),
    ("tradethrone", "risk_alert"): RouteConfig(
        category=WebhookCategory.CUSTOM,
        priority=WebhookPriority.CRITICAL,
        queue_name="webhooks:tradethrone:critical",
        worker_pool="tradethrone_critical",
        max_retries=5,
        retry_delay_seconds=0.5,
        timeout_seconds=10.0,
    ),
    
    # Billing webhooks - HIGH priority
    ("razorpay", "payment.captured"): RouteConfig(
        category=WebhookCategory.BILLING,
        priority=WebhookPriority.HIGH,
        queue_name="webhooks:billing:high",
        worker_pool="billing_high",
        max_retries=3,
        retry_delay_seconds=1.0,
        timeout_seconds=30.0,
    ),
    ("razorpay", "payment.failed"): RouteConfig(
        category=WebhookCategory.BILLING,
        priority=WebhookPriority.HIGH,
        queue_name="webhooks:billing:high",
        worker_pool="billing_high",
        max_retries=3,
        retry_delay_seconds=1.0,
        timeout_seconds=30.0,
    ),
    ("razorpay", "subscription.charged"): RouteConfig(
        category=WebhookCategory.BILLING,
        priority=WebhookPriority.HIGH,
        queue_name="webhooks:billing:high",
        worker_pool="billing_high",
        max_retries=3,
        retry_delay_seconds=1.0,
        timeout_seconds=30.0,
    ),
    ("razorpay", "subscription.cancelled"): RouteConfig(
        category=WebhookCategory.BILLING,
        priority=WebhookPriority.HIGH,
        queue_name="webhooks:billing:high",
        worker_pool="billing_high",
        max_retries=3,
        retry_delay_seconds=1.0,
        timeout_seconds=30.0,
    ),
    
    # Default fallback
    ("*", "*"): RouteConfig(
        category=WebhookCategory.CUSTOM,
        priority=WebhookPriority.NORMAL,
        queue_name="webhooks:custom:normal",
        worker_pool="custom_normal",
        max_retries=3,
        retry_delay_seconds=2.0,
        timeout_seconds=60.0,
    ),
}


def resolve_route(provider: str, event_type: str) -> RouteConfig:
    """Resolve route configuration for a webhook event"""
    # Exact match first
    key = (provider.lower(), event_type.lower())
    if key in ROUTE_TABLE:
        return ROUTE_TABLE[key]
    
    # Provider wildcard
    key = (provider.lower(), "*")
    if key in ROUTE_TABLE:
        return ROUTE_TABLE[key]
    
    # Global fallback
    return ROUTE_TABLE[("*", "*")]


async def enrich_envelope(envelope: WebhookEnvelope) -> WebhookEnvelope:
    """Enrich envelope with routing info and metadata"""
    route = resolve_route(envelope.provider, envelope.event_type)
    
    # Add routing metadata to payload for workers
    enriched_payload = {
        **envelope.payload,
        "_routing": {
            "category": route.category.value,
            "priority": route.priority.value,
            "queue": route.queue_name,
            "worker_pool": route.worker_pool,
            "max_retries": route.max_retries,
            "retry_delay": route.retry_delay_seconds,
            "timeout": route.timeout_seconds,
        }
    }
    
    return WebhookEnvelope(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        timestamp=envelope.timestamp,
        provider=envelope.provider,
        payload=enriched_payload,
        idempotency_key=envelope.idempotency_key,
    )