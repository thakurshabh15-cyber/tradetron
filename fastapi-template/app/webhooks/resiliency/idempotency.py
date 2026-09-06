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
        self.redis_url = redis_url or settings.effective_redis_url or "redis://localhost:6379/0"
        self._redis: redis.Redis | None = None
        self._ttl = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        """The idempotency retention window in seconds.

        This is also the system's replay-protection horizon: an event whose
        authenticated timestamp precedes this window can never be matched to
        a live idempotency record, so it must be rejected as stale by the
        validation layer to prevent replay after the key expires.
        """
        return self._ttl
    
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
    
    async def is_completed(self, key: str) -> bool:
        """Read-only check: is this idempotency key already COMPLETED?

        This is a non-mutating lookup (plain GET).  It is deliberately NOT
        ``check_and_mark_processing()``, which is mutating — it would reopen a
        completed record's state, re-mark a new key as ``processing``, and
        delete a stale ``processing`` record.  Those mutations are correct at
        the HTTP ingress (which owns the create/lock lifecycle) but must never
        run on the worker's *duplicate-suppression* path, where the invariant
        is: "if already completed, do not execute the handler again and do not
        alter the record."

        FAIL-SAFE: any Redis error returns ``False`` (not completed), never
        ``True``.  For a trading/payment side-effect path, false suppression
        (dropping a real event) is far worse than a rare duplicate, so an
        indeterminate lookup must NOT be treated as completed.
        """
        if not key:
            return False
        try:
            value = await self._redis.get(f"idempotency:{key}")
        except Exception as exc:
            logger.warning(
                "Idempotency is_completed lookup failed for %s; treating as "
                "not-completed (fail-safe): %s",
                key, exc,
            )
            return False
        if not value:
            return False
        try:
            record = json.loads(value)
        except (TypeError, ValueError):
            return False
        return record.get("status") == "completed"

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