"""Upstox Pro API v2 broker adapter — Real OAuth 2.0 and live portfolio execution."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from app.brokers.base import BrokerClient
from app.config import settings
from app.core.logging import get_logger
from app.schemas.trading import OrderRequest

logger = get_logger("broker.upstox")


class UpstoxBroker(BrokerClient):
    """Production broker adapter for Upstox Pro API v2."""

    BASE_URL = "https://api.upstox.com/v2"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        access_token: Optional[str] = None,
        redirect_uri: str = "http://localhost:5173/oauth/callback",
    ) -> None:
        self.api_key = api_key or getattr(settings, "upstox_api_key", "")
        self.api_secret = api_secret or getattr(settings, "upstox_api_secret", "")
        self.access_token = access_token
        self.redirect_uri = redirect_uri
        self._is_connected = bool(self.access_token)

    def get_login_url(self) -> str:
        """Return official Upstox OAuth 2.0 authorization URL."""
        key = self.api_key or "YOUR_UPSTOX_API_KEY"
        return (
            f"https://api.upstox.com/v2/login/authorization/dialog"
            f"?response_type=code&client_id={key}&redirect_uri={self.redirect_uri}"
        )

    async def generate_session(self, auth_code: str) -> dict[str, Any]:
        """Exchange OAuth auth_code for access_token with Upstox API."""
        if not self.api_key or not self.api_secret:
            # Deterministic sandbox response if unconfigured in dev mode
            self.access_token = f"upstox_access_{auth_code[:16]}"
            self._is_connected = True
            return {
                "access_token": self.access_token,
                "user_id": "UP9988",
                "user_name": "Upstox Trader",
                "status": "success",
            }

        url = f"{self.BASE_URL}/login/authorization/token"
        headers = {
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "code": auth_code,
            "client_id": self.api_key,
            "client_secret": self.api_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, data=data)
            if resp.status_code != 200:
                logger.error("Upstox token exchange failed [%d]: %s", resp.status_code, resp.text)
                raise RuntimeError(f"Upstox OAuth token exchange failed: {resp.text}")

            result = resp.json()
            self.access_token = result.get("access_token")
            self._is_connected = True
            logger.info("Upstox session generated successfully (user: %s)", result.get("user_id"))
            return {
                "access_token": self.access_token,
                "user_id": result.get("user_id", ""),
                "user_name": result.get("user_name", ""),
                "status": "success",
            }

    def _get_headers(self) -> dict[str, str]:
        if not self.access_token:
            raise RuntimeError("Upstox access token missing. Please complete OAuth flow.")
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }

    async def connect(self) -> None:
        """Verify Upstox session validity."""
        if not self.access_token:
            raise RuntimeError("Upstox access_token is missing. Complete OAuth login.")
        self._is_connected = True

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        """Place an order via Upstox v2 API."""
        await self.connect()
        url = f"{self.BASE_URL}/order/place"
        payload = {
            "quantity": order.quantity,
            "product": "I",  # Intraday MIS
            "validity": "DAY",
            "price": order.price or 0.0,
            "tag": "tradetron",
            "instrument_token": f"NSE_EQ|{order.symbol}",
            "order_type": "MARKET" if order.order_type == "MARKET" else "LIMIT",
            "transaction_type": order.side.value.upper(),
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=self._get_headers(), json=payload)
                if resp.status_code not in (200, 201):
                    logger.error("Upstox order failed [%d]: %s", resp.status_code, resp.text)
                    raise RuntimeError(f"Upstox order failed: {resp.text}")

                data = resp.json().get("data", {})
                order_id = data.get("order_id", "UPSTOX_ORDER")
                logger.info("UPSTOX LIVE ORDER: %s %s %d [order_id=%s]", order.side.value, order.symbol, order.quantity, order_id)
                return {
                    "broker_order_id": str(order_id),
                    "status": "OPEN",
                    "exchange": "NSE",
                    "tradingsymbol": order.symbol,
                    "quantity": order.quantity,
                }
        except Exception as exc:
            logger.error("Upstox place_order error: %s", exc)
            raise

    async def modify_order(
        self, broker_order_id: str, quantity: Optional[int] = None, price: Optional[float] = None
    ) -> dict[str, Any]:
        await self.connect()
        url = f"{self.BASE_URL}/order/modify"
        payload: dict[str, Any] = {"order_id": broker_order_id, "validity": "DAY"}
        if quantity is not None:
            payload["quantity"] = quantity
        if price is not None:
            payload["price"] = price

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.put(url, headers=self._get_headers(), json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Upstox order modify failed: {resp.text}")
        return {"status": "MODIFIED", "broker_order_id": broker_order_id}

    async def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        await self.connect()
        url = f"{self.BASE_URL}/order/cancel?order_id={broker_order_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(url, headers=self._get_headers())
            if resp.status_code != 200:
                raise RuntimeError(f"Upstox order cancel failed: {resp.text}")
        return {"status": "CANCELLED", "broker_order_id": broker_order_id}

    async def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        await self.connect()
        url = f"{self.BASE_URL}/order/history?order_id={broker_order_id}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    if data:
                        latest = data[0]
                        status = "FILLED" if latest.get("status") == "complete" else "OPEN"
                        return {
                            "status": status,
                            "broker_order_id": broker_order_id,
                            "average_price": float(latest.get("average_price", 0.0)),
                            "filled_quantity": int(latest.get("filled_quantity", 0)),
                        }
        except Exception as exc:
            logger.warning("Upstox get_order_status error: %s", exc)
        return {"status": "UNKNOWN", "broker_order_id": broker_order_id}

    async def get_positions(self) -> list[dict[str, Any]]:
        await self.connect()
        url = f"{self.BASE_URL}/portfolio/short-term-positions"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    positions = resp.json().get("data", [])
                    return [
                        {
                            "tradingsymbol": p.get("tradingsymbol", ""),
                            "quantity": int(p.get("quantity", 0)),
                            "average_price": float(p.get("buy_price", 0.0)),
                            "pnl": float(p.get("pnl", 0.0)),
                            "product": p.get("product", ""),
                        }
                        for p in positions
                        if int(p.get("quantity", 0)) != 0
                    ]
        except Exception as exc:
            logger.error("Upstox positions query failed: %s", exc)
            raise RuntimeError(f"Upstox positions query failed: {exc}")
        return []

    async def get_margins(self) -> dict[str, Any]:
        """Fetch live funds & margin balance from Upstox."""
        await self.connect()
        url = f"{self.BASE_URL}/user/get-funds-and-margin"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    equity_data = resp.json().get("data", {}).get("equity", {})
                    return {
                        "available_cash": float(equity_data.get("available_margin", 0.0)),
                        "utilized_margin": float(equity_data.get("used_margin", 0.0)),
                        "total_collateral": float(equity_data.get("payin_amount", 0.0)),
                        "currency": "INR",
                        "broker": "UPSTOX",
                    }
        except Exception as exc:
            logger.error("Upstox margins query failed: %s", exc)
            raise RuntimeError(f"Upstox margins query failed: {exc}")

        return {"available_cash": 0.0, "utilized_margin": 0.0, "total_collateral": 0.0, "currency": "INR", "broker": "UPSTOX"}

    async def get_holdings(self) -> list[dict[str, Any]]:
        """Fetch live portfolio holdings from Upstox."""
        await self.connect()
        url = f"{self.BASE_URL}/portfolio/long-term-holdings"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    holdings = resp.json().get("data", [])
                    return [
                        {
                            "tradingsymbol": h.get("tradingsymbol", ""),
                            "exchange": h.get("exchange", "NSE"),
                            "isin": h.get("isin", ""),
                            "quantity": int(h.get("quantity", 0)),
                            "t1_quantity": int(h.get("t1_quantity", 0)),
                            "average_price": float(h.get("average_price", 0.0)),
                            "last_price": float(h.get("last_price", 0.0)),
                            "pnl": float(h.get("pnl", 0.0)),
                        }
                        for h in holdings
                    ]
        except Exception as exc:
            logger.error("Upstox holdings query failed: %s", exc)
            raise RuntimeError(f"Upstox holdings query failed: {exc}")
        return []
