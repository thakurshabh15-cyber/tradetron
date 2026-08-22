"""WebSocket endpoints for real-time data streaming."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.market_data.manager import ws_manager

logger = get_logger("api.websocket")

router = APIRouter(tags=["websocket"])


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
    """Live trade execution feed.

    Every trade executed by the engine is broadcast here in real-time.
    """
    channel = "trades"
    await ws_manager.connect(channel, websocket)
    logger.debug("WS trade feed opened")

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(channel, websocket)
        logger.debug("WS trade feed closed")


@router.websocket("/ws/events")
async def events_feed(websocket: WebSocket):
    """Lifecycle event feed emitting order_executed, trade_closed, and engine state updates."""
    channel = "trades"  # Emits all execution and closure lifecycle events
    await ws_manager.connect(channel, websocket)
    logger.debug("WS events feed opened")

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(channel, websocket)
        logger.debug("WS events feed closed")
