"""V2-continuation regression tests: cross-tenant isolation for /api/watchlist and price alerts.

Target: '/api/watchlist', '/api/watchlist/alerts/*'.

Fresh audit result: reports/dashboard/strategies isolation is already shipped
(test_v2_reports_dashboard_strategies_isolation.py), but the same anonymous
cross-tenant exposure/mutation class remained LIVE on the watchlist + price
alert surface — the models are ``user_id``-scoped and the API never consulted
or stamped server-derived identity.

These tests pin the MINIMAL SAFE REMEDIATION:

1. Anonymous callers only ever read/write ``user_id IS NULL`` (demo) rows and
   can never see another tenant's watchlist items or price alerts.
2. Authenticated User A sees ONLY A's rows through GET /api/watchlist and
   GET /api/watchlist/alerts/list; User B's rows and the demo rows are never
   visible to A.
3. A client-supplied ``user_id`` (body field or query param) is never
   consulted; scope is always server-derived from the token's ``sub``.
4. Cross-tenant mutation is impossible: DELETE (watchlist + alerts) and
   PATCH (alert toggle) can only target the caller's own namespace.
5. Authenticated mutations stamp server-derived ownership on the row.
6. Intended public behavior is preserved: the anonymous demo dataset
   (guest watchlist page + seeded symbols) keeps working unchanged, so no
   frontend contract change is required.

No real broker/payment/network calls: everything runs against the local
SQLite test database with BROKER_MODE=simulated.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient

from app.config import settings
from app.core.security import create_access_token
from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.user import UserRecord
from app.models.watchlist import PriceAlertRecord, WatchlistRecord

settings.broker_mode = "simulated"


def _token(user_id: str, role: str = "trader") -> str:
    return create_access_token({"sub": user_id, "email": f"{user_id}@wltest.io", "role": role})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _new_user(tag: str) -> UserRecord:
    uid = str(uuid.uuid4())
    return UserRecord(
        id=uid,
        email=f"{tag}-{uid[:8]}@wltest.io",
        hashed_password="x",
        full_name=f"WL {tag}",
        role="trader",
        is_active=True,
        is_verified=True,
        paper_balance=1_000_000.0,
    )


def _seed_scenario():
    """Create users A and B with private watchlist rows/alerts plus demo (NULL) rows.

    Uses fresh UUIDs per call so the shared test database stays idempotent.
    Returns keyed ids/tokens so every assertion references exact rows.
    """
    user_a = _new_user("wa")
    user_b = _new_user("wb")
    sym_a = f"WLA_{uuid.uuid4().hex[:8]}"
    sym_b = f"WLB_{uuid.uuid4().hex[:8]}"
    sym_demo = f"WLDE_{uuid.uuid4().hex[:8]}"

    row_a = WatchlistRecord(id=str(uuid.uuid4()), user_id=user_a.id, symbol=sym_a, notes="private A")
    row_b = WatchlistRecord(id=str(uuid.uuid4()), user_id=user_b.id, symbol=sym_b, notes="private B")
    row_demo = WatchlistRecord(id=str(uuid.uuid4()), user_id=None, symbol=sym_demo, notes="demo")

    alert_a = PriceAlertRecord(
        id=str(uuid.uuid4()), user_id=user_a.id, symbol=sym_a,
        condition="ABOVE", target_price=111.0, is_active=True,
    )
    alert_b = PriceAlertRecord(
        id=str(uuid.uuid4()), user_id=user_b.id, symbol=sym_b,
        condition="ABOVE", target_price=222.0, is_active=True,
    )
    alert_demo = PriceAlertRecord(
        id=str(uuid.uuid4()), user_id=None, symbol=sym_demo,
        condition="ABOVE", target_price=333.0, is_active=True,
    )

    async def _seed() -> None:
        await init_db()
        async with SessionLocal() as session:
            session.add_all([user_a, user_b, row_a, row_b, row_demo, alert_a, alert_b, alert_demo])
            await session.commit()

    asyncio.run(_seed())

    return {
        "tok_a": _token(user_a.id),
        "tok_b": _token(user_b.id),
        "sym_a": sym_a, "sym_b": sym_b, "sym_demo": sym_demo,
        "row_a_id": row_a.id, "row_b_id": row_b.id, "row_demo_id": row_demo.id,
        "alert_a_id": alert_a.id, "alert_b_id": alert_b.id, "alert_demo_id": alert_demo.id,
    }


# ── 1. Read isolation ────────────────────────────────────────────────────────


def test_anonymous_watchlist_never_exposes_tenant_rows():
    ctx = _seed_scenario()
    client = TestClient(app)

    res = client.get("/api/watchlist")
    assert res.status_code == 200, res.text
    symbols = {it["symbol"] for it in res.json()}
    assert ctx["sym_demo"] in symbols, "demo dataset must stay visible to anonymous guests"
    assert ctx["sym_a"] not in symbols, "User A's private watchlist row leaked to anonymous GET"
    assert ctx["sym_b"] not in symbols, "User B's private watchlist row leaked to anonymous GET"


def test_anonymous_alert_list_never_exposes_tenant_rows():
    ctx = _seed_scenario()
    client = TestClient(app)

    res = client.get("/api/watchlist/alerts/list")
    assert res.status_code == 200, res.text
    ids = {a["id"] for a in res.json()}
    assert ctx["alert_demo_id"] in ids
    assert ctx["alert_a_id"] not in ids, "User A's private alert leaked to anonymous GET"
    assert ctx["alert_b_id"] not in ids, "User B's private alert leaked to anonymous GET"


def test_authenticated_watchlist_strictly_tenant_scoped():
    ctx = _seed_scenario()
    client = TestClient(app)

    res_a = client.get("/api/watchlist", headers=_auth(ctx["tok_a"]))
    assert res_a.status_code == 200, res_a.text
    syms_a = {it["symbol"] for it in res_a.json()}
    assert ctx["sym_a"] in syms_a
    assert ctx["sym_b"] not in syms_a, "User B's watchlist leaked into A's list"
    assert ctx["sym_demo"] not in syms_a, "demo dataset leaked into A's tenant view"

    res_b = client.get("/api/watchlist", headers=_auth(ctx["tok_b"]))
    syms_b = {it["symbol"] for it in res_b.json()}
    assert ctx["sym_b"] in syms_b
    assert ctx["sym_a"] not in syms_b, "User A's watchlist leaked into B's list"
    assert ctx["sym_demo"] not in syms_b


def test_authenticated_alert_list_strictly_tenant_scoped():
    ctx = _seed_scenario()
    client = TestClient(app)

    res_a = client.get("/api/watchlist/alerts/list", headers=_auth(ctx["tok_a"]))
    assert res_a.status_code == 200, res_a.text
    ids_a = {a["id"] for a in res_a.json()}
    assert ctx["alert_a_id"] in ids_a
    assert ctx["alert_b_id"] not in ids_a, "User B's alert leaked into A's alert list"
    assert ctx["alert_demo_id"] not in ids_a, "demo alert leaked into A's tenant view"


# ── 2. Server-derived identity / spoofed user_id ─────────────────────────────


def test_mutation_stamps_server_derived_owner_and_spoofed_user_id_is_ignored():
    ctx = _seed_scenario()
    client = TestClient(app)
    spooky_sym = f"WLSP_{uuid.uuid4().hex[:8]}"

    # Body carries a spoofed user_id — must be ignored in favour of the token sub.
    add = client.post(
        "/api/watchlist",
        json={"symbol": spooky_sym, "notes": "spoof attempt", "user_id": str(uuid.uuid4())},
        headers=_auth(ctx["tok_a"]),
    )
    assert add.status_code == 201, add.text

    # A sees it; anonymous guest does NOT (it was stamped to A's namespace).
    symbols_a = {it["symbol"] for it in client.get("/api/watchlist", headers=_auth(ctx["tok_a"])).json()}
    assert spooky_sym.upper() in symbols_a
    symbols_anon = {it["symbol"] for it in client.get("/api/watchlist").json()}
    assert spooky_sym.upper() not in symbols_anon, "User-owned row exposed to the anonymous demo view"


def test_anonymous_add_stays_in_demo_namespace_and_rejects_duplicates():
    ctx = _seed_scenario()
    client = TestClient(app)
    demo_new = f"WLDA_{uuid.uuid4().hex[:8]}"

    add = client.post("/api/watchlist", json={"symbol": demo_new, "notes": "guest add"})
    assert add.status_code == 201, add.text

    # Duplicate in the SAME namespace -> 400 (existing demo-contract behavior).
    assert client.post("/api/watchlist", json={"symbol": demo_new}).status_code == 400

    # A tenant adding the same symbol in their OWN namespace is fine (no collision).
    ok = client.post("/api/watchlist", json={"symbol": demo_new}, headers=_auth(ctx["tok_a"]))
    assert ok.status_code == 201, ok.text

    # The guest symbol stays visible to anonymous; A's duplicate stays private.
    anon_syms = {it["symbol"] for it in client.get("/api/watchlist").json()}
    assert demo_new.upper() in anon_syms


# ── 3. Mutation isolation ────────────────────────────────────────────────────


def test_anonymous_cannot_delete_tenant_rows():
    ctx = _seed_scenario()
    client = TestClient(app)

    assert client.delete(f"/api/watchlist/{ctx['row_a_id']}").status_code == 404
    assert client.delete(f"/api/watchlist/{ctx['alert_a_id']}").status_code == 404


def test_user_a_cannot_delete_or_alter_user_b_rows():
    ctx = _seed_scenario()
    client = TestClient(app)
    auth_a = _auth(ctx["tok_a"])

    assert client.delete(f"/api/watchlist/{ctx['row_b_id']}", headers=auth_a).status_code == 404
    assert client.delete(f"/api/watchlist/alerts/{ctx['alert_b_id']}", headers=auth_a).status_code == 404
    assert client.patch(f"/api/watchlist/alerts/{ctx['alert_b_id']}/toggle", headers=auth_a).status_code == 404

    # B's row must still exist untouched afterwards.
    rows_b = {it["id"] for it in client.get("/api/watchlist", headers=_auth(ctx["tok_b"])).json()}
    assert ctx["row_b_id"] in rows_b
    alerts_b = {a["id"] for a in client.get("/api/watchlist/alerts/list", headers=_auth(ctx["tok_b"])).json()}
    assert ctx["alert_b_id"] in alerts_b


def test_owners_can_still_delete_and_toggle_own_rows():
    ctx = _seed_scenario()
    client = TestClient(app)
    auth_a = _auth(ctx["tok_a"])

    # Owner toggles own alert
    toggle = client.patch(f"/api/watchlist/alerts/{ctx['alert_a_id']}/toggle", headers=auth_a)
    assert toggle.status_code == 200
    assert toggle.json()["is_active"] is False

    # Owner deletes own rows
    assert client.delete(f"/api/watchlist/{ctx['row_a_id']}", headers=auth_a).status_code == 200
    assert client.delete(f"/api/watchlist/alerts/{ctx['alert_a_id']}", headers=auth_a).status_code == 200


# ── 4. Intended public demo behavior preserved (frontend contract) ───────────


def test_anonymous_demo_contract_preserved():
    """Guest landing / watchlist page: seeded demo symbols visible, anonymous
    add + delete on the demo dataset still works (no 401 regression)."""
    ctx = _seed_scenario()
    client = TestClient(app)
    demo_sym = f"WLGC_{uuid.uuid4().hex[:8]}"

    res = client.get("/api/watchlist")
    assert res.status_code == 200
    assert ctx["sym_demo"] in {it["symbol"] for it in res.json()}

    add = client.post("/api/watchlist", json={"symbol": demo_sym})
    assert add.status_code == 201, add.text
    added_id = add.json()["id"]

    assert client.delete(f"/api/watchlist/{added_id}").status_code == 200
    assert client.get("/api/watchlist/alerts/list").status_code == 200