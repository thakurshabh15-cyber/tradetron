"""WebSocket connection manager for real-time data broadcasting.

Handles two independent channel types:
  - ``market:{symbol}`` — live price ticks per symbol
  - ``trades``          — real-time trade execution feed

Thread-safe via ``asyncio.Lock``; stale connections are pruned automatically.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from fastapi import WebSocket

from app.core.logging import get_logger

logger = get_logger("ws.manager")


class ConnectionManager:
    """Manages WebSocket connections grouped by channel name."""

    def __init__(self) -> None:
        self._channels: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, channel: str, ws: WebSocket) -> None:
        """Accept and register a WebSocket on a channel."""
        await ws.accept()
        async with self._lock:
            self._channels[channel].add(ws)
        logger.debug("WS connected: %s  (total=%d)", channel, len(self._channels[channel]))

    async def disconnect(self, channel: str, ws: WebSocket) -> None:
        """Remove a WebSocket from a channel."""
        async with self._lock:
            self._channels[channel].discard(ws)
            if not self._channels[channel]:
                del self._channels[channel]

    async def broadcast(self, channel: str, payload: dict) -> None:
        """Send a JSON message to all clients on a channel."""
        message = json.dumps(payload, default=str)
        async with self._lock:
            clients = list(self._channels.get(channel, []))

        if not clients:
            return

        results = await asyncio.gather(
            *(c.send_text(message) for c in clients),
            return_exceptions=True,
        )

        for client, result in zip(clients, results):
            if isinstance(result, Exception):
                logger.debug("Pruning dead WS on channel %s", channel)
                await self.disconnect(channel, client)

    @property
    def channel_counts(self) -> dict[str, int]:
        """Return subscriber counts per channel (for monitoring)."""
        return {ch: len(subs) for ch, subs in self._channels.items()}


# Singleton instance used across the application
ws_manager = ConnectionManager()
