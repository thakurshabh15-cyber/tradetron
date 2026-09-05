"""WebSocket endpoints for real-time data streaming."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.market_data.manager import ws_manager
import asyncio
import json
from app.market_data.unified_manager import unified_market_manager

logger = get_logger("api.websocket")

router = APIRouter(tags=["websocket"])

# NOTE: Literal paths MUST be registered before the parameterised
# "/ws/market/{symbol}" route — otherwise Starlette matches the dynamic
# route first and captures "stream" as a symbol, silently starving the
# global ticker-tape feed used by the dashboard.

# WebSocket close codes (RFC 6455 application range):
#   4001 — missing/invalid authentication
#   4003 — token rejected (malformed/expired/wrong type/inactive user)
#   4408 — per-user private-connection cap exceeded (enforced in
#          ConnectionManager.connect; socket rejected before accept)
WS_CODE_AUTH_REQUIRED = 4001
WS_CODE_AUTH_REJECTED = 4003


async def authenticate_ws(websocket: WebSocket) -> dict | None:
    """Authenticate a private WebSocket using ``?token=<access JWT>``.

    Identity is strictly server-derived: the JWT is verified with the shared
    secret, must be an ``access`` token, and the ``sub`` claim must resolve to
    an existing ACTIVE user row.  Any client-supplied ``user_id`` parameter is
    never consulted.

    Returns ``{"id": ..., "role": ...}`` for the authenticated user, or
    ``None`` after the socket has been closed with an appropriate close code.
    The access token itself is never logged.
    """
    token = websocket.query_params.get("token")
    if not token:
        await _close_ws(websocket, WS_CODE_AUTH_REQUIRED, "Authentication required")
        return None

    from app.core.security import decode_token

    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        await _close_ws(websocket, WS_CODE_AUTH_REJECTED, "Invalid or expired access token")
        return None

    user_id = payload.get("sub")
    if not user_id:
        await _close_ws(websocket, WS_CODE_AUTH_REJECTED, "Invalid token subject")
        return None

    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.user import UserRecord

    async with SessionLocal() as session:
        res = await session.execute(select(UserRecord).where(UserRecord.id == user_id))
        user = res.scalar_one_or_none()

    if not user or not user.is_active:
        await _close_ws(websocket, WS_CODE_AUTH_REJECTED, "User inactive or not found")
        return None

    return {"id": user.id, "role": user.role}


async def _close_ws(websocket: WebSocket, code: int, reason: str) -> None:
    """Best-effort close of an unaccepted WebSocket (handshake rejection)."""
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, WebSocketDisconnect):
        # Client already gone — nothing to do.
        pass


@router.websocket("/ws/market/stream")
async def global_market_stream(websocket: WebSocket):
    """Global multiplexed ticker & event stream powering all live terminal widgets."""
    channel = "market:stream"
    await ws_manager.connect(channel, websocket)
    logger.debug("WS global market stream opened")

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(channel, websocket)
        logger.debug("WS global market stream closed")


@router.websocket("/ws/market/{symbol}")
async def market_feed(websocket: WebSocket, symbol: str):
    """Live price tick stream for a single symbol.

    The client connects and receives JSON tick messages until disconnect.
    """
    channel = f"market:{symbol.upper()}"
    await ws_manager.connect(channel, websocket)
    logger.debug("WS market feed opened: %s", symbol.upper())

    try:
        while True:
            # Keep connection alive — client can send pings
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(channel, websocket)
        logger.debug("WS market feed closed: %s", symbol.upper())


@router.websocket("/ws/trades")
async def trade_feed(websocket: WebSocket):
    """Live private trade execution feed.

    Authentication is mandatory (``?token=<access JWT>``).  A client receives
    ONLY the execution events owned by their own tenant — see
    ``ConnectionManager.broadcast_user``.  Anonymous connections are rejected
    with close code 4001; malformed/expired/inactive tokens with 4003.
    """
    user = await authenticate_ws(websocket)
    if not user:
        return

    channel = "trades"
    if not await ws_manager.connect(
        channel, websocket, user_id=user["id"], role=user["role"]
    ):
        # Per-user connection cap exceeded — socket already rejected (4408).
        return
    logger.debug("WS trade feed opened (user=%s)", user["id"])

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(channel, websocket)
        logger.debug("WS trade feed closed (user=%s)", user["id"])


# Backward-compatible aliases → same canonical global stream handler
router.websocket("/ws/stream")(global_market_stream)
router.websocket("/ws/dashboard")(global_market_stream)
# Data-engine / ticker aliases — any frontend hitting these paths gets the
# same multiplexed live tick stream instead of a silent 404 close.
router.websocket("/ws/ticks")(global_market_stream)
router.websocket("/ws/market-data")(global_market_stream)


@router.websocket("/ws/events")
async def events_feed(websocket: WebSocket):
    """Private lifecycle event feed (order_executed, trade_closed, engine state).

    Requires the same ``?token=<access JWT>`` as ``/ws/trades`` and is scoped
    to the authenticated tenant — no cross-tenant lifecycle data is emitted.
    """
    user = await authenticate_ws(websocket)
    if not user:
        return

    channel = "trades"  # Emits execution and closure lifecycle events, tenant-scoped
    if not await ws_manager.connect(
        channel, websocket, user_id=user["id"], role=user["role"]
    ):
        # Per-user connection cap exceeded — socket already rejected (4408).
        return
    logger.debug("WS events feed opened (user=%s)", user["id"])

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(channel, websocket)
        logger.debug("WS events feed closed (user=%s)", user["id"])


@router.websocket("/ws/optionchain/{symbol}")
async def option_chain_stream(websocket: WebSocket, symbol: str):
    """Live option-chain stream — rebuilds the full CE/PE ladder off the real-time
    spot tape every second and pushes it while any client stays connected."""
    from app.market_data.option_chain import build_option_chain

    await websocket.accept()
    clean = symbol.upper().strip()
    expiry = websocket.query_params.get("expiry")
    logger.info("WS option-chain stream opened: %s (expiry=%s)", clean, expiry or "nearest")

    try:
        while True:
            quote = unified_market_manager.get_quote(clean)
            spot = None
            if isinstance(quote, dict):
                for k in ("price", "last_price", "ltp", "close"):
                    try:
                        v = float(quote.get(k))
                        if v > 0:
                            spot = v
                            break
                    except (TypeError, ValueError):
                        continue
            if spot:
                try:
                    chain = build_option_chain(clean, spot, expiry=expiry)
                    await websocket.send_text(json.dumps(chain, default=str))
                except Exception as exc:
                    logger.warning("option-chain rebuild failed for %s: %s", clean, exc)
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        logger.info("WS option-chain stream closed: %s", clean)
    except Exception as exc:
        logger.warning("WS option-chain error %s: %s", clean, exc)
        try:
            await websocket.close()
        except Exception:
            pass
