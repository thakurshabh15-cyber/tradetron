"""Generic asynchronous WebSocket streaming engine (Phase 15A).

Implements the real-time transport properties every genuine feed needs:

1. authenticated connection where required (``headers`` / ``additional_headers``)
2. subscription lifecycle (providers drive it; this engine exposes ``send_*``)
3. symbol/instrument mapping                                  (provider level)
4. tick normalization                                         (provider level)
5. timestamps                                                 (provider level)
6. stale-data detection                                       (provider level)
7. heartbeat — protocol ping/pong AND data-liveness deadline
8. reconnect with exponential backoff (bounded delay + bounded attempts)
9. subscription restoration after reconnect (``on_reconnect`` hook)
10. malformed-message handling (oversized-frame cap + contained handler errors)
11. provider disconnect handling (reconnect loop, never crashes)
12. tenant/user isolation                                      (provider level; public feeds)
13. bounded resource usage (max message size, no unbounded queues)
14. cancellation/shutdown (``stop()`` cancels tasks and closes cleanly)
15. no duplicate subscriptions                                 (provider level)
16. cache consistency                                          (provider level)
17. observable feed status (``StreamStatus`` + metrics)

The loop is deliberately bounded: after ``max_reconnect_attempts`` failed
connect attempts the stream transitions to a terminal ``UNAVAILABLE`` state
instead of retrying forever.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

import websockets.asyncio.client

from app.core.logging import get_logger

MessageHandler = Callable[[str | bytes], Awaitable[None]]
OpenHandler = Callable[[], Awaitable[None]]

# RFC 6455 application-range close code used when the stream self-heals a dead
# connection (heartbeat timeout / data starvation).
_WS_CODE_SELF_HEAL = 4588
_STREAM_LOGGER = get_logger("market.ws")


class StreamStatus(str, Enum):
    STOPPED = "STOPPED"                # never started / shut down
    CONNECTING = "CONNECTING"          # connection attempt in flight
    OPEN = "OPEN"                      # websocket open and delivering
    RECONNECTING = "RECONNECTING"      # backoff wait between attempts
    UNAVAILABLE = "UNAVAILABLE"        # terminal — reconnect budget exhausted


class WebSocketStream:
    """Vendor-agnostic async WebSocket client with reconnect + heartbeat.

    Providers compose this engine and register ``on_message`` / ``on_open`` /
    ``on_reconnect`` coroutine callbacks.  ``on_reconnect`` is the
    subscription-restoration hook: it runs after every (re)connect so providers
    re-send their full current subscription set.
    """

    def __init__(
        self,
        *,
        name: str,
        url: str,
        on_message: MessageHandler,
        on_open: Optional[OpenHandler] = None,
        on_reconnect: Optional[OpenHandler] = None,
        on_terminal: Optional[OpenHandler] = None,
        headers: Optional[dict[str, str]] = None,
        connect_timeout: float = 12.0,
        ping_interval: float = 20.0,
        ping_timeout: float = 10.0,
        close_timeout: float = 5.0,
        heartbeat_interval: float = 20.0,
        heartbeat_deadline: Optional[float] = 90.0,
        max_message_bytes: int = 1_000_000,
        backoff_base: float = 1.0,
        max_reconnect_delay: float = 30.0,
        max_reconnect_attempts: Optional[int] = 25,
        stable_reset_after: float = 60.0,
        logger: Any = None,
    ) -> None:
        self.name = name
        self.url = url
        self._on_message = on_message
        self._on_open = on_open
        self._on_reconnect = on_reconnect
        self._on_terminal = on_terminal
        self._headers = dict(headers or {})

        self.connect_timeout = connect_timeout
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.close_timeout = close_timeout
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_deadline = heartbeat_deadline
        self.max_message_bytes = max_message_bytes
        self.backoff_base = backoff_base
        self.max_reconnect_delay = max_reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        self.stable_reset_after = stable_reset_after
        self._logger = logger or _STREAM_LOGGER

        self._status = StreamStatus.STOPPED
        self._running = False
        self._ws: Any = None
        self._task: Optional[asyncio.Task] = None
        self._run_id = 0

        # Observability / health state
        self._connected_at: Optional[datetime] = None
        self._last_message_at: Optional[datetime] = None
        self._messages_received = 0
        self._bytes_received = 0
        self._malformed_frames = 0
        self._handler_errors = 0
        self._reconnect_attempts = 0
        self._heartbeat_timeouts = 0
        self._last_error: Optional[str] = None
# ── Status / metrics ────────────────────────────────────────────────

    @property
    def status(self) -> StreamStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status is StreamStatus.OPEN and self._ws is not None

    @property
    def connected_at(self) -> Optional[datetime]:
        return self._connected_at

    @property
    def last_message_at(self) -> Optional[datetime]:
        return self._last_message_at

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def metrics(self) -> dict[str, Any]:
        return {
            "status": self._status.value,
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
            "last_message_at": (
                self._last_message_at.isoformat() if self._last_message_at else None
            ),
            "messages_received": self._messages_received,
            "bytes_received": self._bytes_received,
            "malformed_frames": self._malformed_frames,
            "handler_errors": self._handler_errors,
            "reconnect_attempts": self._reconnect_attempts,
            "heartbeat_timeouts": self._heartbeat_timeouts,
            "last_error": self._last_error,
        }

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> asyncio.Task:
        """Begin the connect/reconnect loop.  Idempotent while running."""
        if self._running:
            return self._task  # type: ignore[return-value]
        self._running = True
        self._run_id += 1
        self._status = StreamStatus.CONNECTING
        self._task = asyncio.create_task(self._main_loop(), name=f"ws:{self.name}")
        return self._task  # type: ignore[return-value]

    async def stop(self) -> None:
        """Graceful shutdown: close the socket, cancel the loop, free tasks."""
        self._running = False
        self._run_id += 1
        ws = self._ws
        self._ws = None
        if ws is not None:
            await self._safe_close(ws)
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - contained
                pass
        self._status = StreamStatus.STOPPED

    # ── Sending (used by provider subscription lifecycles) ──────────────

    async def send_text(self, message: str) -> bool:
        """Send a text frame.  Returns False when the stream is not open."""
        ws = self._ws
        if ws is None or not self.is_connected:
            return False
        try:
            await ws.send(message)
            return True
        except Exception as exc:  # noqa: BLE001 - contained, reconnect loop heals
            self._last_error = f"send failed: {exc}"
            return False

    async def send_bytes(self, payload: bytes) -> bool:
        """Send a binary frame.  Returns False when the stream is not open."""
        ws = self._ws
        if ws is None or not self.is_connected:
            return False
        try:
            await ws.send(payload)
            return True
        except Exception as exc:  # noqa: BLE001 - contained, reconnect loop heals
            self._last_error = f"send failed: {exc}"
            return False

    async def ping(self) -> bool:
        """Send a protocol ping.  Returns True when delivered."""
        ws = self._ws
        if ws is None or not self.is_connected:
            return False
        try:
            await asyncio.wait_for(ws.ping(), timeout=self.ping_timeout)
            return True
        except Exception:  # noqa: BLE001 - contained
            return False
# ── Internals ───────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        attempts = 0
        while self._running:
            self._status = StreamStatus.CONNECTING
            try:
                async with websockets.asyncio.client.connect(
                    self.url,
                    additional_headers=self._headers or None,
                    open_timeout=self.connect_timeout,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    close_timeout=self.close_timeout,
                    max_size=self.max_message_bytes,
                    logger=self._logger,
                ) as ws:
                    # Reset the attempt counter ONLY once the connection has
                    # proven stable; a churning endpoint must keep escalating
                    # its backoff instead of spinning without bound.
                    if (
                        self._connected_at is not None
                        and (self._utcnow() - self._connected_at).total_seconds()
                        >= self.stable_reset_after
                    ):
                        attempts = 0
                    self._ws = ws
                    self._run_id += 1
                    run_id = self._run_id
                    self._connected_at = self._utcnow()
                    self._status = StreamStatus.OPEN
                    self._last_error = None

                    if self._on_reconnect is not None:
                        try:
                            await self._on_reconnect()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - contained
                            self._logger.warning(
                                "[%s] resubscribe hook error: %s", self.name, exc
                            )
                    if self._on_open is not None:
                        try:
                            await self._on_open()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - contained
                            self._logger.warning("[%s] on_open hook error: %s", self.name, exc)

                    reader = asyncio.create_task(self._reader(ws, run_id))
                    heartbeat = asyncio.create_task(self._heartbeat_task(ws, run_id))
                    try:
                        await reader
                    finally:
                        heartbeat.cancel()
                        try:
                            await heartbeat
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect loop owns errors
                self._last_error = str(exc)[:300]
                if not self._running:
                    break
                attempts += 1
                self._reconnect_attempts = attempts
                self._logger.warning(
                    "[%s] stream error: %s (attempt %d)", self.name, exc, attempts
                )
            finally:
                if self._ws is not None:
                    await self._safe_close(self._ws)
                    self._ws = None

            if not self._running:
                self._status = StreamStatus.STOPPED
                break

            if (
                self.max_reconnect_attempts is not None
                and attempts > self.max_reconnect_attempts
            ):
                self._status = StreamStatus.UNAVAILABLE
                self._logger.error(
                    "[%s] reconnect budget exhausted after %d attempts — UNAVAILABLE",
                    self.name,
                    attempts,
                )
                if self._on_terminal is not None:
                    try:
                        await self._on_terminal()
                    except Exception:  # noqa: BLE001 - contained
                        pass
                break

            delay = min(
                self.backoff_base * (2 ** (attempts - 1)), self.max_reconnect_delay
            )
            self._status = StreamStatus.RECONNECTING
            self._logger.info(
                "[%s] reconnecting in %.1fs (attempt %d)", self.name, delay, attempts + 1
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    async def _reader(self, ws: Any, run_id: int) -> None:
        try:
            async for raw in ws:
                if not self._running or run_id != self._run_id:
                    return
                self._messages_received += 1
                self._last_message_at = self._utcnow()
                size = len(raw) if isinstance(raw, (str, bytes)) else 0
                self._bytes_received += size
                if size > self.max_message_bytes:
                    # Bounded-resource guard: oversized frames are dropped.
                    self._malformed_frames += 1
                    self._last_error = f"oversized frame ({size} bytes) dropped"
                    self._logger.warning("[%s] %s", self.name, self._last_error)
                    continue
                try:
                    await self._on_message(raw)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - contained
                    # A provider handler failure must never kill the reader or
                    # the stream — mirror the engine tick-loop contract.
                    self._handler_errors += 1
                    self._logger.exception(
                        "[%s] message handler error (contained): %s", self.name, exc
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            # ConnectionClosed / other transport errors propagate to the main
            # loop, which schedules the bounded reconnect.
            raise

    async def _heartbeat_task(self, ws: Any, run_id: int) -> None:
        try:
            while self._running and run_id == self._run_id and self.status is StreamStatus.OPEN:
                await asyncio.sleep(self.heartbeat_interval)
                if not (self._running and run_id == self._run_id and self.status is StreamStatus.OPEN):
                    return

                # Data-liveness: if the feed has not delivered ANY message
                # within the deadline, self-heal with a reconnect.
                if self.heartbeat_deadline is not None:
                    anchor = self._last_message_at or self._connected_at or self._utcnow()
                    age = (self._utcnow() - anchor).total_seconds()
                    if age > self.heartbeat_deadline:
                        self._heartbeat_timeouts += 1
                        self._last_error = "data heartbeat timeout — force reconnect"
                        self._logger.warning("[%s] %s", self.name, self._last_error)
                        await self._safe_close(
                            ws, code=_WS_CODE_SELF_HEAL, reason="data heartbeat timeout"
                        )
                        return

                # Protocol ping: if the server never pushed a ping, verify the
                # transport is still alive on our side.
                try:
                    await asyncio.wait_for(ws.ping(), timeout=self.ping_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - contained
                    self._heartbeat_timeouts += 1
                    self._last_error = "protocol ping timeout — force reconnect"
                    self._logger.warning("[%s] %s", self.name, self._last_error)
                    await self._safe_close(
                        ws, code=_WS_CODE_SELF_HEAL, reason="protocol ping timeout"
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - contained
            self._heartbeat_timeouts += 1
            self._last_error = f"heartbeat task error: {exc}"

    async def _safe_close(self, ws: Any, code: int = 1000, reason: str = "") -> None:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 - already closed / closing
            pass


__all__ = ["StreamStatus", "WebSocketStream"]