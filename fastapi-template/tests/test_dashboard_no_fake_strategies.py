"""RED→GREEN: authenticated Dashboard must never return fabricated strategies.

Fake-product behavior this closes:
  1. An authenticated user with NO strategies previously received three
     hardcoded demo strategies with invented PnL/winRate (sma-cross-50-200,
     rsi-reversal-30, bb-squeeze-breakout) — presented as if they were real
     platform strategies.
  2. An authenticated user WITH a strategy but zero trades previously received
     ``winRate: 76.4`` and ``pnl = total_realized_pnl * 0.6`` — fabricated
     metrics presented as the strategy's real performance.

Invariants:
  * Authenticated + zero strategies  -> topStrategies == [] (honest empty).
  * Authenticated + strategy, no fills -> the entry shows real zero metrics
    (pnl/winRate/tradesCount == 0), never invented numbers.
  * Anonymous guest landing view keeps the intentional demo aggregate
    (public feature preserved).
"""

import time

from fastapi.testclient import TestClient

from app.main import app


def _register(seed: str):
    client = TestClient(app)
    uid = int(time.time() * 1000) % 1000000
    email = f"dash-{seed}-{uid}@test.com"
    reg = client.post(
        "/api/auth/register",
        json={"email": email, "password": "SecurePass1!", "full_name": "Dash Tester"},
    )
    assert reg.status_code in (200, 201), reg.text
    token = reg.json()["access_token"]
    return client, {"Authorization": f"Bearer {token}"}, email


def test_authenticated_no_strategies_returns_empty_not_fake():
    """An authenticated user with zero strategies must see an EMPTY list —
    never the three hardcoded demo strategies with fabricated PnL."""
    client, headers, _ = _register("no-strat")
    res = client.get("/api/dashboard/summary", headers=headers)
    assert res.status_code == 200, res.text
    top = res.json()["topStrategies"]
    assert isinstance(top, list)
    assert top == [], f"Authenticated user with no strategies must get [] not {top}"


def test_authenticated_strategy_no_fills_shows_honest_zero_metrics():
    """A real strategy with no fills must report zero pnl/winRate/tradesCount —
    never fabricated winRate=76.4 or invented PnL."""
    client, headers, _ = _register("honest")
    # Create a strategy (enabled=False by default)
    create = client.post(
        "/api/strategies",
        headers=headers,
        json={
            "name": "Honest Strategy",
            "symbols": ["AAPL"],
            "conditions": [{"indicator": "PRICE", "operator": "gt", "value": 100}],
            "action": {"side": "BUY", "quantity": 1},
            "enabled": False,
        },
    )
    # The strategy creation endpoint may live under /api/strategies or be
    # created via the engine; assert it exists. If the endpoint 4xx/404s, fail
    # loudly (this test depends on a real persisted strategy).
    assert create.status_code in (200, 201, 202), create.text

    res = client.get("/api/dashboard/summary", headers=headers)
    assert res.status_code == 200, res.text
    top = res.json()["topStrategies"]
    assert isinstance(top, list) and len(top) >= 1, top
    entry = top[0]
    # Never fabricated "proof" numbers.
    assert float(entry.get("pnl", -1)) == 0.0, f"Fabricated pnl: {entry}"
    assert float(entry.get("winRate", -1)) == 0.0, f"Fabricated winRate: {entry}"
    assert int(entry.get("tradesCount", -1)) == 0, f"Fabricated tradesCount: {entry}"


def test_anonymous_guest_demo_contract_preserved():
    """The anonymous landing view intentionally keeps a demo aggregate so the
    public guest page does not break."""
    client = TestClient(app)
    res = client.get("/api/dashboard/summary")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data["topStrategies"], list)
    assert "engineStatus" in data
