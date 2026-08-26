"""Circuit breaker pattern implementation for external dependencies."""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Awaitable, TypeVar

from app.core.logging import get_logger

logger = get_logger("webhook.circuit_breaker")

T = TypeVar("T")


class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5          # Failures to open circuit
    success_threshold: int = 2          # Successes to close from half-open
    timeout_seconds: float = 30.0       # Time before half-open
    excluded_exceptions: tuple[type[Exception], ...] = ()  # Don't count these


@dataclass
class CircuitBreaker:
    name: str
    config: CircuitBreakerConfig
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    
    async def call(self, func: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        async with self._lock:
            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time >= self.config.timeout_seconds:
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    logger.info("Circuit %s: OPEN -> HALF_OPEN", self.name)
                else:
                    raise CircuitOpenError(f"Circuit {self.name} is OPEN")
        
        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except self.config.excluded_exceptions:
            raise
        except Exception as e:
            await self._on_failure()
            raise
    
    async def _on_success(self) -> None:
        async with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.success_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    logger.info("Circuit %s: HALF_OPEN -> CLOSED", self.name)
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0  # Reset on success
    
    async def _on_failure(self) -> None:
        async with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                logger.warning("Circuit %s: HALF_OPEN -> OPEN (failure in half-open)", self.name)
            elif self.state == CircuitState.CLOSED:
                if self.failure_count >= self.config.failure_threshold:
                    self.state = CircuitState.OPEN
                    logger.warning("Circuit %s: CLOSED -> OPEN (threshold reached)", self.name)


class CircuitOpenError(Exception):
    pass


# Circuit breaker registry
_circuits: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str, config: CircuitBreakerConfig | None = None) -> CircuitBreaker:
    if name not in _circuits:
        _circuits[name] = CircuitBreaker(name, config or CircuitBreakerConfig())
    return _circuits[name]


def init_circuit_breakers() -> None:
    get_circuit_breaker("database", CircuitBreakerConfig(
        failure_threshold=10,
        success_threshold=3,
        timeout_seconds=10.0,
    ))
    get_circuit_breaker("redis", CircuitBreakerConfig(
        failure_threshold=5,
        success_threshold=2,
        timeout_seconds=5.0,
    ))
    get_circuit_breaker("zerodha_api", CircuitBreakerConfig(
        failure_threshold=5,
        success_threshold=2,
        timeout_seconds=30.0,
    ))
    get_circuit_breaker("razorpay_api", CircuitBreakerConfig(
        failure_threshold=5,
        success_threshold=2,
        timeout_seconds=30.0,
    ))
    get_circuit_breaker("websocket_broadcast", CircuitBreakerConfig(
        failure_threshold=20,
        success_threshold=5,
        timeout_seconds=5.0,
        excluded_exceptions=(ConnectionError,),
    ))