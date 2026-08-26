"""Institutional DMA execution engine — pure pre-trade analytics.

Lot-size auto-correction · asset classification · statutory charge engine ·
margin multipliers.  Consumed by POST /api/v1/orders/execute-dma.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ── Exchange lot sizes (auto-correction contract) ──
LOT_SIZES: dict[str, int] = {
    "BANKNIFTY": 30,
    "MIDCPNIFTY": 120,
    "FINNIFTY": 65,
    "SENSEX": 20,
    "NIFTY": 65,
}

# ── Margin multipliers per asset class (leverage fraction of notional) ──
MARGIN_MULTIPLIERS: dict[str, float] = {
    "INDEX_FNO": 0.20,
    "EQUITY_MIS": 0.25,
    "EQUITY_CNC": 1.00,
    "COMMODITY": 0.12,
    "CRYPTO": 1.00,
    "FOREX": 0.20,
}

_CRYPTO_HINTS = ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE")
_INDEX_HINTS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")


def classify_asset(symbol: str, product: str) -> str:
    s = symbol.upper()
    if any(s.startswith(p) for p in _CRYPTO_HINTS) and "USDT" in s:
        return "CRYPTO"
    if any(idx in s for idx in _INDEX_HINTS):
        return "INDEX_FNO"
    return "EQUITY_CNC" if product == "CNC" else "EQUITY_MIS"


def get_lot_size(symbol: str) -> int:
    s = symbol.upper()
    # Longest-symbol-first prevents NIFTY from shadowing BANKNIFTY/FINNIFTY
    for key, size in sorted(LOT_SIZES.items(), key=lambda kv: -len(kv[0])):
        if key in s:
            return size
    return 1


def compute_statutory_charges(symbol: str, side: str, product: str, quantity: int, price: float) -> dict:
    """Exact Indian statutory breakdown — flat Rs20 brokerage desk schedule."""
    turnover = round(quantity * price, 2)
    asset = classify_asset(symbol, product)
    brokerage = 20.0

    if asset == "CRYPTO":
        stt = 0.0
    elif asset == "INDEX_FNO":
        stt = round(turnover * 0.000625, 2) if side == "SELL" else 0.0
    elif asset == "EQUITY_CNC":
        stt = round(turnover * 0.001, 2)
    else:
        stt = round(turnover * 0.00025, 2) if side == "SELL" else 0.0

    exch_rate = 0.00173 if asset == "INDEX_FNO" else 0.00297
    txn = round(turnover * exch_rate / 100, 2)
    sebi = round(turnover * 0.0001, 2)
    gst = round((brokerage + txn + sebi) * 0.18, 2)

    stamp = 0.0
    if side == "BUY":
        if asset == "EQUITY_CNC":
            stamp = round(turnover * 0.00015, 2)
        elif asset != "CRYPTO":
            stamp = round(turnover * 0.00003, 2)

    total = round(brokerage + stt + txn + sebi + gst + stamp, 2)
    return {
        "brokerage": brokerage,
        "stt_ctt": stt,
        "exchange_transaction": txn,
        "sebi_fees": sebi,
        "gst": gst,
        "stamp_duty": stamp,
        "total": total,
        "turnover": turnover,
        "asset_class": asset,
    }


def compute_margin_required(symbol: str, product: str, quantity: int, price: float):
    asset = classify_asset(symbol, product)
    mult = MARGIN_MULTIPLIERS.get(asset, 1.0)
    return round(quantity * price * mult, 2), asset


class DMAOrderRequest(BaseModel):
    """Institutional DMA ticket payload with exchange-native semantics."""

    symbol: str = Field(..., description="Ticker e.g. NIFTY, BANKNIFTY, RELIANCE, BTCUSDT")
    side: Literal["BUY", "SELL"]
    lots: int = Field(1, ge=1, le=1000, description="Exchange lots (auto-corrected)")
    product: Literal["MIS", "CNC", "NRML"] = "MIS"
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: Optional[float] = Field(None, gt=0)
    stop_loss_pct: Optional[float] = Field(None, gt=0, le=50)
    take_profit_pct: Optional[float] = Field(None, gt=0, le=100)
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    broker_account_id: Optional[str] = None
    strategy_id: Optional[str] = None
