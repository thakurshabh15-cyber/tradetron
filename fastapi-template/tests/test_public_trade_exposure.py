"""Phase 3-D Iteration 7 — anonymous trade-history exposure hardening.

Implements and proves Option B for ``GET /api/trades`` and
``GET /api/trades/stats``:

  * Anonymous ``GET /api/trades``  → global public trade tape containing ONLY
    ``id``, ``symbol``, ``side``, ``quantity``, ``price``, ``executed_at``.
    ``pnl``, ``order_id`` and ``strategy_name`` must never appear.
  * Authenticated ``GET /api/trades`` → the caller's OWN records only
    (server-derived scope), retaining the full private ``TradeRead`` fields.
  * ``GET /api/trades/stats`` → authenticated only (401 for anonymous) and
    strictly scoped to the token's ``sub``.
  * Client-supplied ``?user_id=`` is never trusted for authorization/scoping.

Also proves the existing guest Dashboard feed (``publicFetch`` →
``/api/trades?limit=20``) keeps working and remains renderable by the
``TradeLog`` component, which falls back to "SMA Strategy" when
``strategy_name`` is absent.
"""

import asyncio
import uuid

from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.db.session import SessionLocal, init_db
from app.main import app
from app.models.trading import TradeRecord
from app.models.user import UserRecord

SAFE_FIELDS = {"id", "symbol", "side", "quantity", "price", "executed_at"}
LEAK_FIELDS = ("pnl", "order_id", "strategy_name")

_A_BASE_TRADES = [
    {
        "order_id": "ORD_A_WIN_1",
        "strategy_name": "Momentum Scalper",
        "symbol": "PUBWIN",
        "side": "BUY",
        "quantity": 25,
        "price": 2480.50,
        "pnl": 250.0,
    },
    {
        "order_id": "ORD_A_LOSS_1",
        "strategy_name": "Momentum Scalper",
        "symbol": "PUBLOSS",
        "side": "SELL",
        "quantity": 50,
        "price": 510.25,
        "pnl": -80.0,
    },
]

_B_BASE_TRADES = [
    {
        "order_id": "ORD_B_SNIPER_1",
        "strategy_name": "Secret Whale Bot",
        "symbol": "PUBB",
        "side": "BUY",
        "quantity": 100,
        "price": 999.99,
        "pnl": 9999.0,
    },
]


def seed_user(tag: str, trades_data: list[dict]) -> tuple[str, str, list[str]]:
    """Create a fresh user (unique id/email) plus the given trades.

    Returns ``(user_id, bearer_token, [trade_ids])``.  Fresh UUIDs per run
    keep every test idempotent against the shared test database.
    """
    uid = str(uuid.uuid4())
    email = f"{tag}-{uid[:8]}@exposure-test.io"
    trade_ids: list[str] = []
    rows: list[TradeRecord] = []
    for td in trades_data:
        tid = str(uuid.uuid4())
        trade_ids.append(tid)
        rows.append(
            TradeRecord(
                id=tid,
                user_id=uid,
                order_id=td["order_id"],
                strategy_name=td["strategy_name"],
                symbol=td["symbol"],
                side=td["side"],
                quantity=td["quantity"],
                price=td["price"],
                pnl=td["pnl"],
            )
        )

    async def _seed():
        await init_db()
        async with SessionLocal() as session:
            session.add(
                UserRecord(
                    id=uid,
                    email=email,
                    hashed_password="x",
                    is_active=True,
                    is_verified=True,
                    role="trader",
                )
            )
            for r in rows:
                session.add(r)
            await session.commit()

    asyncio.run(_seed())
    token = create_access_token({"sub": uid, "email": email, "role": "trader"})
    return uid, token, trade_ids


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 1. Anonymous tape: private fields must never leak ───────────────────────


def test_anonymous_trades_never_expose_pnl():
    """Even though the seeded rows carry pnl in the DB, the public tape omits it."""
    seed_user("anonpnl", _A_BASE_TRADES)
    seed_user("anonpnl", _B_BASE_TRADES)
    client = TestClient(app)

    res = client.get("/api/trades", params={"limit": 500})
    assert res.status_code == 200
    items = res.json()
    assert items, "expected a non-empty public tape"
    for item in items:
        assert "pnl" not in item, f"pnl leaked anonymously: {item}"
        assert "total_pnl" not in item


def test_anonymous_trades_never_expose_order_id():
    seed_user("anonord", _A_BASE_TRADES)
    seed_user("anonord", _B_BASE_TRADES)
    client = TestClient(app)

    res = client.get("/api/trades", params={"limit": 500})
    assert res.status_code == 200
    for item in res.json():
        assert "order_id" not in item, f"order_id leaked anonymously: {item}"


def test_anonymous_trades_never_expose_strategy_name():
    seed_user("anonstrat", _A_BASE_TRADES)
    seed_user("anonstrat", _B_BASE_TRADES)
    client = TestClient(app)

    res = client.get("/api/trades", params={"limit": 500})
    assert res.status_code == 200
    for item in res.json():
        assert "strategy_name" not in item, f"strategy_name leaked anonymously: {item}"


def test_anonymous_trades_never_expose_user_id():
    seed_user("anonuid", _A_BASE_TRADES)
    client = TestClient(app)

    res = client.get("/api/trades", params={"limit": 500})
    assert res.status_code == 200
    for item in res.json():
        assert "user_id" not in item, f"user_id leaked anonymously: {item}"


# ── 2. Anonymous feed still works with exactly the safe fields ──────────────


def test_anonymous_feed_works_with_safe_fields():
    """Guests get 200 with exactly {id, symbol, side, quantity, price, executed_at}."""
    _, _, ids_a = seed_user("anonsafe", _A_BASE_TRADES)
    client = TestClient(app)

    # Filter to the freshly seeded rows so the assertion targets are deterministic.
    res = client.get("/api/trades", params={"symbol": "PUBWIN", "limit": 20})
    assert res.status_code == 200
    items = res.json()
    assert any(item["id"] == ids_a[0] for item in items), "seeded row missing from tape"

    for item in items:
        assert set(item.keys()) == SAFE_FIELDS, f"unexpected key set: {sorted(item)}"
        assert item["side"] in ("BUY", "SELL")
        assert item["symbol"]
        assert item["quantity"] > 0
        assert item["price"] is not None
        assert item["executed_at"]


# ── 3. Authenticated view: strict per-user scoping + full private fields ────


def test_authenticated_user_a_cannot_see_user_b_trades():
    _, tok_a, ids_a = seed_user("scopeda", _A_BASE_TRADES)
    _, tok_b, ids_b = seed_user("scopedb", _B_BASE_TRADES)
    client = TestClient(app)

    res = client.get("/api/trades", params={"limit": 500}, headers=auth(tok_a))
    assert res.status_code == 200
    returned_ids = {item["id"] for item in res.json()}
    assert returned_ids, "expected at least one scoped trade for user A"
    assert returned_ids <= set(ids_a), "A received trades owned by someone else"
    assert ids_b[0] not in returned_ids, "A saw user B's trade"

    # Symmetric isolation: B must not see A's trades either.
    res_b = client.get("/api/trades", params={"limit": 500}, headers=auth(tok_b))
    returned_b = {item["id"] for item in res_b.json()}
    assert returned_b == set(ids_b)
    assert all(t not in returned_b for t in ids_a)


def test_authenticated_user_gets_own_private_fields():
    """Authenticated A keeps the full TradeRead: pnl, order_id, strategy_name."""
    _, tok_a, ids_a = seed_user("privatea", _A_BASE_TRADES)
    client = TestClient(app)

    res = client.get("/api/trades", params={"limit": 50}, headers=auth(tok_a))
    assert res.status_code == 200
    items = res.json()
    win = next(i for i in items if i["id"] == ids_a[0])
    loss = next(i for i in items if i["id"] == ids_a[1])

    assert float(win["pnl"]) == 250.0
    assert float(loss["pnl"]) == -80.0
    assert win["order_id"] == "ORD_A_WIN_1"
    assert loss["order_id"] == "ORD_A_LOSS_1"
    assert win["strategy_name"] == "Momentum Scalper"
    assert float(win["price"]) == 2480.50


# ── 4. /api/trades/stats: authenticated + user-scoped ───────────────────────


def test_authenticated_stats_scoped_to_user():
    """A's stats reflect only A's trades; B's stats reflect only B's."""
    _, tok_a, _ = seed_user("statsa", _A_BASE_TRADES)   # +250.0 and -80.0
    _, tok_b, _ = seed_user("statsb", _B_BASE_TRADES)   # +9999.0
    client = TestClient(app)

    res_a = client.get("/api/trades/stats", headers=auth(tok_a))
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert data_a["total_trades"] == 2
    assert data_a["winning_trades"] == 1
    assert data_a["losing_trades"] == 1
    assert float(data_a["total_pnl"]) == 170.0
    assert data_a["win_rate"] == 50.0

    res_b = client.get("/api/trades/stats", headers=auth(tok_b))
    assert res_b.status_code == 200
    data_b = res_b.json()
    assert data_b["total_trades"] == 1
    assert data_b["winning_trades"] == 1
    assert data_b["losing_trades"] == 0
    assert float(data_b["total_pnl"]) == 9999.0
    assert data_b["win_rate"] == 100.0


def test_anonymous_stats_returns_401():
    _, uid_a, _ = seed_user("statsanon", _A_BASE_TRADES)
    client = TestClient(app)

    res = client.get("/api/trades/stats")
    assert res.status_code == 401

    # Even a spoofed user_id cannot unlock the endpoint anonymously.
    res_spoof = client.get("/api/trades/stats", params={"user_id": uid_a})
    assert res_spoof.status_code == 401


# ── 5. ?user_id= spoofing can never alter authorization/scoping ─────────────


def test_user_id_query_spoofing_ignored_anonymously():
    uid_b, _, _ = seed_user("spoifanon", _A_BASE_TRADES)
    seed_user("spoifanon", _B_BASE_TRADES)
    client = TestClient(app)

    # Anonymous + ?user_id=<B> must NOT expand privileges to pnl/order_id/strategy.
    res = client.get("/api/trades", params={"user_id": uid_b, "limit": 500})
    assert res.status_code == 200
    items = res.json()
    for item in items:
        for leak in LEAK_FIELDS:
            assert leak not in item, f"{leak} leaked via spoofed user_id: {item}"
        assert set(item.keys()) == SAFE_FIELDS


def test_user_id_query_spoofing_ignored_when_authenticated():
    uid_b, tok_a, ids_a = seed_user("spoifautha", _A_BASE_TRADES)
    _, _, ids_b = seed_user("spoifauthb", _B_BASE_TRADES)
    client = TestClient(app)

    # A authenticates but asks for user_id=<B> → still only A's own trades.
    res = client.get(
        "/api/trades",
        params={"user_id": uid_b, "limit": 500},
        headers=auth(tok_a),
    )
    assert res.status_code == 200
    returned_ids = {item["id"] for item in res.json()}
    assert returned_ids <= set(ids_a)
    assert ids_b[0] not in returned_ids
    assert all(item["id"] in ids_a for item in res.json())


# ── 6. Guest Dashboard contract preserved ───────────────────────────────────


def test_guest_dashboard_feed_contract_unchanged():
    """The exact guest call (/api/trades?limit=20, no auth) keeps working and
    ships only the six fields TradeLog renders (strategy_name → SMA fallback)."""
    seed_user("guestfeed", _A_BASE_TRADES)
    seed_user("guestfeed", _B_BASE_TRADES)
    client = TestClient(app)

    res = client.get("/api/trades", params={"limit": 20})
    assert res.status_code == 200
    payload = res.json()
    assert isinstance(payload, list)
    assert len(payload) <= 20
    for item in payload:
        assert set(item.keys()) == SAFE_FIELDS
        # TradeLog renders symbol, side, quantity, price, executed_at and
        # falls back to "SMA Strategy" when strategy_name is absent.
        assert "strategy_name" not in item
        assert item["side"] in ("BUY", "SELL")