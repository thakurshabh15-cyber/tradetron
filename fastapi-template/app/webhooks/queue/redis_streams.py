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
from app.webhooks.routing.router import resolve_route, ROUTE_TABLE
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
        self.redis_url = redis_url or settings.effective_redis_url or "redis://localhost:6379/0"
        self._redis: Redis | None = None
        self._consumer_groups: dict[str, str] = {}
        self._initialized = False
    
    async def initialize(self) -> None:
        try:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
            await self._redis.ping()
            # Derive consumer-group queue names from the routing table so
            # every route declared in ROUTE_TABLE is guaranteed a consumer
            # group.  The DLQ is always included as well.
            _routed_queues = {route.queue_name for route in ROUTE_TABLE.values()}
            _all_queues = sorted(_routed_queues | {"webhooks:dlq"})
            for queue_name in _all_queues:
                try:
                    await self._redis.xgroup_create(queue_name, "workers", id="0", mkstream=True)
                except redis.ResponseError as e:
                    if "BUSYGROUP" not in str(e):
                        raise
            self._initialized = True
            logger.info(
                "Webhook queue initialized with Redis Streams (%d consumer groups)",
                len(_all_queues),
            )
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
    
    async def recover_pending(
        self,
        queue_names: list[str],
        min_idle_ms: int,
        count: int = 25,
        consumer_name: str = "recovery",
    ) -> list[tuple[str, str, QueuedWebhook]]:
        """Bounded PEL recovery via XPENDING + XAUTOCLAIM.

        Returns ``[(queue_name, entry_id, QueuedWebhook)]`` for every entry
        reclaimed this pass.  This is the ONLY re-delivery mechanism for
        entries stranded in the PEL (e.g. a worker that crashed after XREADGROUP
        delivered the entry but before XACK).  The worker pool feeds every
        recovered entry through the SAME ``_process_webhook()`` path as
        normally-consumed entries, so the duplicate-suppression guard,
        mark_completed-before-XACK ordering, and nack/retry/DLQ semantics all
        apply unchanged.

        Safety properties:
          * ``min_idle_ms`` — an entry is only reclaimed once it has been
            idle (undelivered-but-unacked) for at least this many milliseconds.
            This is what prevents reclaiming an entry another worker is still
            actively processing.
          * ``count`` — bounded batch per queue per pass: at most ``count``
            entries are claimed from any single queue, so a corrupted/large PEL
            is drained incrementally instead of blasting every pending entry
            into the workers in one cycle.
          * XPENDING is used only as a cheap "is there anything pending at
            all?" guard before running XAUTOCLAIM, avoiding needless scans.
          * Errors are contained per queue: one broken queue/group never
            aborts recovery for the other queues.
        """
        if not self._initialized or not self._redis:
            return []
        recovered: list[tuple[str, str, QueuedWebhook]] = []
        for queue_name in queue_names:
            try:
                summary = await self._redis.xpending(queue_name, "workers")
            except Exception as exc:
                logger.warning("PEL recovery XPENDING failed for %s: %s", queue_name, exc)
                continue
            try:
                pending = int(summary.get("pending", 0) or 0) if summary else 0
            except (TypeError, ValueError):
                pending = 0
            if pending <= 0:
                continue
            try:
                xautoclaim = await self._redis.xautoclaim(
                    queue_name,
                    "workers",
                    consumer_name,
                    min_idle_ms,
                    start_id="0-0",
                    count=count,
                )
            except Exception as exc:
                # XAUTOCLAIM requires Redis >= 6.2; on older servers this will
                # be a ResponseError.  Contained here: the normal consume path
                # and other queues are unaffected.
                logger.warning("PEL recovery XAUTOCLAIM failed for %s: %s", queue_name, exc)
                continue
            try:
                next_id, entries, deleted_ids = xautoclaim[0], xautoclaim[1], xautoclaim[2]
            except (IndexError, TypeError, ValueError):
                logger.warning("PEL recovery XAUTOCLAIM malformed result for %s", queue_name)
                continue
            seen = 0
            for entry in entries or []:
                try:
                    entry_id = entry[0]
                    fields = entry[1] if len(entry) > 1 else []
                    if not fields:
                        # The message was deleted from the stream after delivery
                        # while still pending; nothing left to process.
                        continue
                    data = dict(zip(fields[::2], fields[1::2]))
                    webhook = QueuedWebhook.from_stream_entry(entry_id, data)
                except Exception as exc:
                    logger.warning(
                        "PEL recovery could not decode entry on %s (skipped, "
                        "count %d reclaimed so far): %s",
                        queue_name, seen, exc,
                    )
                    continue
                recovered.append((queue_name, entry_id, webhook))
                seen += 1
                if seen >= count:
                    break  # hard bound even if the server returned more
            logger.info(
                "PEL recovery: claimed %d/%d pending from %s (consumer=%s, "
                "min_idle=%dms, batch=%d, next_cursor=%s, deleted=%d)",
                seen, pending, queue_name, consumer_name, min_idle_ms,
                count, next_id, len(deleted_ids or []),
            )
        return recovered

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
            # The delivered-but-failed entry has been superseded by the DLQ
            # copy above.  Resolve it now: it must not linger in the pending
            # entries list (PEL) where a Redis XAUTOCLAIM/XCLAIM reclaim would
            # re-process it from attempt=1 — duplicating the effect, losing the
            # retry history, and growing the PEL without bound.
            await self._redis.xack(queue_name, "workers", entry_id)
        else:
            # Requeue with incremented attempt
            webhook.attempt += 1
            webhook.last_error = error
            await self._redis.xadd(queue_name, webhook.to_stream_entry())
            # Resolve the original pending entry immediately after the retry
            # copy is durably written — and BEFORE the best-effort metric call
            # below, so a metric failure can never re-leak the entry into the
            # PEL and cause a duplicate execution on reclaim.
            await self._redis.xack(queue_name, "workers", entry_id)
            try:
                await self._redis.hincrby("webhook:metrics:retried", queue_name, 1)
            except Exception as exc:
                logger.warning(
                    "Best-effort metric update failed for %s: %s", queue_name, exc
                )
    
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
        try:
            await self._redis.hincrby("webhook:metrics:dlq", "total", 1)
        except Exception as exc:
            logger.warning("Best-effort DLQ metric update failed: %s", exc)
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