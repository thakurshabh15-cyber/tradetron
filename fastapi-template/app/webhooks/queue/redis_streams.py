"""Redis Streams based webhook queue with priority lanes."""

from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass, asdict
from typing import Any, Optional
from datetime import datetime, timezone
import redis.asyncio as redis
from redis.asyncio import Redis

from app.webhooks.validation.schemas import WebhookEnvelope
from app.webhooks.routing.router import resolve_route
from app.config import settings
from app.core.logging import get_logger

logger = get_logger("webhook.queue")


@dataclass
class QueuedWebhook:
    """Webhook event in the queue"""
    envelope: WebhookEnvelope
    attempt: int = 0
    queued_at: datetime = None
    last_error: str | None = None
    
    def __post_init__(self):
        if self.queued_at is None:
            self.queued_at = datetime.now(timezone.utc)
    
    def to_stream_entry(self) -> dict[str, str]:
        return {
            "envelope": self.envelope.model_dump_json(),
            "attempt": str(self.attempt),
            "queued_at": self.queued_at.isoformat(),
            "last_error": self.last_error or "",
        }
    
    @classmethod
    def from_stream_entry(cls, entry_id: str, data: dict[str, str]) -> "QueuedWebhook":
        envelope_data = json.loads(data["envelope"])
        # Reconstruct WebhookEnvelope (simplified)
        from app.webhooks.validation.schemas import WebhookEnvelope
        envelope = WebhookEnvelope(**envelope_data)
        return cls(
            envelope=envelope,
            attempt=int(data["attempt"]),
            queued_at=datetime.fromisoformat(data["queued_at"]),
            last_error=data["last_error"] or None,
        )


class WebhookQueue:
    """Redis Streams based webhook queue with priority lanes"""
    
    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or settings.redis_url or "redis://localhost:6379/0"
        self._redis: Redis | None = None
        self._consumer_groups: dict[str, str] = {}
        self._initialized = False
    
    async def initialize(self) -> None:
        try:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
            await self._redis.ping()
            # Create consumer groups for each queue
            for queue_name in [
                "webhooks:broker:critical",
                "webhooks:billing:high", 
                "webhooks:custom:normal",
                "webhooks:dlq",  # Dead letter queue
            ]:
                try:
                    await self._redis.xgroup_create(queue_name, "workers", id="0", mkstream=True)
                except redis.ResponseError as e:
                    if "BUSYGROUP" not in str(e):
                        raise
            self._initialized = True
            logger.info("Webhook queue initialized with Redis Streams")
        except Exception as e:
            logger.warning("Redis unavailable, running in degraded mode (no queue): %s", e)
            self._initialized = False
            self._redis = None
    
    async def enqueue(self, envelope: WebhookEnvelope, priority: int = 2) -> str:
        """Enqueue webhook event with priority routing"""
        if not self._initialized or not self._redis:
            logger.warning("Queue not initialized, skipping enqueue for %s", envelope.event_id)
            return "local-mode"
        
        route = resolve_route(envelope.provider, envelope.event_type)
        queue_name = route.queue_name
        
        queued = QueuedWebhook(envelope=envelope)
        entry_id = await self._redis.xadd(queue_name, queued.to_stream_entry())
        
        # Track metrics
        await self._redis.hincrby("webhook:metrics:enqueued", queue_name, 1)
        await self._redis.hincrby("webhook:metrics:enqueued", "total", 1)
        
        logger.debug("Enqueued webhook %s to %s (entry: %s)", envelope.event_id, queue_name, entry_id)
        return entry_id
    
    async def dequeue(
        self, 
        worker_pool: str, 
        queue_names: list[str], 
        count: int = 10,
        block_ms: int = 5000
    ) -> list[tuple[str, QueuedWebhook]]:
        """Dequeue events from multiple queues with priority (blocking read)"""
        if not self._initialized or not self._redis:
            return []
        # Read from highest priority queue first
        for queue_name in queue_names:
            try:
                streams = {queue_name: ">"}
                results = await self._redis.xreadgroup(
                    groupname="workers",
                    consumername=worker_pool,
                    streams=streams,
                    count=count,
                    block=block_ms,
                )
                
                if results:
                    events = []
                    for stream_name, entries in results:
                        for entry_id, data in entries:
                            events.append((entry_id, QueuedWebhook.from_stream_entry(entry_id, data)))
                    return events
            except Exception as e:
                logger.error("Dequeue error from %s: %s", queue_name, e)
        
        return []
    
    async def ack(self, queue_name: str, entry_id: str) -> None:
        """Acknowledge successful processing"""
        if not self._initialized or not self._redis:
            return
        await self._redis.xack(queue_name, "workers", entry_id)
        await self._redis.hincrby("webhook:metrics:processed", queue_name, 1)
        await self._redis.hincrby("webhook:metrics:processed", "total", 1)
    
    async def nack(self, queue_name: str, entry_id: str, webhook: QueuedWebhook, error: str) -> None:
        """Negative acknowledgment - requeue or send to DLQ"""
        if not self._initialized or not self._redis:
            return
        route = resolve_route(webhook.envelope.provider, webhook.envelope.event_type)
        
        if webhook.attempt >= route.max_retries:
            # Send to DLQ
            await self._send_to_dlq(webhook, error)
        else:
            # Requeue with incremented attempt
            webhook.attempt += 1
            webhook.last_error = error
            await self._redis.xadd(queue_name, webhook.to_stream_entry())
            await self._redis.hincrby("webhook:metrics:retried", queue_name, 1)
    
    async def _send_to_dlq(self, webhook: QueuedWebhook, error: str) -> None:
        """Send failed webhook to dead letter queue"""
        if not self._initialized or not self._redis:
            return
        dlq_entry = {
            **webhook.to_stream_entry(),
            "final_error": error,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "original_queue": resolve_route(webhook.envelope.provider, webhook.envelope.event_type).queue_name,
        }
        await self._redis.xadd("webhooks:dlq", dlq_entry)
        await self._redis.hincrby("webhook:metrics:dlq", "total", 1)
        logger.error("Webhook %s sent to DLQ after %d attempts: %s", 
                     webhook.envelope.event_id, webhook.attempt, error)
    
    async def health_check(self) -> bool:
        if not self._initialized or not self._redis:
            return False
        try:
            await self._redis.ping()
            return True
        except Exception:
            return False
    
    async def shutdown(self) -> None:
        if self._redis:
            await self._redis.close()


# Global queue instance
webhook_queue = WebhookQueue()