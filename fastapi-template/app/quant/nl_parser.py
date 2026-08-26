"""TradeThrone NL Strategy Parser.

Converts plain-English trading descriptions into the platform's native
strategy structure::

    "Buy NIFTY when RSI(14) drops below 30 on 15min, stop loss 1%,
     target 2%, 2 lots"
      -> {
           "symbols": ["NIFTY"], "timeframe": "15m",
           "conditions": [{"indicator": "RSI", "operator": "lt",
                           "value": 30, "period": 14}],
           "action": {"side": "BUY", "quantity": 130},
           "risk": {"stop_loss_pct": 1.0, "take_profit_pct": 2.0},
           "confidence": 0.9, ...
         }

Rule-based (regex) by design: zero external dependencies, fully
deterministic, and auditable — critical for a compliance-first product.
"""

from __future__ import annotations

import re
from typing import Any

from app.compliance.lot_sizes import LOT_SIZES, get_lot_size, resolve_symbol

# ─── Vocabulary ───────────────────────────────────────────────────────

_SYMBOL_ALIASES: dict[str, str] = {
    "nifty50": "NIFTY",
    "nifty 50": "NIFTY",
    "nifty": "NIFTY",
    "banknifty": "BANKNIFTY",
    "bank nifty": "BANKNIFTY",
    "finnifty": "FINNIFTY",
    "fin nifty": "FINNIFTY",
    "midcpnifty": "MIDCPNIFTY",
    "sensex": "SENSEX",
    "bankex": "BANKEX",
}

_TIMEFRAME_PATTERNS: list[tuple[str, str]] = [
    (r"\b(\d+)\s*-?\s*min(?:ute)?s?\b", "{n}m"),
    (r"\b(\d+)\s*m\b", "{n}m"),
    (r"\bhourly\b|\b1\s*h(?:our)?\b|\b60\s*min\b", "1h"),
    (r"\bdaily\b|\b1\s*day\b|\bday\s*chart\b", "1d"),
]

_DIRECTION_BUY = re.compile(
    r"\b(buy|long|bullish|go\s+long|enter\s+long)\b", re.IGNORECASE
)
_DIRECTION_SELL = re.compile(
    r"\b(sell|short|bearish|go\s+short|enter\s+short)\b", re.IGNORECASE
)

_INDICATOR_RE = re.compile(
    r"\b(RSI|SMA|EMA|MACD|ATR|BB_LOWER|BB_UPPER)\b"
    r"(?:\s*\(\s*(\d+)\s*\)|\s+(\d+))?",
    re.IGNORECASE,
)
_OP_BELOW = re.compile(r"\b(below|under|less\s+than|drops?\s+(?:below|under)|falls?\s+(?:below|under)|<)\b", re.I)
_OP_ABOVE = re.compile(r"\b(above|over|greater\s+than|rises?\s+above|more\s+than|>)\b", re.I)
_OP_CROSS_UP = re.compile(r"\bcross(?:es)?\s+above\b", re.I)
_OP_CROSS_DOWN = re.compile(r"\bcross(?:es)?\s+below\b", re.I)

_VALUE_RE = re.compile(r"(-?\d+(?:\.\d+)?)")
_SL_RE = re.compile(r"\b(?:stop\s*loss|sl|stop)\s*(?:of|at|=|:)?\s*(-?\d+(?:\.\d+)?)\s*%", re.I)
_TP_RE = re.compile(r"\b(?:target|tp|take\s*profit|profit\s*target)\s*(?:of|at|=|:)?\s*(-?\d+(?:\.\d+)?)\s*%", re.I)
_SIZE_RE = re.compile(r"\b(\d+)\s*(lots?|qty|quantit(?:y|ies)|shares?)\b", re.I)


def _extract_symbol(text: str) -> tuple[str | None, str]:
    lowered = text.lower()
    # Longest alias first to avoid "nifty" matching inside "banknifty"
    for alias in sorted(_SYMBOL_ALIASES, key=len, reverse=True):
        if alias in lowered:
            canonical, _ex = resolve_symbol(_SYMBOL_ALIASES[alias])
            return canonical, alias
    for sym in sorted(LOT_SIZES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(sym.lower())}\b", lowered):
            canonical, _ex = resolve_symbol(sym)
            return canonical, sym.lower()
    return None, ""


def _extract_timeframe(text: str) -> tuple[str | None, str]:
    for pattern, template in _TIMEFRAME_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            groups = m.groups()
            tf = template.format(n=groups[0]) if groups and groups[0] else template
            return tf, m.group(0)
    return None, ""


def _extract_conditions(text: str) -> tuple[list[dict], list[str]]:
    """Parse indicator conditions; returns (conditions, unmatched_notes)."""
    conditions: list[dict] = []
    notes: list[str] = []

    for m in _INDICATOR_RE.finditer(text):
        indicator = m.group(1).upper().replace("BB_", "BOLLINGER_")
        period = int(m.group(2) or m.group(3) or 14)
        tail = text[m.end(): m.end() + 60]

        value_m = _VALUE_RE.search(tail)
        value = float(value_m.group(1)) if value_m else None

        op = None
        if _OP_CROSS_UP.match(tail):
            op = "cross_above"
        elif _OP_CROSS_DOWN.match(tail):
            op = "cross_below"
        elif _OP_BELOW.search(tail[:40]):
            op = "lt"
        elif _OP_ABOVE.search(tail[:40]):
            op = "gt"

        if indicator == "MACD":
            conditions.append({
                "indicator": "MACD",
                "operator": "gt" if op == "gt" else "lt",
                "value": value or 0,
                "period": period,
            })
            continue

        if op is None or value is None:
            notes.append(f"Could not resolve comparison for {indicator}({period})")
            continue

        conditions.append({
            "indicator": indicator,
            "operator": op,
            "value": value,
            "period": period,
        })

    # Bare price rules e.g. "price above 24000" without a named indicator
    if not conditions:
        pm = re.search(
            r"\bprice\b(.{0,25}?)(below|above|<|>)\s*(-?\d+(?:\.\d+)?)",
            text, re.IGNORECASE,
        )
        if pm:
            op = "lt" if pm.group(2).lower() in ("below", "<") else "gt"
            conditions.append({"indicator": "PRICE", "operator": op,
                               "value": float(pm.group(3)), "period": 1})

    return conditions, notes


def parse_strategy_text(text: str) -> dict[str, Any]:
    """Parse an English strategy description into platform-native JSON."""
    if not text or not text.strip():
        raise ValueError("Strategy description is empty")

    symbol, symbol_raw = _extract_symbol(text)
    timeframe, tf_raw = _extract_timeframe(text)
    conditions, cond_notes = _extract_conditions(text)

    direction = None
    buy_m = _DIRECTION_BUY.search(text)
    sell_m = _DIRECTION_SELL.search(text)
    if buy_m and (not sell_m or buy_m.start() < sell_m.start()):
        direction = "BUY"
    elif sell_m:
        direction = "SELL"

    sl_m = _SL_RE.search(text)
    tp_m = _TP_RE.search(text)
    size_m = _SIZE_RE.search(text)

    quantity = 1
    size_note = None
    if size_m:
        n = int(size_m.group(1))
        unit = size_m.group(2).lower()
        lots_mode = unit.startswith("lot")
        if symbol:
            ls = get_lot_size(symbol)
            quantity = n * ls if lots_mode else n
            if lots_mode:
                size_note = f"{n} lot(s) x {ls} = {quantity} qty"
        else:
            quantity = n

    parsed: dict[str, Any] = {
        "engine": "TradeThrone NL Strategy Parser v1",
        "name": (text.strip()[:60] + ("…" if len(text.strip()) > 60 else "")),
        "symbols": [symbol] if symbol else [],
        "timeframe": timeframe,
        "conditions": conditions,
        "action": {"side": direction or "BUY", "quantity": quantity},
        "risk": {
            "stop_loss_pct": float(sl_m.group(1)) if sl_m else None,
            "take_profit_pct": float(tp_m.group(1)) if tp_m else None,
        },
        "notes": ([] + ([f"size: {size_note}"] if size_note else []) + cond_notes),
        "unparsed": [],
    }

    # Confidence scoring over the five core slots
    checks = [
        symbol is not None,
        timeframe is not None,
        bool(conditions),
        direction is not None,
        sl_m is not None or tp_m is not None,
    ]
    parsed["confidence"] = round(sum(checks) / len(checks), 2)

    labels = ["symbol", "timeframe", "entry_condition", "direction", "risk_exit"]
    missing = [label for label, ok in zip(labels, checks) if not ok]
    parsed["unparsed"] = [f"missing:{m}" for m in missing]

    return parsed