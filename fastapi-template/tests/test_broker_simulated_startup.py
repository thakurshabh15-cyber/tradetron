"""Simulated startup must never touch a real broker SDK.

Regression suite for the ``BROKER_MODE=simulated`` boot invariant:

- ``app.main.lifespan`` selects :class:`SimulatedBroker` and never instantiates
  (or even imports the constructors of) the Angel One / Zerodha adapters or
  their SDK objects — proven by exploding stand-ins for every real-broker name.
- Constructing the real adapters themselves is inert in simulated mode: no
  SmartConnect / KiteConnect object is created, no log-dir side effects and no
  credentials consumed for connectivity.  Only ``connect()`` — behind the
  live-connectivity guard — could ever reach the SDK.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.brokers.angelone import AngelOneBroker, SmartConnect
from app.brokers.simulated import SimulatedBroker
from app.brokers.zerodha import KiteConnect, ZerodhaKiteBroker
from app.config import settings
from app.main import app, get_engine


def test_simulated_lifespan_never_constructs_real_broker_sdks(monkeypatch):
    """Application boot under BROKER_MODE=simulated must only use
    SimulatedBroker — any reach into the real adapters or SDKs explodes."""

    class _ExplodingSDK:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "a real broker SDK was constructed during simulated startup"
            )

    class _ExplodingAngel(AngelOneBroker):
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "AngelOneBroker must never be constructed in simulated startup"
            )

    class _ExplodingZerodha(ZerodhaKiteBroker):
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "ZerodhaKiteBroker must never be constructed in simulated startup"
            )

    monkeypatch.setattr(settings, "broker_mode", "simulated")
    monkeypatch.setattr("app.brokers.angelone.SmartConnect", _ExplodingSDK)
    monkeypatch.setattr("app.brokers.zerodha.KiteConnect", _ExplodingSDK)
    monkeypatch.setattr("app.brokers.angelone.AngelOneBroker", _ExplodingAngel)
    monkeypatch.setattr("app.brokers.zerodha.ZerodhaKiteBroker", _ExplodingZerodha)

    with TestClient(app) as client:
        engine = get_engine()
        assert engine is not None
        assert isinstance(engine._broker, SimulatedBroker)
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["broker_mode"] == "simulated"


def test_real_adapter_construction_is_inert_in_simulated_mode(monkeypatch):
    """Constructing the real adapters in simulated mode creates NO SDK object
    — no SmartConnect / KiteConnect, no SDK log-dir side effects, no
    credentials consumed for connectivity.  Only a guarded connect() could
    ever build the SDK (and it is blocked in simulated mode)."""
    monkeypatch.setattr(settings, "broker_mode", "simulated")
    angel = AngelOneBroker(api_key="k", client_id="c", pin="p", totp_key="t")
    zerodha = ZerodhaKiteBroker(api_key="k", api_secret="s", access_token="t")
    assert angel._client is None
    assert zerodha._kite is None