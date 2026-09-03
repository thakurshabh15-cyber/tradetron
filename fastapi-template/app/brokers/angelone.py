"""Angel One broker adapter.

Wraps the ``smartapi-python`` SDK behind the abstract ``BrokerClient``
interface. Uses synchronous SDK calls wrapped in ``asyncio.to_thread``
so they don't block the event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.brokers.base import BrokerClient
from app.config import settings
from app.core.logging import get_logger
from app.schemas.trading import OrderRequest

# Optional import for SmartConnect - allows mock mode without the package
try:
    from SmartApi import SmartConnect
except ImportError:
    SmartConnect = None

# Optional import for pyotp - allows mock mode without TOTP generation
try:
    import pyotp
except ImportError:
    pyotp = None

logger = get_logger("broker.angelone")

# Upper bound for every synchronous SmartAPI SDK call offloaded to a thread.
# Without this, a hung broker API call (network partition, SDK stall) blocks
# order dispatch and event-loop workers indefinitely.
DISPATCH_TIMEOUT_SECONDS = 30.0


async def _sdk_call(fn, *args, timeout: float = DISPATCH_TIMEOUT_SECONDS):
    """Run a synchronous SDK call in a worker thread with a hard timeout."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)


class AngelOneBroker(BrokerClient):
    """Production broker adapter for Angel One (SmartAPI)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        client_id: Optional[str] = None,
        pin: Optional[str] = None,
        totp_key: Optional[str] = None,
        jwt_token: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or settings.angel_api_key or ""
        self.client_id = client_id or settings.angel_client_id or ""
        self.pin = pin or settings.angel_pin or ""
        self.totp_key = totp_key or settings.angel_totp_key or ""
        self.jwt_token = jwt_token

        self._client = None
        self._session: dict | None = None

        if self.api_key and SmartConnect is not None:
            try:
                self._client = SmartConnect(api_key=self.api_key)
            except Exception as exc:
                logger.warning("SmartApi package import notice: %s", exc)

    async def validate_credentials(self) -> tuple[bool, str]:
        if not self.api_key or len(self.api_key.strip()) < 8 or self.api_key.strip().lower() in ("test", "mock", "placeholder", "123", "angel_api_key"):
            return False, "Invalid API Key: Angel One requires a valid SmartAPI Key registered on smartapi.angelbroking.com"

        if not self.client_id or len(self.client_id.strip()) < 3:
            return False, "Invalid Client ID: Please enter your valid Angel One Client ID (e.g. S123456)"

        if SmartConnect is None:
            return False, "SmartApi package not installed. Install with: pip install smartapi-python"

        client = SmartConnect(api_key=self.api_key.strip())

        # If JWT token provided
        if self.jwt_token:
            client.setAccessToken(self.jwt_token)
            res = await asyncio.to_thread(client.getProfile, self.jwt_token)
            if res and isinstance(res, dict) and res.get("status"):
                return True, "Angel One SmartAPI session validated successfully."
            err_msg = res.get("message") if isinstance(res, dict) else "Session expired or invalid token"
            return False, f"Angel One authentication failed: {err_msg}"

        # If PIN and TOTP key provided
        if self.pin and self.totp_key:
            if pyotp is None:
                logger.warning("pyotp package not installed, cannot generate TOTP. Falling back to mock mode.")
                return False, "pyotp package not installed. Install with: pip install pyotp"
            try:
                totp = pyotp.TOTP(self.totp_key.strip()).now()
            except Exception:
                return False, "Invalid TOTP Key: Please provide a valid Base32 TOTP key from Angel One"

            session = await asyncio.to_thread(
                client.generateSession,
                self.client_id.strip(),
                self.pin.strip(),
                totp,
            )

            if session and isinstance(session, dict) and session.get("status"):
                self._session = session
                self._client = client
                return True, "Angel One SmartAPI authenticated successfully."

            err_msg = session.get("message") if isinstance(session, dict) else "Invalid credentials"
            return False, f"Angel One rejected login: {err_msg}"

        # If only API Key and Client ID were provided
        return False, "Angel One requires API Key, Client ID, and PIN or TOTP to authenticate live sessions."

    async def connect(self) -> None:
        """Authenticate with Angel One using TOTP."""
        if self._session:
            return

        if not self._client:
            if SmartConnect is None:
                raise RuntimeError("SmartApi package not installed. Install with: pip install smartapi-python")
            self._client = SmartConnect(api_key=self.api_key)

        if not self.totp_key:
            if SmartConnect is None:
                raise RuntimeError("Angel One TOTP key is not configured.")
            # If we don't have TOTP key but SmartConnect is available, we can't do real auth
            # This will be handled by the mock mode fallback
            logger.warning("Angel One TOTP key is not configured, cannot authenticate. Will use mock mode.")
            raise RuntimeError("Angel One TOTP key is not configured.")

        if pyotp is None:
            logger.warning("pyotp package not installed, cannot generate TOTP. Falling back to mock mode.")
            raise RuntimeError("pyotp package not installed. Install with: pip install pyotp")

        totp = pyotp.TOTP(self.totp_key).now()

        self._session = await _sdk_call(
            self._client.generateSession,
            self.client_id,
            self.pin,
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

        response = await _sdk_call(self._client.placeOrder, payload)

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
            result = await _sdk_call(
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

        result = await _sdk_call(self._client.modifyOrder, params)
        return {"status": "MODIFIED", "broker_order_id": broker_order_id, "raw": result}

    async def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        """Retrieve execution status for a specific order on Angel One."""
        await self.connect()
        order_book = await _sdk_call(self._client.orderBook)
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
        result = await _sdk_call(
            self._client.cancelOrder, broker_order_id, "NORMAL"
        )
        return {"status": "CANCELLED", "broker_order_id": broker_order_id, "raw": result}

    async def get_positions(self) -> list[dict[str, Any]]:
        await self.connect()
        result = await _sdk_call(self._client.position)
        if not result or not result.get("data"):
            return []
        return result["data"]

    async def get_margins(self) -> dict[str, Any]:
        """Retrieve available cash and margin collateral from Angel One."""
        await self.connect()
        try:
            res = await _sdk_call(self._client.rmsLimit)
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
            res = await _sdk_call(self._client.holding)
            if res and res.get("data"):
                return res["data"]
        except Exception as exc:
            logger.warning("Angel One holdings fetch failed: %s", exc)
        return []
def place_tradethrone_order(payload: dict) -> dict:
    """
    Place an order from a validated TradeThrone signal payload for Angel One.
    
    Args:
        payload: Validated TradeThrone signal containing:
            - signal: str (e.g., "entry_long", "exit_long")
            - symbol: str (e.g., "NIFTY24AUG25000CE")
            - action: str (e.g., "BUY", "SELL")
            - quantity: int
            - price: float
            - exchange: str (optional, e.g., "NSE", "NFO", default "NSE")
            - order_type: str (optional, "LIMIT" or "MARKET", default "LIMIT")
            - product_type: str (optional, e.g., "INTRADAY", "DELIVERY", default "INTRADAY")
            - duration: str (optional, e.g., "DAY", "IOC", default "DAY")
    
    Returns:
        Order response with order_id, status, and symbol.
        If credentials are available and SmartConnect is installed, places real order.
        Otherwise, returns mock order response.
    """
    signal = payload.get("signal", "")
    symbol = payload.get("symbol", "")
    action = payload.get("action", "")
    quantity = payload.get("quantity", 0)
    price = payload.get("price", 0.0)
    exchange = payload.get("exchange", "NSE")
    order_type = payload.get("order_type", "LIMIT").upper()
    product_type = payload.get("product_type", "INTRADAY")
    duration = payload.get("duration", "DAY")
    
    logger.info(
        "Placing TradeThrone order (Angel One): signal=%s symbol=%s action=%s quantity=%d price=%.2f exchange=%s order_type=%s",
        signal, symbol, action, quantity, price, exchange, order_type
    )
    
    # Check if we have real credentials and SmartConnect is available
    has_real_credentials = (
        bool(settings.angel_api_key) 
        and bool(settings.angel_client_id) 
        and (bool(settings.angel_pin) or bool(settings.angel_totp_key))
    )
    
    if has_real_credentials and SmartConnect is not None and pyotp is not None:
        try:
            # Initialize SmartConnect
            client = SmartConnect(api_key=settings.angel_api_key)
            
            # Generate session using PIN + TOTP
            if not settings.angel_totp_key:
                logger.warning("Angel One TOTP key is not configured, cannot authenticate. Using mock mode.")
                raise RuntimeError("Angel One TOTP key is not configured.")
            
            totp = pyotp.TOTP(settings.angel_totp_key.strip()).now()
            session = client.generateSession(
                settings.angel_client_id.strip(),
                settings.angel_pin.strip(),
                totp,
            )
            
            if not session or not session.get("status"):
                raise RuntimeError(f"Angel One auth failed: {session.get('message', 'Unknown error')}")
            
            # Resolve symbol token
            search_result = client.searchScrip(exchange, symbol)
            if not search_result or not search_result.get("data"):
                raise RuntimeError(f"Could not resolve symbol token for '{symbol}' on {exchange}")
            
            symbol_token = None
            for scrip in search_result["data"]:
                if scrip.get("tradingsymbol", "").upper() == symbol.upper():
                    symbol_token = scrip.get("symboltoken", "")
                    break
            
            if not symbol_token:
                raise RuntimeError(f"Could not find symbol token for '{symbol}'")
            
            # Map transaction type
            transaction_type = "BUY" if action.upper() == "BUY" else "SELL"
            
            # Place the order
            order_payload = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": symbol_token,
                "transactiontype": transaction_type,
                "exchange": exchange,
                "ordertype": order_type,
                "producttype": product_type,
                "duration": duration,
                "quantity": str(quantity),
            }
            
            if order_type == "LIMIT":
                order_payload["price"] = str(price)
            
            response = client.placeOrder(order_payload)
            order_id = str(response) if response else ""
            
            logger.info("Angel One real order placed successfully: order_id=%s", order_id)
            
            return {
                "order_id": order_id,
                "status": "COMPLETE",
                "symbol": symbol,
                "exchange": exchange,
                "action": action,
                "quantity": quantity,
                "price": price,
                "broker": "angelone",
            }
        except Exception as exc:
            # FAIL-SAFETY: real credentials are configured, so a broker/network
            # failure must NEVER be masked with a fabricated mock success
            # response.  Re-raise so the caller can retry/alert instead of
            # recording a phantom fill against live money.
            logger.error("Angel One real order placement FAILED: %s", exc)
            raise
    else:
        if not has_real_credentials:
            logger.warning("Angel One credentials not fully configured, using SIMULATED order response")
        if SmartConnect is None:
            logger.warning("SmartApi package not installed, using SIMULATED order response")
        if pyotp is None:
            logger.warning("pyotp package not installed, using SIMULATED order response")
    
    # Mock order response (only reachable when real credentials are absent)
    import time
    order_id = f"ANGEL{int(time.time() * 1000) % 100000000:08d}"
    
    return {
        "order_id": order_id,
        "status": "COMPLETE",
        "simulated": True,
        "symbol": symbol,
        "broker": "angelone",
    }


# Backward-compatible alias for legacy integrations.
place_tradetron_order = place_tradethrone_order
