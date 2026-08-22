"""Test WebSocket Endpoints and Event Broadcasting (order_executed, trade_closed)."""

import asyncio
from fastapi.testclient import TestClient
from app.main import app
from app.market_data.manager import ws_manager


def test_websocket_event_broadcasting():
    """Verify that WebSocket broadcast emits structured events without error."""
    client = TestClient(app)

    # 1. Verify WebSocket connection to /ws/events and /ws/trades
    with client.websocket_connect("/ws/events") as websocket:
        # Simulate an order_executed event
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
        }

        # Broadcast via ws_manager
        asyncio.run(ws_manager.broadcast("trades", sample_order_event))
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
        }

        asyncio.run(ws_manager.broadcast("trades", sample_close_event))
        received_2 = websocket.receive_json()
        assert received_2["event"] == "trade_closed"
        assert received_2["pnl"] == 45.00
