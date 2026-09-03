"""Broker integrations and adapter factory.

Includes a PHASE-3 SAFETY GUARD: real (non-simulated) broker order dispatch is
hard-blocked unless ``BROKER_MODE=live`` is set.  Every LIVE order-dispatch
path (``/api/trades/order``, DMA, position close, and the strategy engine)
calls :func:`assert_live_dispatch_allowed` before touching a broker, so a
connected broker can never receive a real order by accident in staging/dev.
"""

from __future__ import annotations

from typing import Optional

from app.brokers.angelone import AngelOneBroker
from app.brokers.base import BrokerClient
from app.brokers.binance import BinanceBroker
from app.brokers.simulated import SimulatedBroker
from app.brokers.upstox import UpstoxBroker
from app.brokers.zerodha import ZerodhaKiteBroker
from app.models.broker_account import BrokerAccountRecord


class BrokerModeBlockedError(RuntimeError):
    """Raised when LIVE broker order dispatch is attempted while
    ``BROKER_MODE!=live``.  This is a fail-safe: a connected broker must never
    receive a real order unless the deployment is explicitly in live mode.
    """


def live_dispatch_allowed() -> bool:
    """True only when the deployment is explicitly configured for live trading.

    ``settings.broker_mode`` defaults to ``"simulated"`` and must be flipped to
    ``"live"`` deliberately (see ``app/config.py``).  Both ``"live"`` and
    ``"paper"`` are treated as non-blocking here so the guard applies only to
    the real-money path.
    """
    from app.config import settings

    return settings.broker_mode == "live"


def assert_live_dispatch_allowed() -> None:
    """Fail-fast guard for LIVE order placement.

    Raises :class:`BrokerModeBlockedError` when a caller attempts to place a
    real broker order while the deployment is in simulated mode.  This prevents
    a connected-but-unintended broker from receiving live orders in staging or
    dev environments.
    """
    if not live_dispatch_allowed():
        raise BrokerModeBlockedError(
            "LIVE broker order dispatch blocked: BROKER_MODE is not 'live'. "
            "Set BROKER_MODE=live explicitly to allow real-money orders."
        )


def get_broker_adapter(broker_rec: Optional[BrokerAccountRecord] = None) -> BrokerClient:
    """Instantiate appropriate broker adapter with decrypted credentials from BrokerAccountRecord."""
    if not broker_rec:
        return SimulatedBroker()

    b_name = (broker_rec.broker_name or "SIMULATED").upper()
    if b_name == "ZERODHA":
        return ZerodhaKiteBroker(
            api_key=broker_rec.get_api_key(),
            api_secret=broker_rec.get_api_secret(),
            access_token=broker_rec.get_access_token(),
        )
    elif b_name == "UPSTOX":
        return UpstoxBroker(
            api_key=broker_rec.get_api_key(),
            api_secret=broker_rec.get_api_secret(),
            access_token=broker_rec.get_access_token(),
        )
    elif b_name == "ANGEL_ONE":
        return AngelOneBroker(
            api_key=broker_rec.get_api_key(),
            client_id=broker_rec.client_id,
            pin=broker_rec.get_api_secret(),
            totp_key=broker_rec.get_totp_secret(),
            jwt_token=broker_rec.get_access_token(),
        )
    elif b_name == "BINANCE":
        return BinanceBroker(
            api_key=broker_rec.get_api_key(),
            api_secret=broker_rec.get_api_secret(),
        )
    return SimulatedBroker()


__all__ = [
    "AngelOneBroker",
    "BinanceBroker",
    "BrokerClient",
    "BrokerModeBlockedError",
    "SimulatedBroker",
    "UpstoxBroker",
    "ZerodhaKiteBroker",
    "assert_live_dispatch_allowed",
    "get_broker_adapter",
    "live_dispatch_allowed",
]
