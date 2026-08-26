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


# Backward-compatible aliases → same canonical global stream handler
router.websocket("/ws/stream")(global_market_stream)
router.websocket("/ws/dashboard")(global_market_stream)


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
