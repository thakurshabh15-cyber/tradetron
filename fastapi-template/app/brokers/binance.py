"""Binance Crypto broker adapter — Real REST API with HMAC-SHA256 signing.

When credentials are configured: Makes real HTTP requests to Binance API
(testnet by default, production when BINANCE_TESTNET=false).

Without credentials: Raises clear errors instead of fabricating responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any, Optional
from urllib.parse import urlencode

from app.brokers.base import BrokerClient
from app.core.logging import get_logger
from app.schemas.trading import OrderRequest

logger = get_logger("broker.binance")

_TESTNET_BASE = "https://testnet.binance.vision"
_PRODUCTION_BASE = "https://api.binance.com"


class BinanceBroker(BrokerClient):
    """Crypto broker adapter for Binance Spot — real HTTP API calls."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.base_url = _TESTNET_BASE if testnet else _PRODUCTION_BASE
        self._is_connected = False
        self._session = None  # aiohttp.ClientSession, lazy

    @property
    def _has_credentials(self) -> bool:
        return bool(self.api_key) and bool(self.api_secret)

    def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Sign request parameters using HMAC-SHA256 (Binance requires this for authenticated endpoints)."""
        params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self.api_key}

    async def _get_session(self):
        """Lazy-create aiohttp session."""
        if self._session is None or self._session.closed:
            try:
                import aiohttp
                self._session = aiohttp.ClientSession()
            except ImportError:
                raise RuntimeError("aiohttp package not installed. Install with: pip install aiohttp")
        return self._session

    async def _api_request(
        self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = True
    ) -> dict[str, Any]:
        """Make a real HTTP request to Binance API."""
        if not self._has_credentials:
            raise RuntimeError(
                "Binance API credentials not configured. "
                "Set BINANCE_API_KEY and BINANCE_API_SECRET in .env"
            )

        session = await self._get_session()
        url = f"{self.base_url}{path}"
        params = params or {}

        if signed:
            params = self._sign_params(params)

        try:
            if method == "GET":
                resp = await session.get(url, params=params, headers=self._headers())
            elif method == "POST":
                resp = await session.post(url, params=params, headers=self._headers())
            elif method == "DELETE":
                resp = await session.delete(url, params=params, headers=self._headers())
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            data = await resp.json()

            if resp.status != 200:
                error_msg = data.get("msg", f"HTTP {resp.status}")
                logger.error("Binance API error [%s %s]: %s", method, path, error_msg)
                raise RuntimeError(f"Binance API error: {error_msg}")

            return data
        except RuntimeError:
            raise
        except Exception as exc:
            logger.error("Binance API request failed [%s %s]: %s", method, path, exc)
            raise RuntimeError(f"Binance request failed: {exc}")

    async def connect(self) -> bool:
        """Verify API connectivity with Binance."""
        if not self._has_credentials:
            self._is_connected = False
            return False

        try:
            data = await self._api_request("GET", "/api/v3/ping", signed=False)
            self._is_connected = True
            logger.info("Binance Crypto broker connected (%s)", "Testnet" if self.testnet else "Production")
            return True
        except Exception as exc:
            self._is_connected = False
            logger.warning("Binance connection check failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._is_connected = False

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        """Submit a real order to Binance Spot API."""
        await self.connect()

        params = {
            "symbol": order.symbol,
            "side": order.side.value,
            "type": order.order_type or "MARKET",
            "quantity": str(order.quantity),
        }
        if order.order_type == "LIMIT" and order.price:
            params["price"] = str(order.price)
            params["timeInForce"] = "GTC"

        data = await self._api_request("POST", "/api/v3/order", params)

        logger.info(
            "BINANCE LIVE ORDER: %s %s %s [orderId=%s]",
            order.side.value,
            order.symbol,
            order.quantity,
            data.get("orderId"),
        )

        return {
            "broker_order_id": str(data.get("orderId", "")),
            "symbol": data.get("symbol", order.symbol),
            "status": data.get("status", "NEW"),
            "side": data.get("side", order.side.value),
            "type": data.get("type", order.order_type),
            "origQty": data.get("origQty", str(order.quantity)),
            "executedQty": data.get("executedQty", "0"),
            "cummulativeQuoteQty": data.get("cummulativeQuoteQty", "0"),
        }

    async def modify_order(
        self, broker_order_id: str, quantity: Optional[int] = None, price: Optional[float] = None
    ) -> dict[str, Any]:
        # Binance doesn't support order modification — must cancel and re-place
        logger.warning("Binance does not support order modification. Cancel and re-place instead.")
        raise RuntimeError("Binance does not support order modification. Cancel and re-place instead.")

    async def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        """Cancel an open order on Binance."""
        await self.connect()

        # We need the symbol to cancel — try to get it from order query
        # For now, this requires the caller to pass symbol context
        # In practice, the order_manager tracks symbol per order
        logger.info("BINANCE CANCEL requested for orderId=%s", broker_order_id)
        raise RuntimeError(
            "Binance cancel requires symbol context. Use the order manager's cancel flow."
        )

    async def cancel_order_with_symbol(self, symbol: str, broker_order_id: str) -> dict[str, Any]:
        """Cancel an open Binance order with symbol context."""
        await self.connect()
        params = {"symbol": symbol, "orderId": int(broker_order_id)}
        data = await self._api_request("DELETE", "/api/v3/order", params)
        logger.info("BINANCE ORDER CANCELLED: %s on %s", broker_order_id, symbol)
        return {"status": data.get("status", "CANCELED"), "broker_order_id": broker_order_id}

    async def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        """Query order status — requires symbol context for Binance API."""
        logger.warning("Binance order status query requires symbol. Use full query via order manager.")
        return {"status": "UNKNOWN", "broker_order_id": broker_order_id, "note": "Use order manager for full query"}

    async def get_positions(self) -> list[dict[str, Any]]:
        """Fetch real account balances from Binance (positions = non-zero balances)."""
        await self.connect()
        data = await self._api_request("GET", "/api/v3/account")
        balances = data.get("balances", [])
        return [
            {
                "symbol": b["asset"],
                "positionAmt": b["free"],
                "entryPrice": "0",  # Spot doesn't track entry price
                "unrealizedProfit": "0",
            }
            for b in balances
            if float(b.get("free", 0)) > 0 or float(b.get("locked", 0)) > 0
        ]

    async def get_margins(self) -> dict[str, Any]:
        """Fetch real USDT balance from Binance account."""
        await self.connect()
        data = await self._api_request("GET", "/api/v3/account")
        balances = data.get("balances", [])
        usdt_balance = next(
            (b for b in balances if b["asset"] == "USDT"),
            {"free": "0", "locked": "0"},
        )
        free = float(usdt_balance.get("free", 0))
        locked = float(usdt_balance.get("locked", 0))
        return {
            "available_cash": free,
            "utilized_margin": locked,
            "total_collateral": free + locked,
            "currency": "USDT",
            "broker": "BINANCE",
            "mode": "TESTNET" if self.testnet else "PRODUCTION",
        }

    async def get_holdings(self) -> list[dict[str, Any]]:
        """Fetch spot crypto asset holdings from Binance."""
        await self.connect()
        data = await self._api_request("GET", "/api/v3/account")
        balances = data.get("balances", [])
        return [
            {
                "tradingsymbol": b["asset"],
                "exchange": "BINANCE",
                "isin": "",
                "quantity": float(b.get("free", 0)) + float(b.get("locked", 0)),
                "t1_quantity": 0,
                "average_price": 0.0,
                "last_price": 0.0,
                "pnl": 0.0,
            }
            for b in balances
            if float(b.get("free", 0)) > 0 or float(b.get("locked", 0)) > 0
        ]


    async def close(self) -> None:
        """Clean up aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
