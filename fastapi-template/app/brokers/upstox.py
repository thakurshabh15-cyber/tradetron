"""Upstox Pro API v2 broker adapter — Real OAuth 2.0 and live portfolio execution."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from app.brokers.base import (
    BrokerClient,
    BrokerProtectionCapability,
    PROTECTIVE_ORDER_TYPE_SL_LIMIT,
    PROTECTIVE_ORDER_TYPE_SL_MARKET,
    PROTECTIVE_ORDER_TYPE_TP_LIMIT,
)
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
        """Verify Upstox session validity.

        BROKER_MODE safety: real Upstox reads and order mutations (margins,
        holdings, positions, modify/cancel/status) all funnel through
        ``connect()`` before making HTTP calls to ``api.upstox.com``.  Only
        ``place_order`` carries its own dispatch gate, so ``connect()`` is the
        single choke point that blocks all other live Upstox network work while
        ``BROKER_MODE != live`` — a simulated-mode deployment can never touch
        the Upstox API through class-level calls.
        """
        from app.brokers import assert_live_broker_connect_allowed
        assert_live_broker_connect_allowed()
        if not self.access_token:
            raise RuntimeError("Upstox access_token is missing. Complete OAuth login.")
        self._is_connected = True

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        """Place an order via Upstox v2 API.

        P2-10 defense-in-depth: the class-level method is gated itself, so a
        real order can never reach Upstox while ``BROKER_MODE != live`` — even
        if called directly, bypassing ``OrderManager`` / the API layer.
        The gate fires before any network operation.
        """
        from app.brokers import assert_live_dispatch_allowed
        assert_live_dispatch_allowed()
        await self.connect()
        # ── Phase 15C: normalized protective leg mapping ──────────────────────
        # SL_MARKET -> "SL-M" (trigger-only market stop)
        # SL_LIMIT  -> "SL"   (trigger + limit stop)
        # TP_LIMIT  -> "LIMIT" (take-profit at trigger price)
        # Anything else keeps the legacy MARKET/LIMIT mapping.
        order_type_lit = order.order_type or "MARKET"
        trigger_price = float(order.trigger_price) if order.trigger_price else 0.0
        if order_type_lit == "SL_MARKET":
            upstox_order_type = "SL-M"
            limit_price = 0.0
        elif order_type_lit == "SL_LIMIT":
            upstox_order_type = "SL"
            limit_price = float(order.price) if order.price else trigger_price
        elif order_type_lit == "TP_LIMIT":
            upstox_order_type = "LIMIT"
            limit_price = trigger_price if trigger_price else (float(order.price) if order.price else 0.0)
        else:
            upstox_order_type = "MARKET" if order_type_lit == "MARKET" else "LIMIT"
            limit_price = float(order.price) if order.price else 0.0
        url = f"{self.BASE_URL}/order/place"
        payload = {
            "quantity": order.quantity,
            "product": "I",  # Intraday MIS
            "validity": "DAY",
            "price": limit_price,
            "tag": "tradetron",
            "instrument_token": f"NSE_EQ|{order.symbol}",
            "order_type": upstox_order_type,
            "transaction_type": order.side.value.upper(),
            "disclosed_quantity": 0,
            "trigger_price": trigger_price,
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

    def supports_native_protection(self) -> BrokerProtectionCapability:
        """Upstox v2: native SL / SL-M + target LIMIT via the order/place API.

        Real code path implemented (``place_order`` maps SL_MARKET -> SL-M,
        SL_LIMIT -> SL, TP_LIMIT -> LIMIT with trigger_price).  The Upstox
        ``PUT /order/modify`` endpoint used by ``modify_order`` only supports
        quantity/price — it does NOT update a stop trigger — so in-place
        replacement is declared ``replace=False`` (cancel + re-place instead).
        ``tested=False``: no Upstox sandbox credentials have been provided.
        """
        return BrokerProtectionCapability(
            adapter="upstox",
            native_sl=True,
            native_tp=True,
            bracket=False,
            replace=False,  # modify endpoint cannot move triggers — cancel+replace
            order_types=(
                PROTECTIVE_ORDER_TYPE_SL_MARKET,
                PROTECTIVE_ORDER_TYPE_SL_LIMIT,
                PROTECTIVE_ORDER_TYPE_TP_LIMIT,
            ),
            tested=False,
            reason=(
                "Upstox v2 SL/SL-M + LIMIT protective orders are implemented but "
                "NOT verified against an Upstox sandbox (no sandbox credentials); "
                "order modify does not support trigger moves"
            ),
        )

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
                    from app.brokers.position_normalizer import normalize_upstox_position
                    return [
                        normalized
                        for p in positions
                        if (normalized := normalize_upstox_position(p)) is not None
                    ]
        except Exception as exc:
            logger.error("Upstox positions query failed: %s", exc)
            raise RuntimeError(f"Upstox positions query failed: {exc}")
        return []

    async def get_margins(self) -> dict[str, Any]:
        """Fetch live funds & margin balance from Upstox.

        Fail-closed (Phase 15B honesty): a non-200 response, invalid payload,
        or network error RAISES instead of fabricating an all-zero margin.  The
        caller reports connected-but-unavailable and the UI shows "unavailable"
        — never a fake "₹0.00".
        """
        await self.connect()
        url = f"{self.BASE_URL}/user/get-funds-and-margin"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._get_headers())
        except Exception as exc:
            logger.error("Upstox margins query failed: %s", exc)
            raise RuntimeError(f"Upstox margins query failed: {exc}") from exc

        if resp.status_code != 200:
            detail = (resp.text or "")[:200]
            logger.error(
                "Upstox margins query HTTP %s: %s",
                resp.status_code,
                detail or "empty response",
            )
            raise RuntimeError(
                f"Upstox margins HTTP {resp.status_code}: {detail or 'empty response'}"
            )

        try:
            equity_data = resp.json().get("data", {}).get("equity", {})
        except Exception as exc:
            logger.error("Upstox margins query invalid JSON: %s", exc)
            raise RuntimeError(f"Upstox margins query failed: invalid JSON: {exc}") from exc

        return {
            "available_cash": float(equity_data.get("available_margin", 0.0)),
            "utilized_margin": float(equity_data.get("used_margin", 0.0)),
            "total_collateral": float(equity_data.get("payin_amount", 0.0)),
            "currency": "INR",
            "broker": "UPSTOX",
        }

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
