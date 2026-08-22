"""Unit tests for Reports API endpoints (/performance, /trades/summary, /export)."""

import asyncio
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import init_db

# Ensure all ORM tables are created
asyncio.run(init_db())

client = TestClient(app)


def test_performance_report():
    """Verify aggregated performance report response structure."""
    res = client.get("/api/reports/performance")
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
    """Verify timeline and velocity summary stats."""
    res = client.get("/api/reports/trades/summary")
    assert res.status_code == 200
    data = res.json()
    assert "total_trades" in data
    assert "buy_count" in data
    assert "sell_count" in data
    assert "timeline" in data


def test_export_reports():
    """Verify CSV and JSON export formats."""
    # 1. Test CSV Download
    csv_res = client.get("/api/reports/export?format=csv")
    assert csv_res.status_code == 200
    assert "text/csv" in csv_res.headers.get("content-type", "")
    assert "attachment; filename=tradetron_trades_" in csv_res.headers.get("content-disposition", "")
    csv_lines = csv_res.text.strip().split("\n")
    assert "Trade ID,Order ID,Strategy Name" in csv_lines[0]

    # 2. Test JSON Export
    json_res = client.get("/api/reports/export?format=json")
    assert json_res.status_code == 200
    assert isinstance(json_res.json(), list)
