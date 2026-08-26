"""Distributed token bucket rate limiter using Redis."""

from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass
from typing import Any
import redis.asyncio as redis

from app.config import settings
from app.core.logging import get_logger

logger = get_logger("webhook.rate_limiter")


@dataclass
class RateLimitConfig:
    requests_per_second: int
    burst: int
    key_prefix: str = "ratelimit"


class TokenBucketRateLimiter:
    """Distributed token bucket rate limiter using Redis"""
    
    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or settings.redis_url or "redis://localhost:6379/0"
        self._redis: redis.Redis | None = None
        self._configs: dict[str, RateLimitConfig] = {}
        self._local_buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)
    
    async def initialize(self) -> None:
        self._redis = redis.from_url(self.redis_url, decode_responses=True)
        # Default configs
        self._configs = {
            "global": RateLimitConfig(requests_per_second=10000, burst=20000),
            "provider:zerodha": RateLimitConfig(requests_per_second=5000, burst=10000),
            "provider:upstox": RateLimitConfig(requests_per_second=3000, burst=6000),
            "provider:razorpay": RateLimitConfig(requests_per_second=1000, burst=2000),
            "ip:default": RateLimitConfig(requests_per_second=100, burst=200),
        }
    
    def configure(self, key: str, config: RateLimitConfig) -> None:
        self._configs[key] = config
    
    async def check_limit(self, key: str, cost: int = 1) -> tuple[bool, dict[str, Any]]:
        """
        Check if request is allowed.
        Returns (allowed, metadata)
        """
        config = self._configs.get(key, self._configs["ip:default"])
        
        # Try distributed first, fallback to local
        if self._redis:
            return await self._check_distributed(key, config, cost)
        else:
            return self._check_local(key, config, cost)
    
    async def _check_distributed(self, key: str, config: RateLimitConfig, cost: int) -> tuple[bool, dict]:
        redis_key = f"{config.key_prefix}:{key}"
        now = time.time()
        
        # Lua script for atomic token bucket
        lua_script = """
        local key = KEYS[1]
        local capacity = tonumber(ARGV[1])
        local refill_rate = tonumber(ARGV[2])
        local cost = tonumber(ARGV[3])
        local now = tonumber(ARGV[4])
        
        local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
        local tokens = tonumber(bucket[1])
        local last_refill = tonumber(bucket[2])
        
        if tokens == nil then
            tokens = capacity
            last_refill = now
        end
        
        -- Refill tokens
        local elapsed = now - last_refill
        local new_tokens = math.min(capacity, tokens + elapsed * refill_rate)
        
        local allowed = false
        local remaining = new_tokens
        if new_tokens >= cost then
            allowed = true
            new_tokens = new_tokens - cost
            remaining = new_tokens
        end
        
        redis.call('HMSET', key, 'tokens', new_tokens, 'last_refill', now)
        redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 10)
        
        return {allowed and 1 or 0, remaining, capacity}
        """
        
        try:
            script = self._redis.register_script(lua_script)
            result = await script(
                keys=[redis_key],
                args=[config.burst, config.requests_per_second, cost, now]
            )
            allowed = bool(result[0])
            remaining = float(result[1])
            capacity = float(result[2])
            
            return allowed, {
                "limit": capacity,
                "remaining": max(0, remaining),
                "reset_after": (cost - remaining) / config.requests_per_second if not allowed else 0,
            }
        except Exception as e:
            logger.warning("Distributed rate limit check failed, using local: %s", e)
            return self._check_local(key, config, cost)
    
    def _check_local(self, key: str, config: RateLimitConfig, cost: int) -> tuple[bool, dict]:
        now = time.time()
        tokens, last_refill = self._local_buckets.get(key, (config.burst, now))
        
        elapsed = now - last_refill
        tokens = min(config.burst, tokens + elapsed * config.requests_per_second)
        
        allowed = tokens >= cost
        if allowed:
            tokens -= cost
        
        self._local_buckets[key] = (tokens, now)
        
        return allowed, {
            "limit": config.burst,
            "remaining": max(0, tokens),
            "reset_after": (cost - tokens) / config.requests_per_second if not allowed else 0,
        }


# Global rate limiter
rate_limiter = TokenBucketRateLimiter()