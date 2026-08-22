"""Angel One broker adapter.

Wraps the ``smartapi-python`` SDK behind the abstract ``BrokerClient``
interface. Uses synchronous SDK calls wrapped in ``asyncio.to_thread``
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

    def __init__(
        self,
        api_key: Optional[str] = None,
        client_code: Optional[str] = None,
        password: Optional[str] = None,
        totp_secret: Optional[str] = None,
        jwt_token: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or settings.angel_api_key or ""
        self.client_code = client_code or settings.angel_client_code or ""
        self.password = password or settings.angel_password or ""
        self.totp_secret = totp_secret or settings.angel_totp_secret or ""
        self.jwt_token = jwt_token

        self._client = None
        self._session: dict | None = None

        if self.api_key:
            try:
                from SmartApi import SmartConnect
                self._client = SmartConnect(api_key=self.api_key)
            except Exception as exc:
                logger.warning("SmartApi package import notice: %s", exc)

    async def validate_credentials(self) -> tuple[bool, str]:
        """Verify the validity of Angel One API Key & credentials against Angel One server."""
        if not self.api_key or len(self.api_key.strip()) < 8 or self.api_key.strip().lower() in ("test", "mock", "placeholder", "123", "angel_api_key"):
            return False, "Invalid API Key: Angel One requires a valid SmartAPI Key registered on smartapi.angelbroking.com"

        if not self.client_code or len(self.client_code.strip()) < 3:
            return False, "Invalid Client Code: Please enter your valid Angel One Client Code (e.g. S123456)"

        try:
            from SmartApi import SmartConnect

            client = SmartConnect(api_key=self.api_key.strip())

            # If JWT token provided
            if self.jwt_token:
                client.setAccessToken(self.jwt_token)
                res = await asyncio.to_thread(client.getProfile, self.jwt_token)
                if res and isinstance(res, dict) and res.get("status"):
                    return True, "Angel One SmartAPI session validated successfully."
                err_msg = res.get("message") if isinstance(res, dict) else "Session expired or invalid token"
                return False, f"Angel One authentication failed: {err_msg}"

            # If password and TOTP secret provided
            if self.password and self.totp_secret:
                try:
                    totp = pyotp.TOTP(self.totp_secret.strip()).now()
                except Exception:
                    return False, "Invalid TOTP Secret Key: Please provide a valid Base32 TOTP secret from Angel One"

                session = await asyncio.to_thread(
                    client.generateSession,
                    self.client_code.strip(),
                    self.password.strip(),
                    totp,
                )

                if session and isinstance(session, dict) and session.get("status"):
                    self._session = session
                    self._client = client
                    return True, "Angel One SmartAPI authenticated successfully."

                err_msg = session.get("message") if isinstance(session, dict) else "Invalid credentials"
                return False, f"Angel One rejected login: {err_msg}"

            # If only API Key and Client Code were provided
            return False, "Angel One requires API Key, Client ID, and Password or TOTP to authenticate live sessions."

        except Exception as exc:
            return False, f"Angel One connection error: {str(exc)}"

    async def connect(self) -> None:
        """Authenticate with Angel One using TOTP."""
        if self._session:
            return

        if not self._client:
            from SmartApi import SmartConnect
            self._client = SmartConnect(api_key=self.api_key)

        if not self.totp_secret:
            raise RuntimeError("Angel One TOTP secret is not configured.")

        totp = pyotp.TOTP(self.totp_secret).now()

        self._session = await asyncio.to_thread(
            self._client.generateSession,
            self.client_code,
            self.password,
            totp,
        )

        if not self._session or not self._session.get("status"):
            msg = self._session.get("message", "Angel One login failed") if self._session else "No response from Angel One"
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

        return {"available_cash": None, "utilized_margin": None, "currency": "INR"}

    async def get_holdings(self) -> list[dict[str, Any]]:
        await self.connect()
        try:
            res = await asyncio.to_thread(self._client.holding)
            if res and res.get("data"):
                return res["data"]
        except Exception as exc:
            logger.warning("Angel One holdings fetch failed: %s", exc)
        return []
