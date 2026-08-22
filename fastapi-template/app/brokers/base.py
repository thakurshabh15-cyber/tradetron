"""Abstract broker interface contract for production multi-broker execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from app.schemas.trading import OrderRequest


class BrokerClient(ABC):
    """Protocol that every broker adapter (Angel One, Zerodha Kite, Binance, Simulated) must implement."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection / authenticate with the broker API."""
        pass

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        """Submit an order to the broker. Returns normalized order response."""
        pass

    @abstractmethod
    async def modify_order(
        self, broker_order_id: str, quantity: Optional[int] = None, price: Optional[float] = None
    ) -> dict[str, Any]:
        """Modify an existing pending order."""
        pass

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        """Cancel a pending order by its broker-assigned ID."""
        pass

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        """Query real-time execution status for a specific broker order."""
        pass

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]:
        """Return current live open positions."""
        pass

    @abstractmethod
    async def get_margins(self) -> dict[str, Any]:
        """Return available cash, utilized margin, and total collateral."""
        pass

    @abstractmethod
    async def get_holdings(self) -> list[dict[str, Any]]:
        """Return user portfolio holdings directly from the broker."""
        pass

