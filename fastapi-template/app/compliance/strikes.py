"""TradeThrone Strike & Expiry Engine — SEBI/NSE/BSE Compliant.

Builds ATM/ITM/OTM strike ladders and resolves expiry dates
for weekly, monthly, and next-monthly contracts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

from app.compliance.lot_sizes import get_strike_step, resolve_symbol


# ─── Expiry Types ───
ExpiryType = Literal["weekly", "next_weekly", "monthly", "next_monthly"]


def _nse_holidays_2024_2025() -> set[date]:
    """NSE/BSE trading holidays (2024-2025). Update annually."""
    return {
        # 2024
        date(2024, 1, 26),   # Republic Day
        date(2024, 3, 8),    # Mahashivratri
        date(2024, 3, 25),   # Holi
        date(2024, 3, 29),   # Good Friday
        date(2024, 4, 11),   # Eid-ul-Fitr
        date(2024, 4, 17),   # Ram Navami
        date(2024, 5, 1),    # Maharashtra Day
        date(2024, 6, 17),   # Bakri Eid
        date(2024, 7, 17),   # Muharram
        date(2024, 8, 15),   # Independence Day
        date(2024, 10, 2),   # Gandhi Jayanti
        date(2024, 11, 1),   # Diwali Laxmi Pujan
        date(2024, 11, 15),  # Gurunanak Jayanti
        date(2024, 12, 25),  # Christmas
        # 2025
        date(2025, 1, 26),   # Republic Day
        date(2025, 2, 26),   # Mahashivratri
        date(2025, 3, 14),   # Holi
        date(2025, 3, 31),   # Eid-ul-Fitr
        date(2025, 4, 10),   # Ram Navami
        date(2025, 4, 14),   # Dr. Ambedkar Jayanti
        date(2025, 4, 18),   # Good Friday
        date(2025, 5, 1),    # Maharashtra Day
        date(2025, 6, 7),    # Bakri Eid
        date(2025, 7, 6),    # Muharram
        date(2025, 8, 15),   # Independence Day
        date(2025, 8, 27),   # Ganesh Chaturthi
        date(2025, 10, 2),   # Gandhi Jayanti
        date(2025, 10, 21),  # Diwali Laxmi Pujan
        date(2025, 11, 5),   # Gurunanak Jayanti
        date(2025, 12, 25),  # Christmas
    }


def _is_trading_day(d: date) -> bool:
    """Check if date is a trading day (Mon-Fri, not holiday)."""
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return d not in _nse_holidays_2024_2025()


def _next_trading_day(d: date) -> date:
    """Get next trading day."""
    nxt = d + timedelta(days=1)
    while not _is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def _last_thursday_of_month(year: int, month: int) -> date:
    """Last Thursday of given month (monthly expiry)."""
    if month == 12:
        first_next = date(year + 1, 1, 1)
    else:
        first_next = date(year, month + 1, 1)
    last_day = first_next - timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7
    expiry = last_day - timedelta(days=offset)
    while not _is_trading_day(expiry):
        expiry -= timedelta(days=1)
    return expiry


def _is_monthly_expiry(d: date) -> bool:
    """Check if a Thursday is the last Thursday of its month."""
    last_thu = _last_thursday_of_month(d.year, d.month)
    return d == last_thu


def get_expiry_list(
    symbol: str,
    from_date: date | None = None,
    count: int = 6,
) -> list[dict]:
    """Return list of upcoming expiries with metadata for a symbol."""
    if from_date is None:
        from_date = date.today()

    canonical, exchange = resolve_symbol(symbol)
    expiries = []

    is_index = canonical in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}

    # Generate weekly expiries (Thursdays) for indices
    if is_index:
        days_ahead = (3 - from_date.weekday()) % 7
        thursday = from_date + timedelta(days=days_ahead)
        while not _is_trading_day(thursday):
            thursday = _next_trading_day(thursday)

        for i in range(count):
            expiries.append({
                "date": thursday.isoformat(),
                "type": "weekly" if i < 2 else "monthly",
                "label": thursday.strftime("%d %b %Y"),
                "days_to_expiry": (thursday - from_date).days,
                "is_monthly": _is_monthly_expiry(thursday),
            })
            thursday = _next_trading_day(thursday + timedelta(days=7))

    # Generate monthly expiries (last Thursday of each month)
    current_month = from_date.replace(day=1)
    for i in range(count):
        monthly_exp = _last_thursday_of_month(current_month.year, current_month.month)
        if monthly_exp >= from_date:
            expiries.append({
                "date": monthly_exp.isoformat(),
                "type": "monthly",
                "label": monthly_exp.strftime("%d %b %Y"),
                "days_to_expiry": (monthly_exp - from_date).days,
                "is_monthly": True,
            })
        if current_month.month == 12:
            current_month = current_month.replace(year=current_month.year + 1, month=1)
        else:
            current_month = current_month.replace(month=current_month.month + 1)

    # Sort by date, deduplicate
    seen = set()
    unique = []
    for exp in sorted(expiries, key=lambda x: x["date"]):
        if exp["date"] not in seen:
            seen.add(exp["date"])
            unique.append(exp)
    return unique[:count]


def nearest_expiry(symbol: str, expiry_type: ExpiryType = "weekly") -> str:
    """Return the nearest expiry date string for a symbol and type."""
    expiries = get_expiry_list(symbol)
    if not expiries:
        return date.today().isoformat()
    if expiry_type == "weekly":
        for exp in expiries:
            if not exp["is_monthly"]:
                return exp["date"]
    return expiries[0]["date"]


def build_strike_ladder(
    symbol: str,
    spot_price: float,
    levels: int = 5,
    expiry: str | None = None,
) -> dict:
    """Build ATM/ITM/OTM strike ladder for a symbol at given spot price.

    Returns dict with:
    - atm_strike: nearest strike to spot
    - strikes: list of {strike, type, label, distance_from_atm}
    - spot_price: input spot
    - step: strike step used
    """
    canonical, _ = resolve_symbol(symbol)
    step = get_strike_step(canonical)

    # Round spot to nearest strike step
    atm_strike = round(spot_price / step) * step

    strikes = []
    for i in range(-levels, levels + 1):
        strike = atm_strike + (i * step)
        if i == 0:
            strike_type = "ATM"
            label = f"ATM ({int(strike)})"
        elif i < 0:
            strike_type = f"ITM{abs(i)}" if canonical in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"} else f"OTM{abs(i)}"
            label = f"{strike_type} ({int(strike)})"
        else:
            strike_type = f"OTM{i}"
            label = f"{strike_type} ({int(strike)})"

        strikes.append({
            "strike": int(strike),
            "type": strike_type,
            "label": label,
            "distance_from_atm": i,
            "is_atm": i == 0,
        })

    return {
        "atm_strike": int(atm_strike),
        "strikes": strikes,
        "spot_price": spot_price,
        "step": step,
        "expiry": expiry or nearest_expiry(symbol),
        "levels": levels,
    }


def resolve_strike(
    symbol: str,
    spot_price: float,
    strike_selector: str,
) -> int:
    """Resolve a strike selector (ATM, ITM1-5, OTM1-5) to actual strike price."""
    ladder = build_strike_ladder(symbol, spot_price, levels=5)
    selector = strike_selector.upper()

    if selector == "ATM":
        return ladder["atm_strike"]

    # ITM/OTM selectors
    for strike_info in ladder["strikes"]:
        if strike_info["type"] == selector:
            return strike_info["strike"]

    # Fallback to ATM
    return ladder["atm_strike"]