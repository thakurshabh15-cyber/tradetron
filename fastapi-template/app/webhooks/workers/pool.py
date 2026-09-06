"""Worker pool management for webhook processing."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from typing import Callable, Awaitable
from collections import defaultdict
from contextlib import asynccontextmanager

from app.webhooks.queue.redis_streams import webhook_queue, QueuedWebhook
from app.webhooks.routing.router import resolve_route, WebhookCategory, ROUTE_TABLE
from app.config import settings
from app.webhooks.observability.metrics import record_webhook_received
from app.core.logging import get_logger
from app.core.monitoring import monitoring_sentinel

logger = get_logger("webhook.workers")


# ── PEL Recovery (P1) ────────────────────────────────────────────────
# Background mechanism to reclaim entries stranded in the Redis Streams
# Pending-Entries List after a worker crash or transient XACK failure.
#─────────────────────────────────────────────────────────────────────────
RECOVERY_BATCH_SIZE: int = 25
"""Maximum number of entries claimed per queue per recovery pass."""

RECOVERY_INTERVAL_SECONDS: float = 30.0
"""Seconds between consecutive recovery passes.  Kept low enough that
stranded entries are reclaimed within ~30 s + min_idle, but high enough
that two passes cannot overlap on a single event loop."""

_RECOVERY_MIN_IDLE_FLOOR: int = 120
"""Absolute floor in seconds; applies even if all route timeouts are zero."""


def _recovery_min_idle_seconds() -> int:
    """Derive the XAUTOCLAIM min-idle threshold from the current route table.

    The invariant is:  ``min_idle > max handler timeout across all routes``.

    Rationale:
      * The LARGEST ``RouteConfig.timeout_seconds`` in ``ROUTE_TABLE`` is
        currently 60.0 s (``tradethrone_normal`` and the ``(*,*)`` default
        fallback).  The ``asyncio.wait_for`` in ``_process_webhook`` enforces
        this ceiling: no handler can legitimately run longer than the route
        timeout.
      * ``2 * max_timeout`` = 120 s provides a full timeout-sized safety
        margin: any healthy slow handler (which is in-flight on its original
        consumer) finishes well before a recovery pass can reclaim it.
      * A floor of ``_RECOVERY_MIN_IDLE_FLOOR`` ensures the threshold stays
        safe even if route timeouts are reduced in the future.
    """
    max_timeout = max(
        (r.timeout_seconds for r in ROUTE_TABLE.values()),
        default=float(_RECOVERY_MIN_IDLE_FLOOR),
    )
    return max(int(max_timeout * 2), _RECOVERY_MIN_IDLE_FLOOR)


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
        self._recovery_task: asyncio.Task | None = None
    
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
        
        # P1: Start one background PEL-recovery coroutine for the entire pool
        # instance.  Within a single process the `if _running` guard above
        # prevents a second call from spawning a duplicate task; across
        # processes each XAUTOCLAIM is atomic and min-idle resets the claimed
        # entry, so concurrent recovery instances are safe by design.
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._recovery_loop())
        
        logger.info("Started %d worker pools with %d total workers",
                    len(self._pools), len(self._tasks))
    
    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._recovery_task is not None:
            self._recovery_task.cancel()
        all_tasks = self._tasks + (
            [self._recovery_task] if self._recovery_task is not None else []
        )
        await asyncio.gather(*all_tasks, return_exceptions=True)
        self._tasks.clear()
        self._recovery_task = None
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

                    if not events:
                        # No messages this round.  With a blocking Redis read
                        # this is rare (only after the block window elapses);
                        # with a non-blocking or mocked delegate the loop would
                        # otherwise spin hot and starve the event loop, so give
                        # other tasks a fair turn.
                        await asyncio.sleep(0)

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

        route = resolve_route(webhook.envelope.provider, webhook.envelope.event_type)

        # --- Worker-side duplicate-suppression guard (P0-2b) ------------------
        # Before executing any side-effecting handler, check whether this
        # webhook's idempotency key is ALREADY completed.  This protects against
        # a duplicate execution when an entry is re-delivered from the PEL by a
        # future XAUTOCLAIM/XCLAIM/recovery reclaim, or by any future re-read
        # of newly-arrived entries after the same idempotency key was already
        # processed to completion.
        #
        # Read-only, fail-safe semantics (see IdempotencyStore.is_completed):
        #   * Already completed  -> do NOT run the handler; XACK the current
        #     entry (so it leaves the PEL) and return.  Never nack/requeue, and
        #     never alter the completed record.
        #   * Not completed      -> run the handler normally.
        #   * Redis lookup error -> treat as NOT completed and run the handler
        #     normally: for a trading/payment side-effect path, dropping a real
        #     event on an indeterminate lookup is worse than a rare duplicate.
        #     We must never falsely suppress a not-yet-completed event.
        #   * Missing/empty key  -> skip the check entirely (legacy entries
        #     without an idempotency key preserve the pre-existing behaviour).
        idempotency_key = webhook.envelope.idempotency_key
        if idempotency_key:
            try:
                from app.webhooks.resiliency.idempotency import idempotency_store
                already_completed = await idempotency_store.is_completed(idempotency_key)
            except Exception as exc:
                # is_completed already fails safe internally, but a failure to
                # even import/resolve must also never suppress an event.
                logger.warning(
                    "Worker duplicate-guard lookup failed for %s; proceeding "
                    "with handler execution (fail-safe): %s",
                    webhook.envelope.event_id, exc,
                )
                already_completed = False

            if already_completed:
                logger.info(
                    "Duplicate webhook suppressed at worker: event_id=%s "
                    "idempotency_key=%s already completed; XACKing entry %s",
                    webhook.envelope.event_id, idempotency_key, entry_id,
                )
                record_webhook_received(
                    webhook.envelope.provider,
                    webhook.envelope.event_type,
                    "duplicate",
                )
                # XACK the current stream entry so it leaves the PEL.  This is
                # best-effort; a failure is logged and left for the normal
                # at-least-once reconcile, mirroring the success-path ACK.
                try:
                    await webhook_queue.ack(route.queue_name, entry_id)
                except Exception as exc:
                    logger.error(
                        "Duplicate-suppressed webhook %s XACK failed (%s); "
                        "entry %s left in PEL for reconciliation",
                        webhook.envelope.event_id, exc, entry_id,
                    )
                return

        try:
            # Execute handler with timeout
            await asyncio.wait_for(
                config.handler(webhook),
                timeout=route.timeout_seconds
            )
        except asyncio.TimeoutError:
            error = f"Handler timeout after {route.timeout_seconds}s"
            logger.error("Webhook %s timeout: %s", webhook.envelope.event_id, error)
            await webhook_queue.nack(route.queue_name, entry_id, webhook, error)
            return

        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            logger.error("Webhook %s processing failed: %s", webhook.envelope.event_id, error)
            await webhook_queue.nack(route.queue_name, entry_id, webhook, error)
            return

        # Success — the handler completed and its side effects are committed.
        # Transition the idempotency record from "processing" to "completed"
        # BEFORE the XACK.  The Lua stale-processing branch in
        # check_and_mark_processing() DELETES any record left in "processing"
        # for more than 5 minutes, after which a re-delivered webhook is
        # treated as brand-new and the broker/payment side effect would be
        # executed a SECOND time.  Completing the record extends that real
        # dedup window from 5 minutes to the full idempotency TTL.  This is
        # best-effort: a mark_completed failure must never block the XACK,
        # and it must never fall into the nack path (which would requeue an
        # already-executed webhook and duplicate its side effects).
        idempotency_key = webhook.envelope.idempotency_key
        if idempotency_key:
            try:
                from app.webhooks.resiliency.idempotency import idempotency_store
                await idempotency_store.mark_completed(
                    idempotency_key,
                    {"status": "processed", "event_id": webhook.envelope.event_id},
                )
            except Exception as exc:
                logger.warning(
                    "Best-effort idempotency mark_completed failed for %s: %s",
                    webhook.envelope.event_id, exc,
                )

        # Do NOT let an ack failure below fall into a nack path: nack requeues
        # the event, and requeueing an already-executed webhook would duplicate
        # its broker/payment side effects.  If ack fails, log it loudly and let
        # the standard at-least-once PEL redelivery reconcile (or operator
        # intervention) handle it — never re-run the handler proactively.
        try:
            await webhook_queue.ack(route.queue_name, entry_id)
        except Exception as e:
            logger.error(
                "Webhook %s processed successfully but ACK failed (%s); "
                "entry %s left in PEL for reconciliation",
                webhook.envelope.event_id, e, entry_id,
            )
        duration_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        logger.debug("Processed webhook %s in %.2fms", webhook.envelope.event_id, duration_ms)
    
    # ── P1: Background PEL recovery ──────────────────────────────────────
    async def _recovery_loop(
        self,
        interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
        max_iterations: int | None = None,
    ) -> None:
        """Bounded background loop that reclaims PEL entries.

        One task per ``WorkerPool`` instance, started by ``start()`` and
        cancelled by ``stop()``.  The loop sleeps ``interval_seconds``
        between passes; ``max_iterations`` is exposed purely for testing
        (``None`` = unlimited in production).

        Errors inside ``_recover_once`` are caught and logged: they never
        crash the recovery loop or the worker pool.
        """
        if settings.webhook_local_mode:
            logger.info("PEL recovery disabled in webhook_local_mode")
            return

        iteration = 0
        # Lifetime is governed by ``max_iterations`` when provided (testing)
        # or by task cancellation via ``stop()`` when unlimited (production):
        # cancellation interrupts the sleep/await below and unwinds the loop.
        while max_iterations is None or iteration < max_iterations:
            try:
                await self._recover_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "PEL recovery cycle %d failed (will retry next cycle): %s",
                    iteration + 1, exc,
                )
            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                raise
        logger.debug("PEL recovery loop stopped after %d iteration(s)", iteration)

    async def _recover_once(self) -> None:
        """Run ONE bounded recovery pass across every registered pool.

        For each pool:
          1. ``recover_pending`` probes the PEL via XPENDING, then claims up
             to ``RECOVERY_BATCH_SIZE`` entries idle ≥ min-idle via
             XAUTOCLAIM.
          2. Every claimed entry is dispatched through ``_process_webhook`` —
             the SAME method called by normal workers — so the
             ``is_completed()`` duplicate-suppression guard, normal handler
             execution, ``mark_completed``→XACK ordering, and nack/retry/DLQ
             all apply unchanged.
        """
        if settings.webhook_local_mode:
            return
        min_idle_ms = _recovery_min_idle_seconds() * 1000
        for pool_name, config in self._pools.items():
            try:
                recovered = await webhook_queue.recover_pending(
                    config.queue_names,
                    min_idle_ms=min_idle_ms,
                    count=RECOVERY_BATCH_SIZE,
                    consumer_name=f"{pool_name}-recovery",
                )
            except Exception as exc:
                logger.error(
                    "PEL recovery queue probe failed for pool %s: %s",
                    pool_name, exc,
                )
                continue
            if not recovered:
                continue
            logger.info(
                "PEL recovery: %d entry(-ies) claimed for pool %s "
                "(min_idle=%dms, batch=%d)",
                len(recovered), pool_name, min_idle_ms, RECOVERY_BATCH_SIZE,
            )
            for queue_name, entry_id, webhook in recovered:
                try:
                    await self._process_webhook(
                        pool_name, config, entry_id, webhook,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "PEL recovery dispatch failed for pool %s queue %s "
                        "entry %s: %s",
                        pool_name, queue_name, entry_id, exc,
                    )

    def health_check(self) -> bool:
        return self._running and len(self._tasks) > 0


# Global worker pool
worker_pool = WorkerPool()