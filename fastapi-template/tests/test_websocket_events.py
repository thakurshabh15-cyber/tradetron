"""Test WebSocket Endpoints and Event Broadcasting (order_executed, trade_closed).

Since Phase 3-D Iteration 4, ``/ws/trades`` and ``/ws/events`` are PRIVATE
authenticated streams: connections require ``?token=<access JWT>`` and events
are scoped to the authenticated tenant.
"""

import asyncio
import uuid

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.security import create_access_token
from app.db.session import SessionLocal
from app.main import app
from app.market_data.manager import ws_manager
from app.models.user import UserRecord


def create_test_user() -> str:
    """Create an active user in the test DB and return its id."""
    uid = str(uuid.uuid4())

    async def _seed():
        async with SessionLocal() as session:
            session.add(
                UserRecord(
                    id=uid,
                    email=f"{uid[:8]}@ws-events-test.io",
                    hashed_password="x",
                    is_active=True,
                    is_verified=True,
                )
            )
            await session.commit()

    asyncio.run(_seed())
    return uid


def access_token_for(user_id: str) -> str:
    return create_access_token({"sub": user_id, "email": "u@ws-events-test.io", "role": "trader"})


def test_websocket_event_broadcasting():
    """Verify that tenant-scoped WebSocket broadcast emits structured events."""
    client = TestClient(app)
    user_id = create_test_user()
    token = access_token_for(user_id)

    # 1. Verify WebSocket connection to /ws/events and /ws/trades via auth
    with client.websocket_connect(f"/ws/events?token={token}") as websocket:
        # Simulate an order_executed event scoped to this tenant
        sample_order_event = {
            "event": "order_executed",
            "id": "trade-exec-101",
            "order_id": "ord-001",
            "strategy_name": "SMA 50/200 Cross",
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": 10,
            "price": 226.50,
            "pnl": None,
            "user_id": user_id,
        }

        # Broadcast via ws_manager to this tenant only
        asyncio.run(ws_manager.broadcast_user("trades", user_id, sample_order_event))
        received_1 = websocket.receive_json()
        assert received_1["event"] == "order_executed"
        assert received_1["symbol"] == "AAPL"
        assert received_1["side"] == "BUY"

        # Simulate a trade_closed event
        sample_close_event = {
            "event": "trade_closed",
            "id": "trade-exec-102",
            "order_id": "ord-002",
            "strategy_name": "SMA 50/200 Cross",
            "symbol": "AAPL",
            "side": "SELL",
            "quantity": 10,
            "price": 231.00,
            "pnl": 45.00,
            "user_id": user_id,
        }

        asyncio.run(ws_manager.broadcast_user("trades", user_id, sample_close_event))
        received_2 = websocket.receive_json()
        assert received_2["event"] == "trade_closed"
        assert received_2["pnl"] == 45.00

    # 2. Anonymous connections are rejected with close code 4001
    try:
        with client.websocket_connect("/ws/events"):
            raise AssertionError("anonymous /ws/events connection was accepted")
    except WebSocketDisconnect as exc:
        assert exc.code == 4001

    try:
        with client.websocket_connect("/ws/trades"):
            raise AssertionError("anonymous /ws/trades connection was accepted")
    except WebSocketDisconnect as exc:
        assert exc.code == 4001
