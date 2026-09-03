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
   DB-bookkeeping only: it persists FILLED Order/Trade/Position records
   directly to the database and NEVER calls a broker's ``place_order`` — even
   when ``mode == LIVE``. So it cannot touch a real exchange.

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

def test_copy_trading_module_has_no_broker_dispatch_calls():
    """copy_trading.py must contain no broker execution ``place_order`` call.

    It persists FILLED Order/Trade/Position records directly to the DB and
    never calls a real or simulated broker.
    """
    src = (REPO_ROOT / "app" / "engine" / "copy_trading.py").read_text(encoding="utf-8")
    assert ".place_order(" not in src, "copy_trading must never call broker.place_order"
    # No broker *object* may be referenced in execution code: reject any code
    # usage of a broker instance/type (docstrings/comments may mention the word
    # "broker" — e.g. "Live broker execution modes" — so match code patterns).
    code_broker_refs = re.findall(
        r"\b(broker\s*[.=:(]|Broker\s*\(|from app\.brokers|import .*Broker)\b", src
    )
    assert not code_broker_refs, (
        f"copy_trading must not reference a broker execution object: {code_broker_refs}"
    )
    # Orders are recorded as FILLED directly — bookkeeping, not execution.
    assert 'status="FILLED"' in src or "status='FILLED'" in src


def test_copy_trading_engine_holds_no_broker(monkeypatch):
    """CopyTradingEngine must have no broker attribute to forward orders to."""
    from app.engine.copy_trading import CopyTradingEngine

    engine = CopyTradingEngine()
    assert not hasattr(engine, "broker"), "engine must not carry a broker"
    assert not hasattr(engine, "_broker")
    assert not hasattr(engine, "order_manager")


def test_copy_trading_executor_persists_filled_without_a_broker(monkeypatch):
    """The per-follower executor writes FILLED DB records, never dispatches.

    ``_execute_single_follower_order`` is the innermost execution step — the one
    place a broker call WOULD live if copy trading dispatched to a real broker.
    We drive it directly with a LIVE-mode follower and a capture session,
    proving it persists FILLED Order/Trade/Position records and does so without
    touching any broker (no broker exists on the engine).
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
            return None  # no follower_row -> stats update skipped

        async def flush(self):
            return None

    class _Scalar:
        def scalars(self):
            return self

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
    # notify_trade_fill is imported locally inside the executor — patch its source.
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
    assert outcome.get("success", False) is True, outcome

    # The executor must have persisted a FILLED order + a trade + an open position.
    orders = [r for r in session.added if isinstance(r, OrderRecord)]
    trades = [r for r in session.added if isinstance(r, TradeRecord)]
    positions = [r for r in session.added if isinstance(r, PositionRecord)]
    assert orders, "expected at least one OrderRecord"
    assert all(o.status == "FILLED" for o in orders), "order must be FILLED"
    assert trades, "expected a TradeRecord"
    assert positions, "expected an open PositionRecord"
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
        f"Called from:\n{occurrences}"
    )
    assert "visual_strategy.py" in occurrences[0] and "def execute_legs" in occurrences[0]


def test_visual_strategy_engine_singleton_is_unused():
    """The global ``visual_strategy_engine`` singleton must have no users."""
    occurrences = _grep("visual_strategy_engine")
    # Only its definition line is expected.
    assert len(occurrences) == 1, (
        "visual_strategy_engine must be unused (only its definition exists). "
        f"Found:\n{occurrences}"
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
