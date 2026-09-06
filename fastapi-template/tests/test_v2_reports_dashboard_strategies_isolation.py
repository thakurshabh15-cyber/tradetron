"""V2 regression tests: anonymous cross-tenant data disclosure on reports/dashboard/strategies.

Target V2: '/api/reports/*', '/api/dashboard/summary', '/api/strategies'.

These tests pin the MINIMAL SAFE REMEDIATION:

  1. Every private reports endpoint rejects anonymous callers with 401.
  2. Strategy CRUD endpoints (list/get/create/publish) reject anonymous callers.
  3. Authenticated User A can never see User B's trades/strategies through any
     reports, export, dashboard-summary or strategy-listing endpoint.
  4. A client-supplied ``user_id`` (query param) can never widen or re-target
     the server-derived scope.
  5. CSV/JSON exports can never contain another tenant's records.
  6. Aggregate PnL/counts returned to an authenticated caller are strictly
     tenant-scoped (they match ONLY that user's own trade rows).
  7. Strategy listing/getting/publishing never exposes another user's
     configurations, and create always stamps server-derived ownership.
  8. Intended public behavior is preserved: ``GET /api/strategies/marketplace``
     stays anonymous-accessible, and the anonymous guest dashboard summary
     (landing page!) keeps working unchanged, while the authenticated
     dashboard summary becomes tenant-scoped.

No real broker/payment/network calls: everything runs against the local
SQLite test database with BROKER_MODE=simulated.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid

from fastapi.testclient import TestClient

from app.config import settings
from app.core.security import create_access_token
from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.trading import StrategyRecord, TradeRecord
from app.models.user import UserRecord

settings.broker_mode = "simulated"


def _token(user_id: str, role: str = "trader") -> str:
    return create_access_token({"sub": user_id, "email": f"{user_id}@v2test.io", "role": role})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _new_user(tag: str, role: str = "trader") -> UserRecord:
    uid = str(uuid.uuid4())
    return UserRecord(
        id=uid,
        email=f"{tag}-{uid[:8]}@v2test.io",
        hashed_password="x",
        full_name=f"V2 {tag}",
        role=role,
        is_active=True,
        is_verified=True,
        paper_balance=1_000_000.0,
    )


def _seed_scenario(client: TestClient):
    """Create users A and B with distinct trades and strategies.

    Returns a dict with uids, tokens, trade/strategy ids so every assertion
    can reference exactly which rows belong to which tenant.  Fresh UUIDs per
    call keep the shared test database idempotent.
    """

    A_TRADES = [
        {"order_id": "VA_ORD_WIN_1", "symbol": "V2SYMA", "side": "BUY", "quantity": 25, "price": 2480.50, "pnl": 250.0},
        {"order_id": "VA_ORD_LOSS_1", "symbol": "V2SYMB", "side": "SELL", "quantity": 50, "price": 510.25, "pnl": -80.0},
    ]
    B_TRADES = [
        {"order_id": "VB_ORD_SECRET_1", "symbol": "V2SECRET", "side": "BUY", "quantity": 100, "price": 999.99, "pnl": 9999.0},
    ]
    A_STRAT_NAME = "Visible Algo A"
    B_STRAT_NAME = "Secret Strategy B"

    user_a = _new_user("va")
    user_b = _new_user("vb")

    def _strat(uid, name, symbol):
        sid = str(uuid.uuid4())
        return StrategyRecord(
            id=sid,
            user_id=uid,
            name=name,
            symbols_json=json.dumps([symbol]),
            conditions_json=json.dumps([{"indicator": "PRICE", "operator": "gt", "value": 100.0, "period": 14}]),
            action_json=json.dumps({"side": "BUY", "quantity": 10, "order_type": "MARKET"}),
            enabled=False,
            execution_mode="PAPER",
            capital_allocated=10000.0,
        ), sid

    strat_a, strat_a_id = _strat(user_a.id, A_STRAT_NAME, "AAPL")
    strat_b, strat_b_id = _strat(user_b.id, B_STRAT_NAME, "TSLA")

    def _trade(uid, td):
        tid = str(uuid.uuid4())
        return TradeRecord(
            id=tid,
            user_id=uid,
            order_id=td["order_id"],
            strategy_name=None,
            symbol=td["symbol"],
            side=td["side"],
            quantity=td["quantity"],
            price=td["price"],
            pnl=td["pnl"],
        ), tid

    seed_rows = [strat_a, strat_b]
    seed_rows += [_trade(user_a.id, td)[0] for td in A_TRADES]
    seed_rows += [_trade(user_b.id, td)[0] for td in B_TRADES]

    async def _seed():
        await init_db()
        async with SessionLocal() as session:
            session.add_all([user_a, user_b, *seed_rows])
            await session.commit()

    asyncio.run(_seed())

    return {
        "uid_a": user_a.id,
        "uid_b": user_b.id,
        "tok_a": _token(user_a.id),
        "tok_b": _token(user_b.id),
        "strat_a_id": strat_a_id,
        "strat_b_id": strat_b_id,
        "strat_a_name": A_STRAT_NAME,
        "strat_b_name": B_STRAT_NAME,
        "order_ids_a": {td["order_id"] for td in A_TRADES},
        "order_ids_b": {td["order_id"] for td in B_TRADES},
        "symbols_a": {td["symbol"] for td in A_TRADES},
        "symbols_b": {td["symbol"] for td in B_TRADES},
        "pnl_a": sum(td["pnl"] for td in A_TRADES),
        "count_a": len(A_TRADES),
    }


# ── 1. Anonymous access is rejected on private endpoints ─────────────────────


def test_anonymous_reports_rejected_401():
    client = TestClient(app)
    for path in (
        "/api/reports/performance",
        "/api/reports/trades/summary",
        "/api/reports/export?format=csv",
        "/api/reports/export?format=json",
    ):
        res = client.get(path)
        assert res.status_code == 401, f"{path} leaked to anonymous: {res.status_code}"


def test_anonymous_strategies_rejected_401():
    client = TestClient(app)
    res = client.get("/api/strategies/marketplace")
    assert res.status_code == 200, "public marketplace catalog must stay public"
    # pick a real strategy id to prove even id-targeted anonymous reads are blocked
    item = res.json()["items"][0]

    assert client.get("/api/strategies").status_code == 401
    assert client.get(f"/api/strategies/{item['id']}").status_code == 401
    assert (
        client.post(
            "/api/strategies",
            json={"name": "anon", "symbols": ["AAPL"], "conditions": [], "action": {"side": "BUY", "quantity": 1}},
        ).status_code
        == 401
    )
    assert client.post("/api/strategies/marketplace/publish", json={"strategy_id": item["id"]}).status_code == 401


def test_anonymous_spoofed_user_id_still_401():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    res = client.get("/api/reports/performance", params={"user_id": ctx["uid_b"]})
    assert res.status_code == 401
    res = client.get("/api/reports/trades/summary", params={"user_id": ctx["uid_b"]})
    assert res.status_code == 401
    res = client.get("/api/reports/export", params={"user_id": ctx["uid_b"], "format": "json"})
    assert res.status_code == 401
    res = client.get("/api/strategies", params={"user_id": ctx["uid_b"]})
    assert res.status_code == 401


# ── 2. Tenant scoping for authenticated callers ──────────────────────────────


def test_performance_report_is_tenant_scoped():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)
    res = client.get("/api/reports/performance", headers=_auth(ctx["tok_a"]))
    assert res.status_code == 200, res.text
    data = res.json()

    summary = data["summary"]
    assert summary["total_trades"] == ctx["count_a"] == 2
    assert float(summary["total_realized_pnl"]) == ctx["pnl_a"] == 170.0
    # A's aggregate must NOT contain B's pnl
    assert float(summary["total_realized_pnl"]) != 170.0 + 9999.0

    sym_names = {s["symbol"] for s in data["symbol_breakdown"]}
    assert ctx["symbols_a"] <= sym_names
    assert not (ctx["symbols_b"] & sym_names), "B's symbol leaked into A's performance report"

    # B sees only B's own aggregates
    res_b = client.get("/api/reports/performance", headers=_auth(ctx["tok_b"]))
    summary_b = res_b.json()["summary"]
    assert summary_b["total_trades"] == 1
    assert float(summary_b["total_realized_pnl"]) == 9999.0
    sym_b = {s["symbol"] for s in res_b.json()["symbol_breakdown"]}
    assert "V2SECRET" in sym_b
    assert not (ctx["symbols_a"] & sym_b)


def test_trades_summary_report_is_tenant_scoped():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    res = client.get("/api/reports/trades/summary", headers=_auth(ctx["tok_a"]))
    assert res.status_code == 200
    data = res.json()
    assert data["total_trades"] == ctx["count_a"] == 2
    assert data["buy_count"] == 1
    assert data["sell_count"] == 1
    # B's huge volume must not be part of A's aggregate
    assert float(data["total_volume_usd"]) < 500_000.0

    res_b = client.get("/api/reports/trades/summary", headers=_auth(ctx["tok_b"]))
    assert res_b.json()["total_trades"] == 1


def test_export_cannot_leak_other_tenant():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    # CSV
    res = client.get("/api/reports/export?format=csv", headers=_auth(ctx["tok_a"]))
    assert res.status_code == 200
    assert "text/csv" in res.headers.get("content-type", "")
    rows = list(csv.DictReader(io.StringIO(res.text)))
    csv_order_ids = {r["Order ID"] for r in rows}
    assert ctx["order_ids_a"] <= csv_order_ids
    assert not (ctx["order_ids_b"] & csv_order_ids), "B's orders leaked into A's CSV export"

    # JSON
    res_json = client.get("/api/reports/export?format=json", headers=_auth(ctx["tok_a"]))
    assert res_json.status_code == 200
    items = res_json.json()
    json_order_ids = {it["order_id"] for it in items}
    assert ctx["order_ids_a"] <= json_order_ids
    assert not (ctx["order_ids_b"] & json_order_ids), "B's orders leaked into A's JSON export"
    # B's export shows B's records and never A's
    res_b = client.get("/api/reports/export?format=json", headers=_auth(ctx["tok_b"]))
    items_b = res_b.json()
    assert {it["order_id"] for it in items_b} == ctx["order_ids_b"]


def test_spoofed_user_id_param_cannot_widen_authenticated_scope():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    res = client.get(
        "/api/reports/performance",
        params={"user_id": ctx["uid_b"]},
        headers=_auth(ctx["tok_a"]),
    )
    assert res.status_code == 200
    summary = res.json()["summary"]
    assert summary["total_trades"] == ctx["count_a"] == 2
    assert float(summary["total_realized_pnl"]) == ctx["pnl_a"]

    res_exp = client.get(
        "/api/reports/export",
        params={"user_id": ctx["uid_b"], "format": "json"},
        headers=_auth(ctx["tok_a"]),
    )
    items = res_exp.json()
    assert not (ctx["order_ids_b"] & {it["order_id"] for it in items})


# ── 3. Strategy isolation ────────────────────────────────────────────────────


def test_strategy_listing_cannot_expose_other_users():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    res = client.get("/api/strategies", headers=_auth(ctx["tok_a"]))
    assert res.status_code == 200
    names = [s["name"] for s in res.json()]
    assert ctx["strat_a_name"] in names
    assert ctx["strat_b_name"] not in names, "B's strategy config leaked into A's strategy list"

    res_b = client.get("/api/strategies", headers=_auth(ctx["tok_b"]))
    names_b = [s["name"] for s in res_b.json()]
    assert ctx["strat_b_name"] in names_b
    assert ctx["strat_a_name"] not in names_b


def test_strategy_get_owner_only():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    ok = client.get(f"/api/strategies/{ctx['strat_a_id']}", headers=_auth(ctx["tok_a"]))
    assert ok.status_code == 200
    assert ok.json()["name"] == ctx["strat_a_name"]

    forbidden = client.get(f"/api/strategies/{ctx['strat_b_id']}", headers=_auth(ctx["tok_a"]))
    assert forbidden.status_code == 404, "A must not read B's strategy configuration"

    anon = client.get(f"/api/strategies/{ctx['strat_a_id']}")
    assert anon.status_code == 401


def test_strategy_create_uses_server_derived_ownership():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    payload = {
        "name": "Freshly Created By A",
        "symbols": ["NIFTY50"],
        "conditions": [{"indicator": "PRICE", "operator": "gt", "value": 0.0, "period": 14}],
        "action": {"side": "BUY", "quantity": 10, "order_type": "MARKET"},
        "enabled": False,
    }
    res = client.post("/api/strategies", json=payload, headers=_auth(ctx["tok_a"]))
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["name"] == payload["name"]

    # The record must be stamped with A's server-derived id in the DB
    async def _check_owner():
        async with SessionLocal() as session:
            row = await session.get(StrategyRecord, created["id"])
            return row.user_id if row else None

    owner = asyncio.run(_check_owner())
    assert owner == ctx["uid_a"]

    # B cannot see or read it
    res_b_list = client.get("/api/strategies", headers=_auth(ctx["tok_b"]))
    assert created["name"] not in [s["name"] for s in res_b_list.json()]
    assert client.get(f"/api/strategies/{created['id']}", headers=_auth(ctx["tok_b"])).status_code == 404


def test_marketplace_publish_requires_auth_and_ownership():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    # anonymous -> 401
    res = client.post("/api/strategies/marketplace/publish", json={"strategy_id": ctx["strat_a_id"]})
    assert res.status_code == 401

    # B cannot publish A's strategy -> 403
    res = client.post(
        "/api/strategies/marketplace/publish",
        json={"strategy_id": ctx["strat_a_id"], "creator_name": "Sneaky B"},
        headers=_auth(ctx["tok_b"]),
    )
    assert res.status_code == 403, res.text

    # B CAN publish B's own strategy -> 200
    res = client.post(
        "/api/strategies/marketplace/publish",
        json={"strategy_id": ctx["strat_b_id"], "creator_name": "Honest B", "category": "Momentum"},
        headers=_auth(ctx["tok_b"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["success"] is True


# ── 4. Dashboard summary: tenant-scoped when authenticated, public for guests ─


def test_dashboard_summary_tenant_scoped_when_authenticated():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    res = client.get("/api/dashboard/summary", headers=_auth(ctx["tok_a"]))
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["totalTrades"] == ctx["count_a"] == 2
    assert float(data["totalRealizedPnl"]) == ctx["pnl_a"] == 170.0
    top_names = {s["name"] for s in data["topStrategies"]}
    assert ctx["strat_b_name"] not in top_names, "B's strategy leaked into A's dashboard summary"
    assert "engineStatus" in data
    assert "pendingTasks" in data


def test_dashboard_summary_guest_public_contract_preserved():
    """The landing page fetches /api/dashboard/summary with publicFetch
    (explicit ``{ public: true }`` in Dashboard.jsx); anonymous guests must
    keep receiving the demo summary (this is the intentional public feature)."""
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    res = client.get("/api/dashboard/summary")
    assert res.status_code == 200, res.text
    data = res.json()
    for key in ("weekReturn", "monthReturn", "topStrategies", "pendingTasks", "engineStatus"):
        assert key in data
    assert isinstance(data["topStrategies"], list)
    assert isinstance(data["pendingTasks"], list)


# ── 5. Intended private flows remain fully functional for the owner ──────────


def test_legitimate_owner_flows_intact():
    ctx = _seed_scenario(TestClient(app))
    client = TestClient(app)

    # reports all still work for the owner
    assert client.get("/api/reports/performance", headers=_auth(ctx["tok_a"])).status_code == 200
    assert client.get("/api/reports/trades/summary", headers=_auth(ctx["tok_a"])).status_code == 200
    assert client.get("/api/reports/export?format=csv", headers=_auth(ctx["tok_a"])).status_code == 200
    assert client.get("/api/strategies", headers=_auth(ctx["tok_a"])).status_code == 200
    assert client.get(f"/api/strategies/{ctx['strat_a_id']}", headers=_auth(ctx["tok_a"])).status_code == 200

    # marketplace catalog remains anonymous (public product feature)
    assert client.get("/api/strategies/marketplace").status_code == 200