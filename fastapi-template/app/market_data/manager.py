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

from fastapi import WebSocket, WebSocketDisconnect

from app.core.logging import get_logger

logger = get_logger("ws.manager")

# Maximum simultaneous PRIVATE (authenticated) WebSocket connections a single
# user may hold on the private feed surface (/ws/trades, /ws/events).
#
# Rationale:
# - WebSocket connections are fundamentally process-local: each ASGI worker
#   owns exactly the sockets it accepted.  An in-process per-user budget
#   therefore bounds each worker's per-user memory directly - no distributed
#   counter is required and no cross-worker protocol must be invented.
# - Public market feeds (/ws/market/stream, /ws/market/{symbol},
#   /ws/optionchain/{symbol}) register NO identity, so they can never consume
#   a user budget - public behavior is unchanged.
# - The cap (8) is deliberately generous versus the 1-2 sockets the frontend
#   opens per tab, so legitimate multi-tab sessions are never interrupted.
#   Exceeding the cap is rejected deterministically with close code 4408
#   (RFC 6455 application range) so clients treat it as terminal instead of
#   entering a reconnect loop.
MAX_PRIVATE_CONNECTIONS_PER_USER = 8
# RFC 6455 application close code - per-user private-connection cap exceeded.
WS_CODE_LIMIT_EXCEEDED = 4408


async def _close_unaccepted(websocket: WebSocket, code: int, reason: str) -> None:
    """Best-effort denial of a not-yet-accepted WebSocket (handshake reject).

    Mirrors ``app.api.websocket._close_ws`` for pre-accept rejections so the
    client observes a deterministic close ``code`` instead of a generic one.
    """
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, WebSocketDisconnect):
        # Client already gone - nothing to clean up.
        pass


class ConnectionManager:
    """Manages WebSocket connections grouped by channel name.

    Supports two connection classes on the same channel:
      - public connections (market feeds): no identity, broadcast to all
      - private connections (trade/event feeds): carry a server-derived
        ``user_id``/``role`` and receive ONLY their own tenant's events via
        ``broadcast_user`` / ``broadcast_admins``.  A client can never supply
        its identity — it is always derived from the verified JWT + DB row.
    """

    def __init__(self) -> None:
        self._channels: dict[str, set[WebSocket]] = defaultdict(set)
        self._ws_info: dict[WebSocket, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        channel: str,
        ws: WebSocket,
        *,
        user_id: str | None = None,
        role: str | None = None,
    ) -> bool:
        """Accept and register a WebSocket on a channel.

        ``user_id``/``role`` are strictly server-derived (JWT + DB lookup).
        When omitted the connection is public and receives channel-wide
        broadcasts only.

        Returns ``True`` when the connection was accepted and registered, or
        ``False`` when the per-user private-connection cap was exceeded and
        the socket was already rejected with close code 4408.
        """
        if user_id is not None:
            async with self._lock:
                active = sum(
                    1
                    for info in self._ws_info.values()
                    if info.get("user_id") == user_id
                )
                limit_hit = active >= MAX_PRIVATE_CONNECTIONS_PER_USER
            if limit_hit:
                await _close_unaccepted(
                    ws,
                    WS_CODE_LIMIT_EXCEEDED,
                    "Per-user connection limit exceeded",
                )
                logger.warning(
                    "WS private connection rejected (user=%s active=%d cap=%d)",
                    user_id,
                    active,
                    MAX_PRIVATE_CONNECTIONS_PER_USER,
                )
                return False

        await ws.accept()
        async with self._lock:
            self._channels[channel].add(ws)
            if user_id is not None:
                self._ws_info[ws] = {"user_id": user_id, "role": role or "trader"}
        logger.debug("WS connected: %s  (total=%d)", channel, len(self._channels[channel]))
        return True

    async def disconnect(self, channel: str, ws: WebSocket) -> None:
        """Remove a WebSocket from a channel."""
        async with self._lock:
            self._channels[channel].discard(ws)
            self._ws_info.pop(ws, None)
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

    async def broadcast_user(
        self, channel: str, user_id: str | None, payload: dict
    ) -> None:
        """Send a JSON message only to the connection(s) owned by ``user_id``.

        Server-derived identity scoping — the only connection(s) touched are
        those whose verified JWT subject equals ``user_id``.  A ``None``
        user_id (system-originated event without an owner) is dropped rather
        than leaked to arbitrary tenants.
        """
        if not user_id:
            return
        message = json.dumps(payload, default=str)
        async with self._lock:
            clients = [
                ws
                for ws in self._channels.get(channel, ())
                if self._ws_info.get(ws, {}).get("user_id") == user_id
            ]

        if not clients:
            return

        results = await asyncio.gather(
            *(ws.send_text(message) for ws in clients),
            return_exceptions=True,
        )

        for client, result in zip(clients, results):
            if isinstance(result, Exception):
                logger.debug("Pruning dead WS on channel %s", channel)
                await self.disconnect(channel, client)

    async def broadcast_admins(self, channel: str, payload: dict) -> None:
        """Send a JSON message only to admin-role connections on a channel.

        Used for admin-only lifecycle/kill-switch events so privileged
        operational messages never leak to ordinary tenants.
        """
        message = json.dumps(payload, default=str)
        async with self._lock:
            clients = [
                ws
                for ws in self._channels.get(channel, ())
                if self._ws_info.get(ws, {}).get("role") == "admin"
            ]

        if not clients:
            return

        results = await asyncio.gather(
            *(ws.send_text(message) for ws in clients),
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
