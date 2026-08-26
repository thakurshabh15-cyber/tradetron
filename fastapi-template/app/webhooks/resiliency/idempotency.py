"""Distributed idempotency store using Redis."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
import redis.asyncio as redis

from app.config import settings
from app.core.logging import get_logger

logger = get_logger("webhook.idempotency")


@dataclass
class IdempotencyRecord:
    key: str
    result: dict[str, Any] | None
    status: str  # "processing", "completed", "failed"
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None


class IdempotencyStore:
    """Distributed idempotency store using Redis"""
    
    def __init__(self, redis_url: str | None = None, ttl_seconds: int = 86400 * 7):  # 7 days
        self.redis_url = redis_url or settings.redis_url or "redis://localhost:6379/0"
        self._redis: redis.Redis | None = None
        self._ttl = ttl_seconds
    
    async def initialize(self) -> None:
        self._redis = redis.from_url(self.redis_url, decode_responses=True)
    
    async def check_and_mark_processing(self, key: str) -> tuple[bool, Optional[dict]]:
        """
        Check if key exists and mark as processing.
        Returns (is_new, existing_result)
        - is_new=True: First time seeing this key, proceed with processing
        - is_new=False: Key exists, return existing result if completed
        """
        redis_key = f"idempotency:{key}"
        
        # Atomic check-and-set using Lua
        lua_script = """
        local key = KEYS[1]
        local ttl = tonumber(ARGV[1])
        local now = ARGV[2]
        
        local existing = redis.call('GET', key)
        if existing then
            local data = cjson.decode(existing)
            if data.status == 'completed' then
                return {0, existing}  -- Not new, return result
            elseif data.status == 'processing' then
                -- Check for stale processing (older than 5 minutes)
                local age = now - data.created_at
                if age > 300 then
                    -- Stale, allow reprocessing
                    redis.call('DEL', key)
                    return {1, nil}
                end
                return {-1, nil}  -- Currently processing
            else
                -- Failed, allow retry
                return {1, nil}
            end
        end
        
        -- New key, mark as processing
        local record = cjson.encode({
            key = key,
            status = 'processing',
            created_at = now,
            result = cjson.null
        })
        redis.call('SET', key, record, 'EX', ttl)
        return {1, nil}
        """
        
        try:
            script = self._redis.register_script(lua_script)
            result = await script(keys=[redis_key], args=[self._ttl, datetime.now(timezone.utc).timestamp()])
            
            status_code = result[0]
            if status_code == 1:
                return True, None  # New, proceed
            elif status_code == 0:
                existing = json.loads(result[1])
                return False, existing.get("result")  # Completed, return cached result
            else:  # -1
                raise IdempotencyConflictError(f"Key {key} is currently being processed")
        except Exception as e:
            logger.warning("Idempotency check failed, allowing request: %s", e)
            return True, None  # Fail open
    
    async def mark_completed(self, key: str, result: dict[str, Any]) -> None:
        redis_key = f"idempotency:{key}"
        record = {
            "key": key,
            "status": "completed",
            "created_at": datetime.now(timezone.utc).timestamp(),
            "completed_at": datetime.now(timezone.utc).timestamp(),
            "result": result,
        }
        await self._redis.set(redis_key, json.dumps(record), ex=self._ttl)
    
    async def mark_failed(self, key: str, error: str) -> None:
        redis_key = f"idempotency:{key}"
        record = {
            "key": key,
            "status": "failed",
            "created_at": datetime.now(timezone.utc).timestamp(),
            "completed_at": datetime.now(timezone.utc).timestamp(),
            "error": error,
        }
        await self._redis.set(redis_key, json.dumps(record), ex=self._ttl)


class IdempotencyConflictError(Exception):
    pass


# Global idempotency store
idempotency_store = IdempotencyStore()