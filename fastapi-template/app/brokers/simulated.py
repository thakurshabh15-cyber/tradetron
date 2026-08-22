"""Simulated (paper-trading) broker.

Instantly fills every order at the current simulated market price.  Tracks
positions in-memory so the engine and risk manager can query them.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from app.brokers.base import BrokerClient
from app.core.logging import get_logger
from app.schemas.trading import OrderRequest, Side

logger = get_logger("broker.simulated")


class SimulatedBroker(BrokerClient):
    """Paper-trading broker — all orders fill instantly at last known price."""

    def __init__(self) -> None:
        self._positions: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"quantity": 0, "avg_price": 0.0}
        )
        self._last_prices: dict[str, float] = {}
        self._connected = False

    async def connect(self) -> None:
        self._connected = True
        logger.info("Simulated broker connected")

    def update_price(self, symbol: str, price: float) -> None:
        """Called by the market data simulator to keep prices current."""
        self._last_prices[symbol] = price

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        """Simulate instant fill at the last known price."""
        price = self._last_prices.get(order.symbol, 100.0)
        broker_order_id = f"SIM-{uuid.uuid4().hex[:12].upper()}"

        # Update position tracking
        pos = self._positions[order.symbol]
        if order.side == Side.BUY:
            total_cost = pos["avg_price"] * pos["quantity"] + price * order.quantity
            pos["quantity"] += order.quantity
            pos["avg_price"] = total_cost / pos["quantity"] if pos["quantity"] else 0
        else:
            pos["quantity"] -= order.quantity
            if pos["quantity"] <= 0:
                pos["quantity"] = 0
                pos["avg_price"] = 0.0

        logger.info(
            "SIM ORDER FILLED: %s %s %d @ %.2f  [%s]",
            order.side.value,
            order.symbol,
            order.quantity,
            price,
            broker_order_id,
        )

        return {
            "broker_order_id": broker_order_id,
            "status": "FILLED",
            "filled_price": price,
            "filled_quantity": order.quantity,
        }

    async def modify_order(
        self, broker_order_id: str, quantity: int | None = None, price: float | None = None
    ) -> dict[str, Any]:
        logger.info("SIM ORDER MODIFIED: %s -> qty=%s price=%s", broker_order_id, quantity, price)
        return {"status": "MODIFIED", "broker_order_id": broker_order_id, "quantity": quantity, "price": price}

    async def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        logger.info("SIM ORDER CANCEL: %s", broker_order_id)
        return {"status": "CANCELLED", "broker_order_id": broker_order_id}

    async def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        return {"status": "FILLED", "broker_order_id": broker_order_id}

    async def get_positions(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": symbol,
                "quantity": data["quantity"],
                "avg_price": data["avg_price"],
            }
            for symbol, data in self._positions.items()
            if data["quantity"] != 0
        ]

    async def get_margins(self) -> dict[str, Any]:
        """Return simulated virtual paper capital margin."""
        return {
            "available_cash": 1000000.0,  # ₹10,00,000 / $10,000 virtual balance
            "utilized_margin": 15000.0,
            "total_collateral": 1000000.0,
            "currency": "INR",
        }

    async def get_holdings(self) -> list[dict[str, Any]]:
        """Return simulated virtual paper holdings."""
        return [
            {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "isin": f"INE{idx:09d}",
                "quantity": data["quantity"],
                "t1_quantity": 0,
                "average_price": data["avg_price"],
                "last_price": data["avg_price"],
                "pnl": 0.0,
            }
            for idx, (symbol, data) in enumerate(self._positions.items(), 1)
            if data["quantity"] != 0
        ]

