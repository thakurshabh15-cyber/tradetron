"""Quant QA — TradeThrone AI Quant Lab (NL Parser + Strategy Doctor)."""

import pytest

from app.quant import diagnose_strategy, parse_strategy_text
from app.quant.nl_parser import _extract_conditions


def test_parse_full_sentence():
    p = parse_strategy_text(
        "Buy NIFTY when RSI(14) drops below 35 on 15min, "
        "stop loss 0.5%, target 1%, 2 lots"
    )
    assert p["symbols"] == ["NIFTY"]
    assert p["timeframe"] == "15m"
    assert p["action"]["side"] == "BUY"
    assert p["conditions"][0] == {
        "indicator": "RSI", "operator": "lt", "value": 35.0, "period": 14,
    }
    # 2 lots x NIFTY(65) = 130
    assert p["action"]["quantity"] == 130
    assert p["risk"]["stop_loss_pct"] == 0.5
    assert p["risk"]["take_profit_pct"] == 1.0
    assert p["confidence"] >= 0.8
    assert not any(m.startswith("missing:") for m in p["unparsed"])


def test_parse_short_direction_and_hourly():
    p = parse_strategy_text(
        "Sell BANKNIFTY when RSI goes above 70 hourly, sl 1%, target 2%, 500 qty"
    )
    assert p["symbols"] == ["BANKNIFTY"]
    assert p["timeframe"] == "1h"
    assert p["action"]["side"] == "SELL"
    assert p["conditions"][0]["operator"] == "gt"
    assert p["action"]["quantity"] == 500  # qty mode, no lot multiply


@pytest.mark.parametrize("alias,sym", [
    ("nifty", "NIFTY"), ("bank nifty", "BANKNIFTY"),
    ("sensex", "SENSEX"), ("finnifty", "FINNIFTY"),
])
def test_symbol_aliases_resolve(alias, sym):
    text = f"buy {alias} when price above 100 daily"
    assert parse_strategy_text(text)["symbols"] == [sym]


def test_stock_symbols_via_lot_table():
    p = parse_strategy_text("buy RELIANCE when SMA(50) rises above 2500 daily, target 3%")
    assert p["symbols"] == ["RELIANCE"]
    assert p["conditions"][0]["indicator"] == "SMA"
    assert p["timeframe"] == "1d"


def test_price_fallback_condition():
    conds, notes = _extract_conditions("price below 24000")
    assert conds == [{"indicator": "PRICE", "operator": "lt", "value": 24000.0, "period": 1}]
    assert notes == []


def test_confidence_penalises_missing_slots():
    p = parse_strategy_text("buy when rsi low")   # no symbol/timeframe/value
    assert p["confidence"] <= 0.5
    assert len(p["unparsed"]) >= 2


def test_empty_text_rejected():
    with pytest.raises(ValueError):
        parse_strategy_text("   ")


# ─── AI Strategy Doctor ───────────────────────────────────────────────

STRATEGY = {
    "symbols": ["NIFTY"],
    "timeframe": "15m",
    "conditions": [{"indicator": "RSI", "operator": "lt", "value": 45, "period": 14}],
    "action": {"side": "BUY", "quantity": 65},
    "risk": {"stop_loss_pct": 0.5, "take_profit_pct": 1.0},
}


def test_doctor_report_shape_and_bounds():
    report = diagnose_strategy(STRATEGY, days=60)
    score = report["robustness_score"]
    assert 0 <= score <= 100
    assert report["grade"] in {"A", "B", "C", "D", "F"}
    assert isinstance(report["verdict"], str) and report["verdict"]
    weights = sum(c["weight"] for c in report["components"].values())
    assert weights == 100
    for comp in report["components"].values():
        assert 0 <= comp["score"] <= comp["weight"]
    assert len(report["findings"]) >= 7
    assert report["evidence"]["windows_tested"] == 6
    assert len(report["evidence"]["fold_net_pnls"]) == 3


def test_doctor_flags_missing_stop_loss():
    strat = dict(STRATEGY)
    strat["risk"] = {"stop_loss_pct": None, "take_profit_pct": 1.0}
    report = diagnose_strategy(strat, days=30)
    risk_findings = [f for f in report["findings"]
                     if f["component"] == "risk_management"]
    assert risk_findings and risk_findings[0]["severity"] == "warning"


def test_doctor_requires_symbol_and_conditions():
    with pytest.raises(ValueError):
        diagnose_strategy({"symbols": [], "conditions": []})


def test_doctor_deterministic():
    a = diagnose_strategy(STRATEGY, days=45)
    b = diagnose_strategy(STRATEGY, days=45)
    assert a["robustness_score"] == b["robustness_score"]
    assert a["components"] == b["components"]