"""Angel One broker adapter.

Wraps the ``smartapi-python`` SDK behind the abstract ``BrokerClient``
interface.  Uses synchronous SDK calls wrapped in ``asyncio.to_thread``
so they don't block the event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pyotp

from app.brokers.base import BrokerClient
from app.config import settings
from app.core.logging import get_logger
from app.schemas.trading import OrderRequest

logger = get_logger("broker.angelone")


class AngelOneBroker(BrokerClient):
    """Production broker adapter for Angel One (SmartAPI)."""

    def __init__(self) -> None:
        # Lazy import — ``smartapi-python`` is only required in live mode
        from SmartApi import SmartConnect

        self._client = SmartConnect(api_key=settings.angel_api_key)
        self._session: dict | None = None

    async def connect(self) -> None:
        """Authenticate with Angel One using TOTP."""
        if self._session:
            return

        totp = pyotp.TOTP(settings.angel_totp_secret).now()

        self._session = await asyncio.to_thread(
            self._client.generateSession,
            settings.angel_client_code,
            settings.angel_password,
            totp,
        )

        if not self._session.get("status"):
            msg = self._session.get("message", "Angel One login failed")
            logger.error("Angel One auth failed: %s", msg)
            raise RuntimeError(msg)

        logger.info("Angel One authenticated successfully")

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        """Place a market order via Angel One with automatic symbol token resolution."""
        await self.connect()

        # Resolve symbol token from instrument master
        symbol_token = await self._resolve_symbol_token(order.symbol)

        payload = {
            "variety": "NORMAL",
            "tradingsymbol": order.symbol,
            "symboltoken": symbol_token,
            "transactiontype": order.side.value,
            "exchange": "NSE",
            "ordertype": order.order_type,
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": str(order.quantity),
        }

        response = await asyncio.to_thread(self._client.placeOrder, payload)

        if not response:
            raise RuntimeError(f"Angel One order failed for {order.symbol}")

        logger.info(
            "LIVE ORDER: %s %s %d  broker_id=%s",
            order.side.value,
            order.symbol,
            order.quantity,
            response,
        )

        return {
            "broker_order_id": str(response),
            "status": "OPEN",
            "filled_price": 0,
            "filled_quantity": 0,
        }

    async def _resolve_symbol_token(self, symbol: str) -> str:
        """Resolve trading symbol to Angel One's instrument token via searchScrip."""
        try:
            result = await asyncio.to_thread(
                self._client.searchScrip, "NSE", symbol
            )
            if result and result.get("data"):
                for scrip in result["data"]:
                    if scrip.get("tradingsymbol", "").upper() == symbol.upper():
                        token = scrip.get("symboltoken", "")
                        logger.debug("Resolved %s -> token %s", symbol, token)
                        return token
                # Fallback to first result
                token = result["data"][0].get("symboltoken", "")
                logger.debug("Resolved %s -> token %s (first match)", symbol, token)
                return token
        except Exception as exc:
            logger.error("Symbol token resolution failed for %s: %s", symbol, exc)

        raise RuntimeError(
            f"Could not resolve symbol token for '{symbol}' on Angel One. "
            f"Verify the symbol exists on NSE."
        )

    async def modify_order(
        self, broker_order_id: str, quantity: Optional[int] = None, price: Optional[float] = None
    ) -> dict[str, Any]:
        """Modify an active pending order on Angel One."""
        await self.connect()
        params: dict[str, Any] = {
            "variety": "NORMAL",
            "orderid": broker_order_id,
            "ordertype": "LIMIT" if price else "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
        }
        if quantity is not None:
            params["quantity"] = str(quantity)
        if price is not None:
            params["price"] = str(price)

        result = await asyncio.to_thread(self._client.modifyOrder, params)
        return {"status": "MODIFIED", "broker_order_id": broker_order_id, "raw": result}

    async def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        """Retrieve execution status for a specific order on Angel One."""
        await self.connect()
        order_book = await asyncio.to_thread(self._client.orderBook)
        if order_book and order_book.get("data"):
            for o in order_book["data"]:
                if str(o.get("orderid")) == str(broker_order_id):
                    return {
                        "broker_order_id": broker_order_id,
                        "status": o.get("status", "OPEN"),
                        "filled_quantity": int(o.get("filledshares", 0)),
                        "average_price": float(o.get("averageprice", 0.0)),
                    }
        return {"broker_order_id": broker_order_id, "status": "UNKNOWN"}

    async def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        await self.connect()
        result = await asyncio.to_thread(
            self._client.cancelOrder, broker_order_id, "NORMAL"
        )
        return {"status": "CANCELLED", "broker_order_id": broker_order_id, "raw": result}

    async def get_positions(self) -> list[dict[str, Any]]:
        await self.connect()
        result = await asyncio.to_thread(self._client.position)
        if not result or not result.get("data"):
            return []
        return result["data"]

    async def get_margins(self) -> dict[str, Any]:
        """Retrieve available cash and margin collateral from Angel One."""
        await self.connect()
        try:
            res = await asyncio.to_thread(self._client.rmsLimit)
            if res and res.get("data"):
                data = res["data"]
                return {
                    "available_cash": float(data.get("net", 0.0)),
                    "utilized_margin": float(data.get("utilizedamount", 0.0)),
                    "total_collateral": float(data.get("collateralvalue", 0.0)),
                }
        except Exception as exc:
            logger.warning("Angel One RMS limit fetch failed: %s", exc)

    async def get_holdings(self) -> list[dict[str, Any]]:
        """Retrieve user equity holdings from Angel One."""
        await self.connect()
        try:
            res = await asyncio.to_thread(self._client.holding)
            if res and res.get("data"):
                return [
                    {
                        "tradingsymbol": h.get("tradingsymbol", ""),
                        "exchange": h.get("exchange", "NSE"),
                        "isin": h.get("isin", ""),
                        "quantity": int(h.get("quantity", 0)),
                        "t1_quantity": int(h.get("t1quantity", 0)),
                        "average_price": float(h.get("avgprice", 0.0)),
                        "last_price": float(h.get("ltp", 0.0)),
                        "pnl": float(h.get("pnl", 0.0)),
                    }
                    for h in res["data"]
                ]
        except Exception as exc:
            logger.warning("Angel One holding fetch failed: %s", exc)

        return []

