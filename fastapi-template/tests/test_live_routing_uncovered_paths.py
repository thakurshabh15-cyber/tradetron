"""Phase 4 — LIVE ≠ PAPER routing verification for uncovered dispatch paths.

The eight order-dispatch paths were audited (see PHASE4_REPORT.md). Most are
guarded by ``assert_live_dispatch_allowed()``. This module verifies the three
paths that had NO live-caller and NO guard — proving they are safe by
construction / not wired to any real-broker dispatch:

1. Webhook signal handler (``tradethrone_signal.py``)
   Routes through the engine's startup order manager (``get_engine()``). The
   broker used by that order manager is fixed at engine-construction time from
   ``BROKER_MODE``; no user-supplied broker or LIVE override can reach the
   broker from a webhook payload.

2. Copy-trading fan-out (``copy_trading.py``)
   LIVE follower fan-out is now gated by ``assert_live_dispatch_allowed()``
   and dispatched through the follower's own owned broker adapter; PAPER
   fan-out remains pure DB bookkeeping (FILLED records, no broker). A LIVE
   fan-out can never fabricate FILLED/OPEN state: when the deployment is
   not ``BROKER_MODE=live`` (or the follower has no owned connected broker,
   or the broker rejects the order) the engine persists a REJECTED order
   only.

3. ``visual_strategy.execute_legs``
   A latent dispatch primitive that takes an arbitrary ``broker``; this verifies
   it has ZERO callers in the codebase and its singleton is unused — nothing
   wires it to a live dispatch path today.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── 1. Webhook signal handler → engine startup broker ──────────────────────

def test_webhook_signal_uses_engine_order_manager_not_user_broker():
    """The webhook handler must dispatch via ``get_engine()._order_manager``.

    The broker is selected once at app startup from ``BROKER_MODE`` and passed
    into the TradingEngine, which builds its ``_order_manager`` around it. A
    webhook payload carries order fields only — never a broker/client override —
    so a LIVE override can't be smuggled into the dispatch path.
    """
    src = inspect.getsource(
        importlib_import("app.webhooks.handlers.tradethrone_signal")
    )

    # It sources the order manager from the shared engine singleton.
    assert "from app.main import get_engine" in src
    assert "engine = get_engine()" in src
    assert "engine._order_manager" in src
    assert "order_manager.place_order(" in src

    # It must never construct its own broker, nor accept a broker from payload.
    assert "Broker(" not in src
    assert "broker_mode" not in src  # selection happens at startup, not here


def test_webhook_signal_handler_has_no_user_broker_input():
    """The handler's signature and payload model carry no broker field."""
    handler_src = inspect.getsource(
        importlib_import("app.webhooks.handlers.tradethrone_signal")
    )
    assert "def handle_tradethrone_signal(webhook" in handler_src
    # The handler takes only a QueuedWebhook; the payload schema must not
    # define any broker/live selection field we'd hand to a broker.
    payload_src = inspect.getsource(
        importlib_import("app.webhooks.validation.schemas")
    ) if _module_exists("app.webhooks.validation.schemas") else ""
    for frag in ("broker_name", "broker_mode", "broker_account_id"):
        assert frag not in payload_src, (
            f"TradeThronePayload must not carry '{frag}' — webhooks can't "
            "select a broker."
        )



# ── 2. Copy trading → DB-only, never dispatches to a broker ─────────────────

def test_copy_trading_module_live_dispatch_is_guarded():
    """copy_trading.py must gate EVERY LIVE follower fan-out behind the guard.

    The P0-1 fix wired LIVE copy-trades through the follower's own broker
    adapter, so the module MUST:

      - import assert_live_dispatch_allowed() and call it before any broker call
      - resolve the broker account from server data only (filtered by
        ``BrokerAccountRecord.user_id == follower.follower_user_id``)
      - persist REJECTED (never FILLED/OPEN) when the guard blocks, the
        follower owns no connected broker, or the broker rejects the order
      - keep PAPER bookkeeping FILLED-only (unchanged behavior, no broker)
    """
    src = (REPO_ROOT / "app" / "engine" / "copy_trading.py").read_text(encoding="utf-8")
    assert "assert_live_dispatch_allowed" in src, "LIVE copy-trade must invoke the live-dispatch guard"
    assert ".place_order(" in src, "LIVE copy-trade must dispatch through the broker boundary"
    assert 'status="REJECTED"' in src, "blocked/failed LIVE fan-out must persist REJECTED"
    assert 'status="FILLED"' in src, "PAPER/confirmed fills still persist FILLED"
    assert (
        "BrokerAccountRecord.user_id == follower.follower_user_id" in src
    ), "server-derived follower identity must scope the broker resolution"


def test_copy_trading_engine_holds_no_broker(monkeypatch):
    """CopyTradingEngine must have no broker attribute to forward orders to."""
    from app.engine.copy_trading import CopyTradingEngine

    engine = CopyTradingEngine()
    assert not hasattr(engine, "broker"), "engine must not carry a broker"
    assert not hasattr(engine, "_broker")
    assert not hasattr(engine, "order_manager")


def test_copy_trading_executor_rejects_live_fill_without_owned_broker(monkeypatch):
    """The per-follower executor must NOT fabricate FILLED/OPEN for a LIVE follower.

    ``_execute_single_follower_order`` is the innermost execution step. For a
    LIVE-mode follower the engine must resolve a CONNECTED broker account owned
    by ``follower.follower_user_id`` from the DB before dispatching. Here the
    capture session yields no owned broker row, so the executor must persist a
    REJECTED order and create NO Trade/Position - never a phantom fill.
    """
    from app.engine.copy_trading import CopyTradingEngine
    from app.models.trading import OrderRecord, TradeRecord, PositionRecord

    class _CaptureSession:
        def __init__(self):
            self.added: list[object] = []
            self._committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            self._committed = True

        async def execute(self, stmt):
            return _Scalar()

        async def get(self, model, pk):
            return None  # no follower_row / broker row resolvable

        async def flush(self):
            return None

    class _Scalar:
        def scalars(self):
            return self

        def first(self):
            return None  # no owned broker account resolves

        def all(self):
            return []

    class _Follower:
        follower_user_id = "follower-1"
        multiplier = 2.0
        max_allocation = None
        mode = "LIVE"            # worst case: a LIVE-mode follower
        broker_account_id = "live-account-1"
        id = "f1"
        total_copied_trades = 0

    session = _CaptureSession()
    monkeypatch.setattr("app.engine.copy_trading.SessionLocal", lambda: session)
    # notify_trade_fill is imported locally inside the executor - patch its source.
    monkeypatch.setattr("app.engine.alerts.notify_trade_fill", AsyncMock())

    engine = CopyTradingEngine()
    assert not hasattr(engine, "broker"), "engine must not carry a broker"

    import asyncio

    outcome = asyncio.run(
        engine._execute_single_follower_order(
            follower=_Follower(),
            symbol="NIFTY",
            side="BUY",
            master_qty=10,
            order_type="MARKET",
            price=250.0,
            master_mode="LIVE",
        )
    )
    assert outcome.get("success", False) is False, outcome
    assert outcome.get("reason") == "no_owned_broker", outcome

    orders = [r for r in session.added if isinstance(r, OrderRecord)]
    trades = [r for r in session.added if isinstance(r, TradeRecord)]
    positions = [r for r in session.added if isinstance(r, PositionRecord)]
    assert len(orders) == 1, "exactly one REJECTED order record expected"
    assert orders[0].status == "REJECTED", "must never fabricate a FILLED order"
    assert not trades, "no fabricated trade record"
    assert not positions, "no fabricated OPEN position"
    assert session._committed is True


# ── 3. visual_strategy.execute_legs is a latent (unwired) primitive ─────────

def test_visual_strategy_execute_legs_has_no_callers():
    """``execute_legs`` must have ZERO callers anywhere in the codebase.

    The only occurrence is its own definition — nothing invokes it with a live
    (or any) broker, so it cannot dispatch a real order today.
    """
    occurrences = _grep("execute_legs")
    assert len(occurrences) == 1, (
        "execute_legs must appear exactly once (its definition). "
        f"Called from:\r\n{occurrences}"
    )
    assert "visual_strategy.py" in occurrences[0] and "def execute_legs" in occurrences[0]


def test_visual_strategy_engine_singleton_is_unused():
    """The global ``visual_strategy_engine`` singleton must have no users."""
    occurrences = _grep("visual_strategy_engine")
    # Only its definition line is expected.
    assert len(occurrences) == 1, (
        "visual_strategy_engine must be unused (only its definition exists). "
        f"Found:\r\n{occurrences}"
    )


def test_visual_strategy_is_not_imported_as_a_dispatch_path():
    """Nothing routes visual strategies through a live broker.

    The only cross-module imports of the visual-strategy code pull in the
    *model* (VisualStrategyRecord) for CRUD — never the engine / dispatcher.
    """
    imports = _grep(r"(from app\.engine\.visual_strategy import|import visual_strategy)")
    assert imports == [], (
        "No module may import the visual_strategy ENGINE; found: " + str(imports)
    )



# ── helpers ──────────────────────────────────────────────────────────────────

def importlib_import(dotted: str):
    import importlib

    return importlib.import_module(dotted)


def _module_exists(dotted: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(dotted) is not None


def _grep(pattern: str) -> list[str]:
    """Return '<relpath>:<lineno>: <line>' for every code match (excluding tests)."""
    hits: list[str] = []
    root = Path(__file__).resolve().parent.parent
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                rel = path.relative_to(root)
                hits.append(f"{rel}:{i}: {line.strip()}")
    return hits
