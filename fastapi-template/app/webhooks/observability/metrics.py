"""Prometheus metrics for webhook platform."""

from __future__ import annotations

from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry
from app.core.logging import get_logger

logger = get_logger("webhook.metrics")

# Custom registry for webhook metrics
WEBHOOK_REGISTRY = CollectorRegistry()

# Counters
webhook_received_total = Counter(
    "webhook_received_total",
    "Total webhooks received",
    ["provider", "event_type", "status"],
    registry=WEBHOOK_REGISTRY,
)

webhook_processed_total = Counter(
    "webhook_processed_total",
    "Total webhooks processed successfully",
    ["provider", "event_type", "worker_pool"],
    registry=WEBHOOK_REGISTRY,
)

webhook_failed_total = Counter(
    "webhook_failed_total",
    "Total webhooks failed after retries",
    ["provider", "event_type", "error_type"],
    registry=WEBHOOK_REGISTRY,
)

webhook_dlq_total = Counter(
    "webhook_dlq_total",
    "Total webhooks sent to dead letter queue",
    ["provider", "event_type"],
    registry=WEBHOOK_REGISTRY,
)

webhook_retried_total = Counter(
    "webhook_retried_total",
    "Total webhook retries",
    ["provider", "event_type", "attempt"],
    registry=WEBHOOK_REGISTRY,
)

# Histograms
webhook_processing_duration = Histogram(
    "webhook_processing_duration_seconds",
    "Webhook processing duration",
    ["provider", "event_type", "worker_pool"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=WEBHOOK_REGISTRY,
)

webhook_queue_latency = Histogram(
    "webhook_queue_latency_seconds",
    "Time from enqueue to dequeue",
    ["queue_name"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    registry=WEBHOOK_REGISTRY,
)

# Gauges
webhook_queue_depth = Gauge(
    "webhook_queue_depth",
    "Current number of pending webhooks in queue",
    ["queue_name"],
    registry=WEBHOOK_REGISTRY,
)

webhook_worker_active = Gauge(
    "webhook_worker_active",
    "Number of active workers per pool",
    ["worker_pool"],
    registry=WEBHOOK_REGISTRY,
)

webhook_circuit_state = Gauge(
    "webhook_circuit_state",
    "Circuit breaker state (0=closed, 1=half-open, 2=open)",
    ["circuit_name"],
    registry=WEBHOOK_REGISTRY,
)

# Metrics helper functions
def record_webhook_received(provider: str, event_type: str, status: str) -> None:
    webhook_received_total.labels(provider=provider, event_type=event_type, status=status).inc()

def record_webhook_processed(provider: str, event_type: str, worker_pool: str) -> None:
    webhook_processed_total.labels(provider=provider, event_type=event_type, worker_pool=worker_pool).inc()

def record_webhook_failed(provider: str, event_type: str, error_type: str) -> None:
    webhook_failed_total.labels(provider=provider, event_type=event_type, error_type=error_type).inc()

def record_webhook_dlq(provider: str, event_type: str) -> None:
    webhook_dlq_total.labels(provider=provider, event_type=event_type).inc()

def record_webhook_retried(provider: str, event_type: str, attempt: int) -> None:
    webhook_retried_total.labels(provider=provider, event_type=event_type, attempt=str(attempt)).inc()

def observe_processing_duration(provider: str, event_type: str, worker_pool: str, duration: float) -> None:
    webhook_processing_duration.labels(provider=provider, event_type=event_type, worker_pool=worker_pool).observe(duration)

def observe_queue_latency(queue_name: str, latency: float) -> None:
    webhook_queue_latency.labels(queue_name=queue_name).observe(latency)

def set_queue_depth(queue_name: str, depth: int) -> None:
    webhook_queue_depth.labels(queue_name=queue_name).set(depth)

def set_worker_active(worker_pool: str, active: int) -> None:
    webhook_worker_active.labels(worker_pool=worker_pool).set(active)

def set_circuit_state(circuit_name: str, state: int) -> None:
    webhook_circuit_state.labels(circuit_name=circuit_name).set(state)