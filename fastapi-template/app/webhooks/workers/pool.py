"""Worker pool management for webhook processing."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from typing import Callable, Awaitable
from collections import defaultdict
from contextlib import asynccontextmanager

from app.webhooks.queue.redis_streams import webhook_queue, QueuedWebhook
from app.webhooks.routing.router import resolve_route, WebhookCategory
from app.core.logging import get_logger
from app.core.monitoring import monitoring_sentinel

logger = get_logger("webhook.workers")


@dataclass
class WorkerConfig:
    pool_name: str
    queue_names: list[str]
    concurrency: int
    handler: Callable[[QueuedWebhook], Awaitable[None]]


class WorkerPool:
    """Manages multiple worker pools for different webhook categories"""
    
    def __init__(self):
        self._pools: dict[str, WorkerConfig] = {}
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._semaphores: dict[str, asyncio.Semaphore] = {}
    
    def register_pool(self, config: WorkerConfig) -> None:
        self._pools[config.pool_name] = config
        self._semaphores[config.pool_name] = asyncio.Semaphore(config.concurrency)
        logger.info("Registered worker pool: %s (concurrency=%d, queues=%s)", 
                    config.pool_name, config.concurrency, config.queue_names)
    
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        
        for pool_name, config in self._pools.items():
            for i in range(config.concurrency):
                task = asyncio.create_task(self._worker_loop(pool_name, config, i))
                self._tasks.append(task)
        
        logger.info("Started %d worker pools with %d total workers", 
                    len(self._pools), len(self._tasks))
    
    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("All worker pools stopped")
    
    async def _worker_loop(self, pool_name: str, config: WorkerConfig, worker_id: int) -> None:
        semaphore = self._semaphores[pool_name]
        logger.debug("Worker %s-%d started", pool_name, worker_id)
        
        while self._running:
            try:
                async with semaphore:
                    events = await webhook_queue.dequeue(
                        worker_pool=f"{pool_name}-{worker_id}",
                        queue_names=config.queue_names,
                        count=1,
                        block_ms=5000,
                    )
                    
                    for entry_id, webhook in events:
                        await self._process_webhook(pool_name, config, entry_id, webhook)
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Worker %s-%d error: %s", pool_name, worker_id, e)
                await asyncio.sleep(1)  # Back off on error
        
        logger.debug("Worker %s-%d stopped", pool_name, worker_id)
    
    async def _process_webhook(
        self, 
        pool_name: str, 
        config: WorkerConfig, 
        entry_id: str, 
        webhook: QueuedWebhook
    ) -> None:
        start_time = asyncio.get_event_loop().time()
        
        try:
            # Execute handler with timeout
            route = resolve_route(webhook.envelope.provider, webhook.envelope.event_type)
            await asyncio.wait_for(
                config.handler(webhook),
                timeout=route.timeout_seconds
            )
            
            # Success
            await webhook_queue.ack(route.queue_name, entry_id)
            duration_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            logger.debug("Processed webhook %s in %.2fms", webhook.envelope.event_id, duration_ms)
            
        except asyncio.TimeoutError:
            error = f"Handler timeout after {route.timeout_seconds}s"
            logger.error("Webhook %s timeout: %s", webhook.envelope.event_id, error)
            await webhook_queue.nack(route.queue_name, entry_id, webhook, error)
            
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            logger.error("Webhook %s processing failed: %s", webhook.envelope.event_id, error)
            await webhook_queue.nack(route.queue_name, entry_id, webhook, error)
    
    def health_check(self) -> bool:
        return self._running and len(self._tasks) > 0


# Global worker pool
worker_pool = WorkerPool()