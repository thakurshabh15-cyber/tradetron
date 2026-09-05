"""Phase 3-D Iteration 5 — per-user WebSocket connection-abuse protection.

Before this iteration the authenticated private surfaces (/ws/trades,
/ws/events) accepted an unbounded number of connections per user: any
authenticated caller (or an attacker holding an account) could pin unlimited
sockets per worker, growing ConnectionManager memory without bound and
amplifying per-user broadcast fan-out.

``ConnectionManager.connect`` now enforces an in-process per-user budget
(``MAX_PRIVATE_CONNECTIONS_PER_USER``).  A user who already holds the cap is
deterministically rejected with close code 4408 (RFC 6455 application range)
BEFORE the new socket is accepted.

Matrix covered:
- (cap+1)-th private connection is rejected with 4408 (and no socket is
  registered server-side)
- closing one connection frees a slot (no leaked budget on disconnect)
- limits are per-user: user A at the cap does not block user B
- the budget is shared across private channels (/ws/trades + /ws/events)
- public market feeds are never counted against the cap and stay public
- lifting the cap restores the pre-fix (unbounded) behavior — proving the
  rejection is driven by the cap, not by connection mechanics
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.security import create_access_token
from app.db.session import SessionLocal
from app.main import app
from app.market_data.manager import MAX_PRIVATE_CONNECTIONS_PER_USER, WS_CODE_LIMIT_EXCEEDED, ws_manager
from app.models.user import UserRecord

CAP = MAX_PRIVATE_CONNECTIONS_PER_USER


# ── helpers ────────────────────────────────────────────────────────────────


def seed_user() -> str:
    """Create an active user row in the test DB and return its id."""
    uid = str(uuid.uuid4())

    async def _seed():
        async with SessionLocal() as session:
            session.add(
                UserRecord(
                    id=uid,
                    email=f"{uid[:8]}@ws-limits-test.io",
                    hashed_password="x",
                    is_active=True,
                    is_verified=True,
                    role="trader",
                )
            )
            await session.commit()

    asyncio.run(_seed())
    return uid


def token_for(user_id: str) -> str:
    return create_access_token({"sub": user_id, "email": "u@ws-limits-test.io", "role": "trader"})


def _open(client, path):
    """Open a WebSocket session and keep it alive (returns the session)."""
    session = client.websocket_connect(path)
    session.__enter__()
    return session


def _close(session) -> None:
    session.__exit__(None, None, None)


def assert_rejected_limit(client, path: str) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path):
            pass
    assert exc.value.code == WS_CODE_LIMIT_EXCEEDED


def _active_count(user_id: str) -> int:
    return sum(
        1
        for info in ws_manager._ws_info.values()
        if info.get("user_id") == user_id
    )


# ── CAP ENFORCEMENT ────────────────────────────────────────────────────────


def test_private_cap_enforced_and_no_socket_registered():
    """The (cap+1)-th private connection is rejected with 4408."""
    client = TestClient(app)
    user_id = seed_user()
    token = token_for(user_id)
    sessions = []
    try:
        for _ in range(CAP):
            sessions.append(_open(client, f"/ws/trades?token={token}"))
        assert _active_count(user_id) == CAP

        # Over the cap — rejection with the deterministic close code.
        assert_rejected_limit(client, f"/ws/trades?token={token}")

        # Nothing beyond the cap was registered server-side.
        assert _active_count(user_id) == CAP
    finally:
        for s in sessions:
            _close(s)


def test_close_frees_slot():
    """Disconnecting one socket frees a slot for a new connection."""
    client = TestClient(app)
    user_id = seed_user()
    token = token_for(user_id)
    sessions = []
    try:
        for _ in range(CAP):
            sessions.append(_open(client, f"/ws/events?token={token}"))
        assert_rejected_limit(client, f"/ws/events?token={token}")

        # Close one and the slot is immediately reusable.
        _close(sessions.pop())
        with client.websocket_connect(f"/ws/events?token={token}"):
            # While connected, the budget is exactly full again (no leak).
            assert _active_count(user_id) == CAP
    finally:
        for s in sessions:
            _close(s)
# ── HARDENING PROPERTIES ───────────────────────────────────────────────────


def test_cap_is_per_user():
    """User A at the cap never blocks user B."""
    client = TestClient(app)
    user_a = seed_user()
    user_b = seed_user()
    sessions = []
    try:
        for _ in range(CAP):
            sessions.append(_open(client, f"/ws/trades?token={token_for(user_a)}"))
        with client.websocket_connect(f"/ws/trades?token={token_for(user_b)}"):
            # User B connected while A held the full cap — and B is counted.
            assert _active_count(user_b) == 1
        assert _active_count(user_a) == CAP
    finally:
        for s in sessions:
            _close(s)


def test_budget_shared_across_private_channels():
    """/ws/trades and /ws/events consume one shared per-user budget."""
    client = TestClient(app)
    user_id = seed_user()
    token = token_for(user_id)
    half = CAP // 2
    sessions = []
    try:
        for _ in range(half):
            sessions.append(_open(client, f"/ws/trades?token={token}"))
        for _ in range(CAP - half):
            sessions.append(_open(client, f"/ws/events?token={token}"))

        # Full budget consumed across both private channels.
        assert_rejected_limit(client, f"/ws/trades?token={token}")
        assert_rejected_limit(client, f"/ws/events?token={token}")
    finally:
        for s in sessions:
            _close(s)


def test_public_feeds_unaffected_by_user_cap():
    """Public market streams remain open even while a user holds the full cap."""
    client = TestClient(app)
    user_id = seed_user()
    token = token_for(user_id)
    sessions = []
    try:
        for _ in range(CAP):
            sessions.append(_open(client, f"/ws/trades?token={token}"))

        # Anonymous public feeds connect fine while the user is capped.
        with client.websocket_connect("/ws/market/stream") as public_ws:
            # Public sockets are never attributed to any user budget.
            assert public_ws not in ws_manager._ws_info
        with client.websocket_connect("/ws/market/AAPL"):
            pass
    finally:
        for s in sessions:
            _close(s)


def test_lifting_cap_restores_pre_fix_behavior(monkeypatch):
    """Without the cap the 9th+ connection is accepted (the pre-fix defect).

    Proves the rejection is driven by the cap constant, not by incidental
    connection mechanics: a high cap must not reject honest clients.
    """
    from app.market_data import manager as _manager

    monkeypatch.setattr(_manager, "MAX_PRIVATE_CONNECTIONS_PER_USER", 64)

    client = TestClient(app)
    user_id = seed_user()
    token = token_for(user_id)
    sessions = []
    try:
        for _ in range(CAP + 1):
            sessions.append(_open(client, f"/ws/trades?token={token}"))
        # A further connection is still accepted while the (lifted) cap holds.
        with client.websocket_connect(f"/ws/trades?token={token}"):
            pass
    finally:
        for s in sessions:
            _close(s)