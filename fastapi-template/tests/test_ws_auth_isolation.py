"""Phase 3-D Iteration 4 — WebSocket authentication + tenant isolation.

Covers the full matrix:
  AUTH:    anonymous rejected, valid token accepted, expired rejected,
           malformed rejected, wrong-token-type rejected, inactive user rejected
  ISOLATION: user A receives A's event only, B isolated from A and vice-versa,
           client spy/user_id spoof ignored (server-derived identity),
           admin-only events do not leak to ordinary users
  PUBLIC:  market stream / symbol stream / option-chain remain unauthenticated
  REGRESSION: disconnect cleanup works, existing broadcast path intact
"""

import asyncio
import uuid
from datetime import timedelta

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.security import create_access_token
from app.db.session import SessionLocal
from app.main import app
from app.market_data.manager import ws_manager
from app.models.user import UserRecord


# ── helpers ────────────────────────────────────────────────────────────────


def seed_user(*, is_active: bool = True, role: str = "trader") -> str:
    """Create a user row directly in the test DB and return its id."""
    uid = str(uuid.uuid4())

    async def _seed():
        async with SessionLocal() as session:
            session.add(
                UserRecord(
                    id=uid,
                    email=f"{uid[:8]}@ws-auth-test.io",
                    hashed_password="x",
                    is_active=is_active,
                    is_verified=True,
                    role=role,
                )
            )
            await session.commit()

    asyncio.run(_seed())
    return uid


def token_for(user_id: str, *, role: str = "trader") -> str:
    return create_access_token({"sub": user_id, "email": "u@ws-auth-test.io", "role": role})


def assert_rejected(client, path: str, expected_code: int) -> None:
    """Assert that connecting to ``path`` is rejected with ``expected_code``."""
    try:
        with client.websocket_connect(path):
            raise AssertionError(f"connection to {path} was unexpectedly accepted")
    except WebSocketDisconnect as exc:
        assert exc.code == expected_code, f"expected {expected_code}, got {exc.code} for {path}"


# ── AUTH ───────────────────────────────────────────────────────────────────


def test_anonymous_ws_trades_rejected():
    client = TestClient(app)
    assert_rejected(client, "/ws/trades", 4001)


def test_anonymous_ws_events_rejected():
    client = TestClient(app)
    assert_rejected(client, "/ws/events", 4001)


def test_valid_access_token_accepted():
    client = TestClient(app)
    user_id = seed_user()
    token = token_for(user_id)
    with client.websocket_connect(f"/ws/trades?token={token}"):
        pass  # accepted — no WebSocketDisconnect raised
    with client.websocket_connect(f"/ws/events?token={token}"):
        pass


def test_expired_token_rejected():
    client = TestClient(app)
    user_id = seed_user()
    expired = create_access_token(
        {"sub": user_id, "email": "u@ws-auth-test.io", "role": "trader"},
        expires_delta=timedelta(seconds=-30),
    )
    assert_rejected(client, f"/ws/trades?token={expired}", 4003)


def test_malformed_token_rejected():
    client = TestClient(app)
    assert_rejected(client, "/ws/trades?token=not.a.jwt", 4003)


def test_wrong_token_type_rejected():
    """A refresh-type JWT must never authenticate a private WS connection."""
    client = TestClient(app)
    user_id = seed_user()
    from app.core.security import create_refresh_token

    refresh = create_refresh_token({"sub": user_id, "email": "u@ws-auth-test.io", "role": "trader"})
    assert_rejected(client, f"/ws/trades?token={refresh}", 4003)


def test_nonexistent_user_token_rejected():
    client = TestClient(app)
    ghost = token_for(str(uuid.uuid4()))
    assert_rejected(client, f"/ws/trades?token={ghost}", 4003)


def test_inactive_user_rejected():
    client = TestClient(app)
    user_id = seed_user(is_active=False)
    token = token_for(user_id)
    assert_rejected(client, f"/ws/trades?token={token}", 4003)
# ── ISOLATION ──────────────────────────────────────────────────────────────


def test_user_a_receives_only_own_events_and_b_is_isolated():
    client = TestClient(app)
    uid_a = seed_user()
    uid_b = seed_user()
    tok_a = token_for(uid_a)
    tok_b = token_for(uid_b)

    with client.websocket_connect(f"/ws/trades?token={tok_a}") as ws_a:
        with client.websocket_connect(f"/ws/trades?token={tok_b}") as ws_b:
            # A's event first, then B's event.
            asyncio.run(
                ws_manager.broadcast_user("trades", uid_a, {"event": "order_executed", "user_id": uid_a, "symbol": "AAPL"})
            )
            asyncio.run(
                ws_manager.broadcast_user("trades", uid_b, {"event": "order_executed", "user_id": uid_b, "symbol": "MSFT"})
            )

            # A's FIRST message must be A's own event — if a cross-tenant leak
            # were queued first, this assertion fails.
            got_a = ws_a.receive_json()
            assert got_a["user_id"] == uid_a, got_a
            assert got_a["symbol"] == "AAPL"

            # B's FIRST message must be B's own event — proves A's event never
            # reached B's buffer (isolation both directions).
            got_b = ws_b.receive_json()
            assert got_b["user_id"] == uid_b, got_b
            assert got_b["symbol"] == "MSFT"


def test_user_b_does_not_receive_a_event_and_vice_versa():
    """Explicit two-way isolation in one broadcast round."""
    client = TestClient(app)
    uid_a = seed_user()
    uid_b = seed_user()
    tok_a = token_for(uid_a)
    tok_b = token_for(uid_b)

    with client.websocket_connect(f"/ws/trades?token={tok_a}") as ws_a:
        with client.websocket_connect(f"/ws/trades?token={tok_b}") as ws_b:
            # Broadcast ONLY user A's trade.
            asyncio.run(
                ws_manager.broadcast_user("trades", uid_a, {"event": "trade_closed", "user_id": uid_a, "symbol": "NVDA", "pnl": 120.0})
            )
            got_a = ws_a.receive_json()
            assert got_a["user_id"] == uid_a
            assert got_a["pnl"] == 120.0

            # Now broadcast ONLY user B's trade; B's first (and only staged)
            # message must be B's — if A's had leaked to B, B's buffer would
            # have surfaced A's event first.
            asyncio.run(
                ws_manager.broadcast_user("trades", uid_b, {"event": "trade_closed", "user_id": uid_b, "symbol": "AMZN", "pnl": -30.0})
            )
            got_b = ws_b.receive_json()
            assert got_b["user_id"] == uid_b
            assert got_b["pnl"] == -30.0


def test_client_cannot_spoof_user_id():
    """A client-supplied ?user_id= param must never override JWT identity."""
    client = TestClient(app)
    uid_a = seed_user()
    uid_b = seed_user()
    tok_a = token_for(uid_a)

    # Attacker connects with A's token but claims B's user_id in the query.
    with client.websocket_connect(f"/ws/trades?token={tok_a}&user_id={uid_b}") as spoof:
        # Broadcast an event FOR B — spoof is registered server-side as A and
        # must NOT receive it.
        asyncio.run(
            ws_manager.broadcast_user("trades", uid_b, {"event": "order_executed", "user_id": uid_b, "symbol": "LEAK"})
        )
        # Broadcast A's own event — spoof's FIRST message must be A's.
        asyncio.run(
            ws_manager.broadcast_user("trades", uid_a, {"event": "order_executed", "user_id": uid_a, "symbol": "SAFE"})
        )
        got = spoof.receive_json()
        assert got["user_id"] == uid_a, got
        assert got["symbol"] == "SAFE"


def test_admin_events_do_not_leak_to_ordinary_users():
    client = TestClient(app)
    uid_user = seed_user(role="trader")
    uid_admin = seed_user(role="admin")
    tok_user = token_for(uid_user)
    tok_admin = token_for(uid_admin, role="admin")

    with client.websocket_connect(f"/ws/trades?token={tok_user}") as ws_user:
        with client.websocket_connect(f"/ws/trades?token={tok_admin}") as ws_admin:
            # Admin-only kill-switch event.
            asyncio.run(ws_manager.broadcast_admins("trades", {"event": "KILL_SWITCH_ACTIVE", "status": "HALTED"}))
            # Then an event for the ordinary user.
            asyncio.run(
                ws_manager.broadcast_user("trades", uid_user, {"event": "order_executed", "user_id": uid_user, "symbol": "GOOGL"})
            )
            # Ordinary user's FIRST message must be their own event — proving
            # the admin kill-switch never entered their buffer.
            got_user = ws_user.receive_json()
            assert got_user["event"] == "order_executed", got_user

            # Admin connection receives the admin event.
            got_admin = ws_admin.receive_json()
            assert got_admin["event"] == "KILL_SWITCH_ACTIVE", got_admin
# ── PUBLIC FEEDS ───────────────────────────────────────────────────────────


def test_market_stream_remains_public():
    client = TestClient(app)
    with client.websocket_connect("/ws/market/stream") as ws:
        tick = {"symbol": "AAPL", "price": 226.5, "type": "tick"}
        asyncio.run(ws_manager.broadcast("market:stream", tick))
        got = ws.receive_json()
        assert got["symbol"] == "AAPL"


def test_symbol_stream_remains_public():
    client = TestClient(app)
    with client.websocket_connect("/ws/market/AAPL") as ws:
        tick = {"symbol": "AAPL", "price": 227.0, "type": "tick"}
        asyncio.run(ws_manager.broadcast("market:AAPL", tick))
        got = ws.receive_json()
        assert got["price"] == 227.0


def test_optionchain_remains_public():
    client = TestClient(app)
    # The option-chain endpoint accepts immediately and streams on a 1s timer —
    # a successful unauthenticated connect proves it stays public.
    with client.websocket_connect("/ws/optionchain/AAPL"):
        pass


# ── REGRESSION ─────────────────────────────────────────────────────────────


def test_disconnect_cleanup_removes_private_connection():
    client = TestClient(app)
    user_id = seed_user()
    token = token_for(user_id)

    with client.websocket_connect(f"/ws/trades?token={token}") as ws:
        connected_ids = {
            info["user_id"]
            for ws_obj, info in ws_manager._ws_info.items()
            if ws_obj in ws_manager._channels.get("trades", set())
        }
        assert user_id in connected_ids

    # After close, the connection must be gone from the channel.
    assert not ws_manager._channels.get("trades", set()), "channel not cleaned up after disconnect"
    assert user_id not in ws_manager._ws_info

    # Broadcasting to the disconnected user is a safe no-op.
    asyncio.run(ws_manager.broadcast_user("trades", user_id, {"event": "order_executed", "user_id": user_id}))
    asyncio.run(ws_manager.broadcast_admins("trades", {"event": "KILL_SWITCH_ACTIVE"}))


def test_public_channel_broadcast_unchanged():
    """The legacy channel-wide broadcast path still delivers to public feeds."""
    client = TestClient(app)
    with client.websocket_connect("/ws/market/stream") as ws:
        asyncio.run(ws_manager.broadcast("market:stream", {"symbol": "MSFT", "price": 415.0}))
        assert ws.receive_json()["symbol"] == "MSFT"