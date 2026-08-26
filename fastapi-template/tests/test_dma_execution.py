"""Institutional DMA execution engine tests.

Covers: dynamic lot auto-correction (NIFTY 65 / BANKNIFTY 30 / SENSEX 20),
statutory charge engine, margin multipliers, authenticated /execute-dma flow,
and chart-drag risk-target validation rules.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dma_engine import (
    compute_margin_required,
    compute_statutory_charges,
    get_lot_size,
)
from app.main import app


# ── Pure engine unit tests ──────────────────────────────────────────────────

def test_lot_sizes_contract():
    assert get_lot_size("NIFTY") == 65
    assert get_lot_size("BANKNIFTY") == 30
    assert get_lot_size("SENSEX") == 20
    assert get_lot_size("NIFTY28AUG2448000CE") == 65
    assert get_lot_size("RELIANCE") == 1  # equity default


def test_statutory_charges_structure_and_math():
    c = compute_statutory_charges("NIFTY", "BUY", "MIS", 65, 21000.0)
    assert c["brokerage"] == 20.0                      # flat desk schedule
    assert c["stt_ctt"] == 0.0                          # buy-side index F&O exempt
    assert c["turnover"] == 65 * 21000.0
    expected_gst = round((20.0 + c["exchange_transaction"] + c["sebi_fees"]) * 0.18, 2)
    assert c["gst"] == expected_gst
    assert c["total"] > c["gst"] + c["brokerage"]

    sell = compute_statutory_charges("NIFTY", "SELL", "MIS", 65, 21000.0)
    assert sell["stt_ctt"] > 0                          # sell side taxed

    delivery = compute_statutory_charges("RELIANCE", "BUY", "CNC", 100, 2500.0)
    assert delivery["stamp_duty"] > 0                   # buy-side delivery stamp

    crypto = compute_statutory_charges("BTCUSDT", "BUY", "NRML", 1, 65000.0)
    assert crypto["stt_ctt"] == 0.0 and crypto["stamp_duty"] == 0.0


def test_margin_multipliers():
    m_idx, a_idx = compute_margin_required("NIFTY", "MIS", 65, 21000.0)
    assert a_idx == "INDEX_FNO"
    assert m_idx == round(65 * 21000.0 * 0.20, 2)       # 5x leverage

    m_cnc, a_cnc = compute_margin_required("RELIANCE", "CNC", 100, 2500.0)
    assert a_cnc == "EQUITY_CNC" and m_cnc == 250000.0  # full outlay


# ── Authenticated API integration ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_dma_execute_and_risk_targets_flow():
    from app.db.session import init_db

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reg = await client.post("/api/auth/register", json={
            "email": "dma_desk_898665@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "DMA Desk Tester",
        })
        assert reg.status_code == 201, reg.text
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

        # Execute 2 lots of NIFTY → auto-corrected to 130 qty
        res = await client.post("/api/v1/orders/execute-dma", json={
            "symbol": "NIFTY", "side": "BUY", "lots": 2,
            "product": "MIS", "order_type": "MARKET",
            "stop_loss_pct": 0.5, "take_profit_pct": 1.0,
            "mode": "PAPER",
        }, headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["success"] is True
        assert body["lot_size"] == 65 and body["quantity"] == 130
        assert body["status"] == "FILLED"
        assert body["executed_price"] > 0
        assert body["charges"]["total"] > 0
        assert body["margin_required"] > 0
        assert isinstance(body["latency_ms"], (int, float))
        assert "within_50ms_slo" in body
        assert body["stop_loss_price"] and body["take_profit_price"]
        pos_id = body["position_id"]

        # Valid drag-modify: LONG SL below entry
        entry = body["executed_price"]
        ok = await client.patch(
            f"/api/v1/orders/positions/{pos_id}/risk-targets",
            json={"stop_loss_price": round(entry * 0.97, 2)},
            headers=headers,
        )
        assert ok.status_code == 200
        assert ok.json()["stop_loss_price"] == round(entry * 0.97, 2)

        # Invalid: LONG TP below entry rejected
        bad = await client.patch(
            f"/api/v1/orders/positions/{pos_id}/risk-targets",
            json={"take_profit_price": round(entry * 0.99, 2)},
            headers=headers,
        )
        assert bad.status_code == 422

        # Unauthenticated blocked
        anon = await client.post("/api/v1/orders/execute-dma", json={
            "symbol": "NIFTY", "side": "BUY", "lots": 1,
        })
        assert anon.status_code in (401, 403)
