"""Canonical broker-position normalization layer.

Every adapter's ``get_positions()`` routes native payloads through these
normalizers.  The canonical contract is:

    symbol: str       – upper-cased trading symbol
    quantity: int     – SIGNED: positive = LONG, negative = SHORT
    side: str         – "LONG" | "SHORT" (derived from quantity sign)
    average_price: float – volume-weighted avg price (0.0 when unknown)

Rules: zero-quantity / empty-symbol rows silently dropped (None).
Side is derived exclusively from signed quantity, never guessed from a
raw ``"side"`` field.  Pure functions – no I/O, no broker calls.
Never fabricates a fill.  Never mutates broker state.
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Zerodha – kite.positions()["net"] items
# Raw keys: tradingsymbol, quantity (signed int), average_price, pnl, product
# ---------------------------------------------------------------------------
def normalize_zerodha_position(pos: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one Zerodha net position to the canonical contract.

    Kite ``quantity`` is a signed integer: positive = long, negative = short.
    """
    symbol = str(pos.get("tradingsymbol", "")).strip()
    if not symbol:
        return None
    try:
        quantity = int(pos.get("quantity", 0))
    except (TypeError, ValueError):
        return None
    if quantity == 0:
        return None
    try:
        avg_price = float(pos.get("average_price", 0.0))
    except (TypeError, ValueError):
        avg_price = 0.0
    return {
        "symbol": symbol.upper(),
        "quantity": quantity,
        "side": "LONG" if quantity > 0 else "SHORT",
        "average_price": avg_price,
    }


# ---------------------------------------------------------------------------
# Upstox – GET /portfolio/short-term-positions  "data" items
# Raw keys: tradingsymbol, quantity (signed int), buy_price, pnl, product
# ---------------------------------------------------------------------------
def normalize_upstox_position(pos: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one Upstox short-term position to the canonical contract.

    ``quantity`` is signed (positive = long).  Avg price lives under
    ``buy_price``.
    """
    symbol = str(pos.get("tradingsymbol", "")).strip()
    if not symbol:
        return None
    try:
        quantity = int(pos.get("quantity", 0))
    except (TypeError, ValueError):
        return None
    if quantity == 0:
        return None
    try:
        avg_price = float(pos.get("buy_price", 0.0))
    except (TypeError, ValueError):
        avg_price = 0.0
    return {
        "symbol": symbol.upper(),
        "quantity": quantity,
        "side": "LONG" if quantity > 0 else "SHORT",
        "average_price": avg_price,
    }


# ---------------------------------------------------------------------------
# Angel One – SmartAPI client.position()  result["data"] items
# Raw keys: tradingsymbol, netqty (str|int signed), averageprc (str|float),
#           exchange, buyqty, sellqty, pnl, product, etc.
# ---------------------------------------------------------------------------
def normalize_angelone_position(pos: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one Angel One SmartAPI position to the canonical contract.

    ``netqty`` is signed (positive = long).  May arrive as string or int.
    Average price is ``averageprc`` (may be string or float).
    """
    symbol = str(pos.get("tradingsymbol", "")).strip()
    if not symbol:
        return None
    try:
        quantity = int(float(pos.get("netqty", 0)))
    except (TypeError, ValueError):
        return None
    if quantity == 0:
        return None
    try:
        avg_price = float(pos.get("averageprc", 0.0))
    except (TypeError, ValueError):
        avg_price = 0.0
    return {
        "symbol": symbol.upper(),
        "quantity": quantity,
        "side": "LONG" if quantity > 0 else "SHORT",
        "average_price": avg_price,
    }


# ---------------------------------------------------------------------------
# Binance – GET /api/v3/account  "balances" items mapped by the adapter
# Raw keys: symbol, positionAmt (str, non-negative), entryPrice, unrealizedProfit
# ---------------------------------------------------------------------------
def normalize_binance_position(pos: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one Binance spot balance to the canonical contract.

    ``positionAmt`` is a non-negative string.  Spot has no margin shorts
    so side is LONG when quantity > 0.  ``entryPrice`` is "0" for spot.
    """
    symbol = str(pos.get("symbol", "")).strip()
    if not symbol:
        return None
    try:
        quantity = int(float(pos.get("positionAmt", "0")))
    except (TypeError, ValueError):
        return None
    if quantity == 0:
        return None
    return {
        "symbol": symbol.upper(),
        "quantity": quantity,
        "side": "LONG" if quantity > 0 else "SHORT",
        "average_price": 0.0,
    }


# ---------------------------------------------------------------------------
# Simulated broker – internal _positions dict
# Raw keys: symbol, quantity, avg_price
# ---------------------------------------------------------------------------
def normalize_simulated_position(pos: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one SimulatedBroker position to the canonical contract."""
    symbol = str(pos.get("symbol", "")).strip()
    if not symbol:
        return None
    try:
        quantity = int(pos.get("quantity", 0))
    except (TypeError, ValueError):
        return None
    if quantity == 0:
        return None
    try:
        avg_price = float(pos.get("avg_price", 0.0))
    except (TypeError, ValueError):
        avg_price = 0.0
    return {
        "symbol": symbol.upper(),
        "quantity": quantity,
        "side": "LONG" if quantity > 0 else "SHORT",
        "average_price": avg_price,
    }
