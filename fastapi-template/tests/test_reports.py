"""Unit tests for Reports API endpoints (/performance, /trades/summary, /export).

V2 contract: these endpoints are PRIVATE.  Anonymous callers receive 401 and
authenticated callers only ever see their own tenant-scoped records, so the
tests register a fresh user and exercise the endpoints with its bearer token.
"""

import asyncio
import uuid

from fastapi.testclient import TestClient
from app.main import app
from app.core.security import create_access_token
from app.db.session import SessionLocal, init_db
from app.models.user import UserRecord

# Ensure all ORM tables are created
asyncio.run(init_db())

client = TestClient(app)


def _fresh_user_headers() -> dict:
    """Create a unique authenticated user and return bearer-token headers."""
    uid = str(uuid.uuid4())

    async def _seed():
        async with SessionLocal() as session:
            session.add(
                UserRecord(
                    id=uid,
                    email=f"reports-{uid[:8]}@tradetron.io",
                    hashed_password="x",
                    full_name="Reports Tester",
                    role="trader",
                    is_active=True,
                    is_verified=True,
                )
            )
            await session.commit()

    asyncio.run(_seed())
    token = create_access_token({"sub": uid, "email": f"reports-{uid[:8]}@tradetron.io", "role": "trader"})
    return {"Authorization": f"Bearer {token}"}


def test_performance_report():
    """Verify aggregated performance report response structure (authenticated)."""
    headers = _fresh_user_headers()

    # Anonymous access is rejected — reports are private.
    assert client.get("/api/reports/performance").status_code == 401

    res = client.get("/api/reports/performance", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "summary" in data
    assert "strategy_breakdown" in data
    assert "symbol_breakdown" in data

    summary = data["summary"]
    assert "total_trades" in summary
    assert "win_rate_pct" in summary
    assert "total_realized_pnl" in summary
    assert "profit_factor" in summary


def test_trades_summary_report():
    """Verify timeline and velocity summary stats (authenticated)."""
    headers = _fresh_user_headers()

    assert client.get("/api/reports/trades/summary").status_code == 401

    res = client.get("/api/reports/trades/summary", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "total_trades" in data
    assert "buy_count" in data
    assert "sell_count" in data
    assert "timeline" in data


def test_export_reports():
    """Verify CSV and JSON export formats (authenticated, tenant-scoped)."""
    headers = _fresh_user_headers()

    # 1. Anonymous access is rejected
    assert client.get("/api/reports/export?format=csv").status_code == 401
    assert client.get("/api/reports/export?format=json").status_code == 401

    # 2. Test CSV Download
    csv_res = client.get("/api/reports/export?format=csv", headers=headers)
    assert csv_res.status_code == 200
    assert "text/csv" in csv_res.headers.get("content-type", "")
    assert "attachment; filename=tradetron_trades_" in csv_res.headers.get("content-disposition", "")
    csv_lines = csv_res.text.strip().split("\n")
    assert "Trade ID,Order ID,Strategy Name" in csv_lines[0]

    # 3. Test JSON Export
    json_res = client.get("/api/reports/export?format=json", headers=headers)
    assert json_res.status_code == 200
    assert isinstance(json_res.json(), list)
