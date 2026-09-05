"""Phase 3 — Staging Verification: Broker adapter offline harness.

Tests SimulatedBroker, and verifies that all live brokers (Zerodha,
Upstox, Binance, AngelOne) refuse to operate when credentials are
missing — guaranteeing no accidental money exposure.
"""

from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


class TestBrokerSimulated:
    """SimulatedBroker must fully work offline — paper-trading path."""

    def test_place_fill_and_positions(self):
        from app.brokers.simulated import SimulatedBroker
        from app.schemas.trading import OrderRequest, Side

        async def go():
            b = SimulatedBroker()
            await b.connect()
            b.update_price("AAPL", 150.0)
            fill = await b.place_order(
                OrderRequest(symbol="AAPL", side=Side.BUY, quantity=10)
            )
            assert fill["status"] == "FILLED"
            assert fill["filled_price"] == 150.0
            pos = await b.get_positions()
            assert len(pos) == 1 and pos[0]["quantity"] == 10
            h = await b.get_holdings()
            assert h[0]["tradingsymbol"] == "AAPL"
            c = await b.cancel_order("SIM-T1")
            assert c["status"] == "CANCELLED"

        _run(go())

    def test_sell_closes_position(self):
        from app.brokers.simulated import SimulatedBroker
        from app.schemas.trading import OrderRequest, Side

        async def go():
            b = SimulatedBroker()
            await b.connect()
            b.update_price("TSLA", 200.0)
            await b.place_order(OrderRequest(symbol="TSLA", side=Side.BUY, quantity=5))
            await b.place_order(OrderRequest(symbol="TSLA", side=Side.SELL, quantity=5))
            assert await b.get_positions() == []

        _run(go())

    def test_virtual_margins_positive(self):
        from app.brokers.simulated import SimulatedBroker

        async def go():
            b = SimulatedBroker()
            m = await b.get_margins()
            assert m["available_cash"] > 0 and m["utilized_margin"] >= 0

        _run(go())


class TestBrokerMissingCredentials:
    """Live brokers refuse to connect outside BROKER_MODE=live, and even inside
    a permitted live mode missing credentials still fail closed.  Nothing is
    reachable in simulated mode and no money can ever be exposed."""

    def test_zerodha_connect_blocked_in_simulated_mode(self, monkeypatch):
        """BROKER_MODE is the authoritative safety invariant: connect() is a
        live-broker connectivity operation and is blocked in simulated mode
        before any credential/SDK work."""
        from app.brokers import BrokerModeBlockedError
        from app.brokers.zerodha import ZerodhaKiteBroker

        monkeypatch.setattr("app.config.settings.broker_mode", "simulated")
        async def go():
            with pytest.raises(BrokerModeBlockedError, match="BROKER_MODE"):
                await ZerodhaKiteBroker(api_key="", api_secret="").connect()

        _run(go())

    def test_zerodha_missing_creds_in_live_mode(self, monkeypatch):
        """With BROKER_MODE=live the credential precondition still surfaces —
        the guard must not mask legitimate missing-credential errors."""
        from app.brokers.zerodha import ZerodhaKiteBroker

        monkeypatch.setattr("app.config.settings.broker_mode", "live")
        async def go():
            with pytest.raises(RuntimeError) as ei:
                await ZerodhaKiteBroker(api_key="", api_secret="").connect()
            assert "credential" in str(ei.value).lower() or "not configured" in str(ei.value).lower()

        _run(go())

    def test_upstox_missing_creds(self):
        from app.brokers.upstox import UpstoxBroker

        async def go():
            with pytest.raises(RuntimeError):
                await UpstoxBroker(api_key="", api_secret="").connect()

        _run(go())

    def test_binance_missing_creds(self):
        from app.brokers.binance import BinanceBroker

        async def go():
            with pytest.raises(RuntimeError) as ei:
                await BinanceBroker(api_key="", api_secret="")._api_request(
                    "GET", "/sapi/v1/account", signed=True
                )
            assert "credentials" in str(ei.value).lower()

        _run(go())

    def test_angelone_invalid_key(self):
        from app.brokers.angelone import AngelOneBroker

        async def go():
            ok, msg = await AngelOneBroker(api_key="test", client_id="X", pin="123").validate_credentials()
            assert ok is False

        _run(go())
