"""Bulkhead isolation pattern to prevent cascade failures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import TypeVar

T = TypeVar("T")


@dataclass
class BulkheadConfig:
    max_concurrent: int
    max_queue_size: int = 0  # 0 = unlimited
    timeout_seconds: float = 30.0


class Bulkhead:
    """Isolates critical resources to prevent cascade failures"""
    
    def __init__(self, name: str, config: BulkheadConfig):
        self.name = name
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._queue: asyncio.Queue | None = None
        if config.max_queue_size > 0:
            self._queue = asyncio.Queue(maxsize=config.max_queue_size)
        self._active = 0
        self._rejected = 0
    
    @asynccontextmanager
    async def execute(self):
        """Execute operation within bulkhead"""
        acquired = False
        try:
            if self._queue:
                # Try to queue if at capacity
                try:
                    await asyncio.wait_for(self._queue.put(None), timeout=0.1)
                except asyncio.TimeoutError:
                    self._rejected += 1
                    raise BulkheadRejectedError(f"Bulkhead {self.name} queue full")
            
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.config.timeout_seconds)
            acquired = True
            self._active += 1
            yield
        finally:
            if acquired:
                self._semaphore.release()
                self._active -= 1
            if self._queue:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
    
    def stats(self) -> dict:
        return {
            "name": self.name,
            "active": self._active,
            "capacity": self.config.max_concurrent,
            "available": self.config.max_concurrent - self._active,
            "rejected": self._rejected,
            "queue_size": self._queue.qsize() if self._queue else 0,
        }


class BulkheadRejectedError(Exception):
    pass


# Pre-configured bulkheads
_bulkheads: dict[str, Bulkhead] = {}


def get_bulkhead(name: str, config: BulkheadConfig | None = None) -> Bulkhead:
    if name not in _bulkheads:
        _bulkheads[name] = Bulkhead(name, config or BulkheadConfig(max_concurrent=100))
    return _bulkheads[name]


def init_bulkheads() -> None:
    get_bulkhead("database", BulkheadConfig(max_concurrent=50, max_queue_size=100, timeout_seconds=10.0))
    get_bulkhead("redis", BulkheadConfig(max_concurrent=100, max_queue_size=200, timeout_seconds=5.0))
    get_bulkhead("zerodha_api", BulkheadConfig(max_concurrent=20, max_queue_size=50, timeout_seconds=30.0))
    get_bulkhead("razorpay_api", BulkheadConfig(max_concurrent=10, max_queue_size=20, timeout_seconds=30.0))
    get_bulkhead("websocket", BulkheadConfig(max_concurrent=200, max_queue_size=500, timeout_seconds=5.0))
    get_bulkhead("webhook_processing", BulkheadConfig(max_concurrent=100, max_queue_size=1000, timeout_seconds=60.0))