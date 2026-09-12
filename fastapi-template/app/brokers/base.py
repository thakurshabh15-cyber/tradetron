"""Abstract broker interface contract for production multi-broker execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from app.schemas.trading import OrderRequest


# ── Phase 15C: normalized protective-order type literals ────────────────────
# The protective-order manager expresses intent with these normalized literals;
# each adapter's ``place_order`` maps them to its provider-specific enum and
# wire fields.  ``supports_native_protection().order_types`` enumerates the
# subset a provider actually implements (never assumed).
PROTECTIVE_ORDER_TYPE_SL_MARKET = "SL_MARKET"      # trigger-only market stop
PROTECTIVE_ORDER_TYPE_SL_LIMIT = "SL_LIMIT"        # trigger + limit price stop
PROTECTIVE_ORDER_TYPE_TP_LIMIT = "TP_LIMIT"        # take-profit limit
PROTECTIVE_ORDER_TYPES = (
    PROTECTIVE_ORDER_TYPE_SL_MARKET,
    PROTECTIVE_ORDER_TYPE_SL_LIMIT,
    PROTECTIVE_ORDER_TYPE_TP_LIMIT,
)


@dataclass(frozen=True)
class BrokerProtectionCapability:
    """Explicit, provider-specific declaration of exchange-side SL/TP support.

    Phase 15C contract — a provider is NEVER assumed to support native
    protective orders just because the generic ``place_order`` method exists.
    Each real adapter declares with evidence exactly what its own API does:

      native_sl      : the adapter can place an exchange-side stop-loss order
                       (triggered market/limit) for an open position.
      native_tp      : the adapter can place an exchange-side take-profit
                       (limit) order against an open position.
      bracket        : the adapter additionally supports a true bracket/OCO
                       pairing in a single broker construct.
      replace        : the adapter can MODIFY a pending protective order in
                       place (price / trigger).  ``False`` means cancel + re-place.
      order_types    : the exact order-type literals the adapter maps for
                       protective legs (STOP_LOSS, TAKE_PROFIT).
      tested         : True ONLY after verification against a real broker
                       sandbox/testnet.  Always False when untested.
      reason         : human-readable evidence / limitation note.

    An adapter returning ``native_sl=False`` / ``native_tp=False`` means the
    protective-order manager MUST NOT attempt that leg and MUST surface an
    honest PROTECTION_FAILED / UNSUPPORTED state — never a fabricated success.
    """

    adapter: str
    native_sl: bool = False
    native_tp: bool = False
    bracket: bool = False
    replace: bool = False
    order_types: tuple[str, ...] = field(default_factory=tuple)
    tested: bool = False
    reason: str = ""


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

    def supports_native_protection(self) -> BrokerProtectionCapability:
        """Declare exchange-side SL/TP capability for this adapter.

        Non-abstract default: an adapter that does NOT override this does NOT
        support native protective orders.  The protective-order manager reads
        this capability BEFORE attempting any placement and fails closed on
        unsupported legs — a generic ``place_order`` method never implies
        broker-side protection.
        """
        return BrokerProtectionCapability(
            adapter=type(self).__name__,
            native_sl=False,
            native_tp=False,
            bracket=False,
            replace=False,
            reason=(
                f"{type(self).__name__} does not declare native exchange-side "
                "SL/TP support — engine-only protection only"
            ),
        )

