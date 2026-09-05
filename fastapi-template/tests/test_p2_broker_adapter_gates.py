"""P2-10 defense-in-depth: adapter-level live-dispatch gates.

Regression tests proving that every real-broker adapter blocks order dispatch
at its own method/network boundary when ``BROKER_MODE != live``:

- ``AngelOneBroker.place_order`` / ``ZerodhaKiteBroker.place_order`` /
  ``UpstoxBroker.place_order`` raise ``BrokerModeBlockedError`` before any
  SDK/network work (so even direct, unguarded class-level calls are safe).
- ``BinanceBroker`` blocks on the network boundary (``_api_request``), which
  also covers order placement / cancels; the credential precondition still
  surfaces ahead of the gate so missing-creds errors keep their message.
- In ``live`` mode the gates pass through (next precondition fires instead).
"""

from __future__ import annotations

import asyncio

import pytest

from app.brokers import BrokerModeBlockedError
from app.brokers.angelone import AngelOneBroker
from app.brokers.binance import BinanceBroker
from app.brokers.upstox import UpstoxBroker
from app.brokers.zerodha import ZerodhaKiteBroker
from app.config import settings
from app.schemas.trading import OrderRequest, Side


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


ANGEL = dict(api_key="k", client_id="c", pin="p", totp_key="t")
ZERODHA = dict(api_key="k", api_secret="s", access_token="t")
UPSTOX = dict(access_token="t")


def _market_order() -> OrderRequest:
    return OrderRequest(symbol="RELIANCE", side=Side.BUY, quantity=1)


@pytest.mark.parametrize(
    "broker_cls,kwargs",
    [
        (AngelOneBroker, ANGEL),
        (ZerodhaKiteBroker, ZERODHA),
        (UpstoxBroker, UPSTOX),
    ],
)
@pytest.mark.asyncio
async def test_rest_broker_place_order_blocked_in_simulated_mode(broker_cls, kwargs, monkeypatch):
    """Direct class-level place_order is hard-blocked in simulated mode.

    A plain RuntimeError (e.g. "package not installed") would mean the gate was
    skipped and ``connect()`` was reached first — so BrokerModeBlockedError is
    the exact assertion that the guard fires before any SDK/network work.
    """
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = broker_cls(**kwargs)
    with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
        await broker.place_order(_market_order())


@pytest.mark.asyncio
async def test_binance_network_boundary_blocked_in_simulated_mode(monkeypatch):
    """Every real Binance API call is blocked while BROKER_MODE != live."""
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = BinanceBroker(api_key="k", api_secret="s", testnet=True)
    with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
        await broker._api_request("GET", "/api/v3/ping", signed=False)


@pytest.mark.asyncio
async def test_binance_place_order_blocked_in_simulated_mode(monkeypatch):
    """place_order funnels through the gated network boundary."""
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = BinanceBroker(api_key="k", api_secret="s", testnet=True)
    with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
        await broker.place_order(_market_order())


@pytest.mark.asyncio
async def test_binance_gate_preserves_missing_credentials_error(monkeypatch):
    """Credential precondition is checked before the mode gate (message kept)."""
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = BinanceBroker(api_key="", api_secret="", testnet=True)
    with pytest.raises(RuntimeError, match="credentials"):
        await broker._api_request("GET", "/api/v3/ping", signed=False)


@pytest.mark.asyncio
async def test_binance_gate_allows_live_mode(monkeypatch):
    """In live mode the guard passes; the next precondition (creds) fails."""
    monkeypatch.setattr(settings, "broker_mode", "live")
    broker = BinanceBroker(api_key="", api_secret="", testnet=True)
    with pytest.raises(RuntimeError, match="credentials") as ei:
        await broker._api_request("GET", "/api/v3/ping", signed=False)
    assert not isinstance(ei.value, BrokerModeBlockedError)


@pytest.mark.asyncio
async def test_rest_broker_gate_allows_live_mode(monkeypatch):
    """In live mode the guard passes and connect() is actually reached.

    ``connect()`` is stubbed to fail without any network traffic. If the guard
    were still blocking, ``BrokerModeBlockedError`` would fire and ``connect()``
    would never be called — so the await_count is the decisive assertion.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, "broker_mode", "live")
    for broker in (
        AngelOneBroker(**ANGEL),
        ZerodhaKiteBroker(**ZERODHA),
    ):
        connect_mock = AsyncMock(side_effect=RuntimeError("stub connect reached"))
        broker.connect = connect_mock  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="stub connect reached") as ei:
            await broker.place_order(_market_order())
        assert not isinstance(ei.value, BrokerModeBlockedError)
        assert connect_mock.await_count == 1


# ── BROKER_MODE hardening: connect() is a LIVE-connectivity gate ─────────────
# ``connect()`` establishes/validates a real broker session (SmartAPI login,
# Kite profile check) — a network operation that must be impossible while
# BROKER_MODE != live, regardless of how the adapter instance was obtained
# (startup, portfolio reads, engine, cron).

@pytest.mark.asyncio
async def test_angelone_connect_blocked_in_simulated_mode(monkeypatch):
    """Simulated mode: connect() is hard-blocked before any SDK construction or
    network work, and constructing the adapter itself creates no SDK object."""
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = AngelOneBroker(**ANGEL)
    assert broker._client is None  # no SmartConnect object at construction
    with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
        await broker.connect()
    assert broker._client is None  # still no SDK object after the block


@pytest.mark.asyncio
async def test_zerodha_connect_blocked_in_simulated_mode(monkeypatch):
    """Simulated mode: connect() is hard-blocked before any SDK construction or
    network work, and constructing the adapter itself creates no SDK object."""
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = ZerodhaKiteBroker(**ZERODHA)
    assert broker._kite is None  # no KiteConnect object at construction
    with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
        await broker.connect()
    assert broker._kite is None  # still no SDK object after the block


@pytest.mark.asyncio
async def test_angelone_connect_reaches_login_in_live_mode(monkeypatch):
    """Live mode: the connect guard passes and the normal login preconditions
    surface — proving the gate does not break legitimate live connectivity.

    SmartApi may or may not be installed in the active environment (it is now
    installed in this venv), so a stub SDK is injected to deterministically
    reach the login boundary without touching the broker network.  The
    decisive assertion is that the surfaced error is NOT a
    BrokerModeBlockedError (i.e. the mode gate passed).
    """
    import app.brokers.angelone as angelone_mod

    class _StubSDK:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def generateSession(self, client_id: str, pin: str, totp: str):
            raise RuntimeError("stub SmartConnect.generateSession reached")

    monkeypatch.setattr(settings, "broker_mode", "live")
    monkeypatch.setattr(angelone_mod, "SmartConnect", _StubSDK)
    broker = AngelOneBroker(**{**ANGEL, "totp_key": "JBSWY3DPEHPK3PXP"})
    with pytest.raises(RuntimeError, match="stub SmartConnect.generateSession reached") as ei:
        await broker.connect()
    assert not isinstance(ei.value, BrokerModeBlockedError)


@pytest.mark.asyncio
async def test_zerodha_connect_reaches_precondition_in_live_mode(monkeypatch):
    """Live mode: the connect guard passes and missing credentials surface
    their original error (no fabricated success, no SDK/network work)."""
    monkeypatch.setattr(settings, "broker_mode", "live")
    broker = ZerodhaKiteBroker(api_key="", api_secret="", access_token="t")
    with pytest.raises(RuntimeError, match="credentials") as ei:
        await broker.connect()
    assert not isinstance(ei.value, BrokerModeBlockedError)


# ── BROKER_MODE hardening: validate_credentials() is live login connectivity ─
# ``AngelOneBroker.validate_credentials`` performs a REAL SmartAPI login
# (``getProfile`` / ``generateSession``) against the live broker and is
# reachable from ``POST /api/brokers/accounts/manual``.  It is therefore a
# live-connectivity operation and must be hard-blocked in simulated mode
# before any SDK construction or network work.

ANGEL_VALID_CREDS = dict(
    api_key="valid_angel_api_key_12345678",
    client_id="S123456",
    pin="secret",
)


@pytest.mark.asyncio
async def test_angelone_validate_credentials_blocked_in_simulated_mode(monkeypatch):
    """Simulated mode: a real SmartAPI credential validation must be
    hard-blocked before any SDK construction or network work — even with
    well-formed credentials that would normally pass the local sanity checks."""
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = AngelOneBroker(**ANGEL_VALID_CREDS)
    assert broker._client is None  # no SmartConnect object at construction
    with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
        await broker.validate_credentials()
    assert broker._client is None  # still no SDK object after the block


@pytest.mark.asyncio
async def test_angelone_validate_credentials_reaches_sdk_in_live_mode(monkeypatch):
    """Live mode: the validate_credentials guard passes and the real login
    path is reached (proving the guard does not disable legitimate credential
    validation).  A stub SmartAPI SDK is injected so the test deterministically
    asserts the login boundary fires without touching the broker network."""
    import app.brokers.angelone as angelone_mod

    class _StubSDK:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def setAccessToken(self, token: str) -> None:
            ...

        def getProfile(self, jwt_token: str):
            raise RuntimeError("stub SmartConnect.getProfile reached")

    monkeypatch.setattr(settings, "broker_mode", "live")
    monkeypatch.setattr(angelone_mod, "SmartConnect", _StubSDK)
    broker = AngelOneBroker(**ANGEL_VALID_CREDS, jwt_token="jwt-token")
    with pytest.raises(RuntimeError, match="stub SmartConnect.getProfile reached") as ei:
        await broker.validate_credentials()
    assert not isinstance(ei.value, BrokerModeBlockedError)


# ── BROKER_MODE hardening: Upstox read/modify/status paths are gated too ────
# Previously only ``UpstoxBroker.place_order`` carried a dispatch gate; every
# other real Upstox call (``get_margins`` / ``get_holdings`` / ``get_positions``
# / ``modify_order`` / ``cancel_order`` / ``get_order_status``) funnels through
# ``connect()``.  ``connect()`` is now gated, making those API calls impossible
# while ``BROKER_MODE != live``.


@pytest.mark.asyncio
async def test_upstox_connect_blocked_in_simulated_mode(monkeypatch):
    """Simulated mode: Upstox connect() is the single choke point for every
    read/modify/cancel/status call and must be hard-blocked before any network
    work — even when a token is present."""
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = UpstoxBroker(access_token="t")
    with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
        await broker.connect()


@pytest.mark.asyncio
async def test_upstox_read_modify_paths_blocked_in_simulated_mode(monkeypatch):
    """Simulated mode: every real Upstox read/modify/cancel/status call is
    hard-blocked through the gated connect(), closing the gap where only
    place_order was gated."""
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    broker = UpstoxBroker(access_token="t")
    for coro in (
        broker.get_margins(),
        broker.get_holdings(),
        broker.get_positions(),
        broker.get_order_status("ORD-1"),
        broker.modify_order("ORD-1", quantity=5),
        broker.cancel_order("ORD-1"),
    ):
        with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
            await coro


@pytest.mark.asyncio
async def test_upstox_connect_reaches_precondition_in_live_mode(monkeypatch):
    """Live mode: the connect guard passes and the missing-token precondition
    surfaces its original RuntimeError (no fabricated success)."""
    monkeypatch.setattr(settings, "broker_mode", "live")
    broker = UpstoxBroker(api_key="", api_secret="", access_token=None)
    with pytest.raises(RuntimeError, match="access_token") as ei:
        await broker.connect()
    assert not isinstance(ei.value, BrokerModeBlockedError)