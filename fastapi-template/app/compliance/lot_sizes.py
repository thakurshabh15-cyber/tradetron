"""TradeThrone Lot-Size Engine — SEBI/NSE/BSE/MCX Compliant.

Authoritative lot-size mapping for Indian derivatives markets.
All values sourced from NSE/BSE/MCX circulars (2024-2025).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

# ──────────────────────────────────────────────────────────────────────
# EXCHANGE-TRUTHFUL LOT SIZES (as of 2024-2025 circulars)
# ──────────────────────────────────────────────────────────────────────

LOT_SIZES: dict[str, int] = {
    # ─── NSE Index Futures & Options ───
    "NIFTY": 65,          # NIFTY 50
    "BANKNIFTY": 30,      # NIFTY Bank
    "FINNIFTY": 60,       # NIFTY Financial Services
    "MIDCPNIFTY": 120,    # NIFTY Midcap Select
    # ─── BSE Index Futures & Options ───
    "SENSEX": 20,         # S&P BSE SENSEX
    "BANKEX": 30,         # S&P BSE BANKEX
    # ─── NSE Stock Futures & Options (top liquid names) ───
    # Values per NSE F&O lot size circular (updated periodically)
    "RELIANCE": 250,
    "HDFCBANK": 550,
    "ICICIBANK": 1375,
    "INFY": 600,
    "TCS": 175,
    "ITC": 3200,
    "SBIN": 3000,
    "BHARTIARTL": 1851,
    "KOTAKBANK": 400,
    "LT": 300,
    "AXISBANK": 1200,
    "HINDUNILVR": 300,
    "BAJFINANCE": 250,
    "ASIANPAINT": 300,
    "MARUTI": 100,
    "SUNPHARMA": 700,
    "TITAN": 150,
    "ULTRACEMCO": 100,
    "NESTLEIND": 50,
    "POWERGRID": 7200,
    "NTPC": 10800,
    "ONGC": 3800,
    "COALINDIA": 4200,
    "TATAMOTORS": 2850,
    "TATASTEEL": 5500,
    "JSWSTEEL": 2250,
    "HINDALCO": 3500,
    "ADANIPORTS": 1600,
    "ADANIENT": 800,
    "BAJAJFINSV": 250,
    "BAJAJ-AUTO": 250,
    "HEROMOTOCO": 200,
    "EICHERMOT": 175,
    "M&M": 700,
    "DRREDDY": 125,
    "CIPLA": 1000,
    "DIVISLAB": 200,
    "APOLLOHOSP": 250,
    "BPCL": 3600,
    "IOC": 13500,
    "GAIL": 5325,
    "INDUSINDBK": 600,
    "TECHM": 1200,
    "WIPRO": 2400,
    "HCLTECH": 700,
    "BRITANNIA": 200,
    "DABUR": 2500,
    "GODREJCP": 800,
    "MARICO": 2600,
    "COLPAL": 350,
    "PIDILITIND": 250,
    "BERGEPAINT": 2200,
    "HAVELLS": 1000,
    "VOLTAS": 1000,
    "WHIRLPOOL": 250,
    "CROMPTON": 3000,
    "AMBUJACEM": 2500,
    "SHREECEM": 50,
    "ACC": 400,
    "RAMCOCEM": 800,
    "JKCEMENT": 250,
    "HEIDELBERG": 200,
    "DALBHARAT": 300,
    "NUVAMA": 200,
    "ZOMATO": 13000,
    "TRENT": 350,
    "DMART": 100,
    "NYKAA": 1250,
    "PAYTM": 1350,
    "POLICYBZR": 1100,
    "DELHIVERY": 1800,
    "CARERATING": 500,
    "ICICIGI": 1000,
    "ICICIPRULI": 1500,
    "HDFCLIFE": 1100,
    "SBILIFE": 1000,
    "MAXHEALTH": 500,
    "STARHEALTH": 400,
    # ─── MCX Commodity Futures ───
    "GOLD": 100,          # 1 kg (100 grams per lot)
    "GOLDM": 10,          # Gold Mini (10 grams)
    "GOLDGUINEA": 1,      # Gold Guinea (1 gram)
    "GOLDPETAL": 1,       # Gold Petal (1 gram)
    "SILVER": 30,         # 30 kg
    "SILVERM": 5,         # Silver Mini (5 kg)
    "SILVERMIC": 1,       # Silver Micro (1 kg)
    "COPPER": 2500,       # 2.5 MT
    "COPPERM": 250,       # Copper Mini (250 kg)
    "ZINC": 5000,         # 5 MT
    "ZINCMINI": 1000,     # Zinc Mini (1 MT)
    "LEAD": 5000,         # 5 MT
    "LEADMINI": 1000,     # Lead Mini (1 MT)
    "NICKEL": 250,        # 250 kg
    "NICKELM": 25,        # Nickel Mini (25 kg)
    "ALUMINIUM": 5000,    # 5 MT
    "ALUMINIUMM": 1000,   # Aluminium Mini (1 MT)
    "CRUDEOIL": 100,      # 100 barrels
    "CRUDEOILM": 10,      # Crude Oil Mini (10 barrels)
    "NATURALGAS": 1250,   # 1250 mmBtu
    "NATURALGASM": 250,   # Natural Gas Mini (250 mmBtu)
    "COTTON": 25,         # 25 bales
    "COTTONM": 5,         # Cotton Mini (5 bales)
    "CPO": 10,            # Crude Palm Oil (10 MT)
    "KAPAS": 20,          # Kapas (20 bales)
    "MENTHAOIL": 360,     # 360 kg
    "RUBBER": 1,          # 1 MT
    "CARDAMOM": 100,      # 100 kg
    "PEPPER": 1000,       # 1 MT
    "CASTORSEED": 10,     # 10 MT
    "GUARSEED10": 10,     # Guar Seed (10 MT)
    "GUARGUM5": 5,        # Guar Gum (5 MT)
    "SOYABEAN": 10,       # 10 MT
    "REFSOYOIL": 10,      # Refined Soy Oil (10 MT)
    "RBD PALMOLEIN": 10,  # RBD Palmolein (10 MT)
    # ─── Currency Derivatives (NSE CDS) ───
    "USDINR": 1000,       # $1000
    "EURINR": 1000,       # €1000
    "GBPINR": 1000,       # £1000
    "JPYINR": 100000,     # ¥100,000
}

# ─── Strike Step (tick) per underlying ───
STRIKE_STEPS: dict[str, int | float] = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 50,
    "SENSEX": 100,
    "BANKEX": 100,
    # Stock options typically use ₹5 or ₹10 steps depending on price band
    # Default fallback handled in get_strike_step()
}

# ─── Exchange segment mapping ───
SYMBOL_EXCHANGE: dict[str, str] = {
    **{k: "NFO" for k in [
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
        "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "SBIN",
        "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "HINDUNILVR", "BAJFINANCE",
        "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO", "NESTLEIND",
        "POWERGRID", "NTPC", "ONGC", "COALINDIA", "TATAMOTORS", "TATASTEEL",
        "JSWSTEEL", "HINDALCO", "ADANIPORTS", "ADANIENT", "BAJAJFINSV",
        "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT", "M&M", "DRREDDY", "CIPLA",
        "DIVISLAB", "APOLLOHOSP", "BPCL", "IOC", "GAIL", "INDUSINDBK",
        "TECHM", "WIPRO", "HCLTECH", "BRITANNIA", "DABUR", "GODREJCP",
        "MARICO", "COLPAL", "PIDILITIND", "BERGEPAINT", "HAVELLS", "VOLTAS",
        "WHIRLPOOL", "CROMPTON", "AMBUJACEM", "SHREECEM", "ACC", "RAMCOCEM",
        "JKCEMENT", "HEIDELBERG", "DALBHARAT", "NUVAMA", "ZOMATO", "TRENT",
        "DMART", "NYKAA", "PAYTM", "POLICYBZR", "DELHIVERY", "CARERATING",
        "ICICIGI", "ICICIPRULI", "HDFCLIFE", "SBILIFE", "MAXHEALTH", "STARHEALTH"
    ]},
    **{k: "BFO" for k in ["SENSEX", "BANKEX"]},
    **{k: "MCX" for k in [
        "GOLD", "GOLDM", "GOLDGUINEA", "GOLDPETAL", "SILVER", "SILVERM",
        "SILVERMIC", "COPPER", "COPPERM", "ZINC", "ZINCMINI", "LEAD",
        "LEADMINI", "NICKEL", "NICKELM", "ALUMINIUM", "ALUMINIUMM",
        "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATURALGASM", "COTTON",
        "COTTONM", "CPO", "KAPAS", "MENTHAOIL", "RUBBER", "CARDAMOM",
        "PEPPER", "CASTORSEED", "GUARSEED10", "GUARGUM5", "SOYABEAN",
        "REFSOYOIL", "RBD PALMOLEIN"
    ]},
    **{k: "CDS" for k in ["USDINR", "EURINR", "GBPINR", "JPYINR"]},
}


@dataclass(frozen=True, slots=True)
class QuantityValidationResult:
    """Result of quantity validation with auto-correction."""
    is_valid: bool
    original_quantity: int
    corrected_quantity: int
    lots: int
    warning: str | None = None


def get_lot_size(symbol: str) -> int:
    """Return lot size for a symbol (case-insensitive). Defaults to 1."""
    return LOT_SIZES.get(symbol.upper(), 1)


def get_strike_step(symbol: str) -> int | float:
    """Return strike step for a symbol. Defaults to 50 for indices, 5 for stocks."""
    sym = symbol.upper()
    if sym in STRIKE_STEPS:
        return STRIKE_STEPS[sym]
    # Heuristic: indices use 50/100, stocks use 5/10
    if sym in {"NIFTY", "FINNIFTY", "MIDCPNIFTY"}:
        return 50
    if sym in {"BANKNIFTY", "SENSEX", "BANKEX"}:
        return 100
    return 5  # default for stock options


def resolve_symbol(symbol: str) -> tuple[str, str]:
    """Normalize symbol and return (canonical_symbol, exchange_segment)."""
    sym = symbol.upper().replace("&", "").replace("-", "").replace(" ", "")
    # Handle common aliases
    aliases = {
        "NIFTY50": "NIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "BANKEX": "BANKEX",
        "SENSEX50": "SENSEX",
    }
    canonical = aliases.get(sym, sym)
    exchange = SYMBOL_EXCHANGE.get(canonical, "NFO")
    return canonical, exchange


def lots_to_quantity(symbol: str, lots: int) -> int:
    """Convert lots to exact quantity (lots × lot_size)."""
    return lots * get_lot_size(symbol)


def quantity_to_lots(symbol: str, quantity: int) -> tuple[int, int]:
    """Convert quantity to (full_lots, remainder_shares)."""
    lot = get_lot_size(symbol)
    return divmod(quantity, lot)


def normalize_quantity(symbol: str, quantity: int, mode: Literal["round_down", "round_up", "nearest"] = "nearest") -> int:
    """Round quantity to nearest valid lot multiple."""
    lot = get_lot_size(symbol)
    if lot == 1:
        return quantity
    full_lots, remainder = divmod(quantity, lot)
    if mode == "round_down":
        return full_lots * lot
    if mode == "round_up":
        return (full_lots + (1 if remainder else 0)) * lot
    # nearest
    return (full_lots + (1 if remainder >= lot / 2 else 0)) * lot


def validate_quantity(symbol: str, quantity: int, auto_correct: bool = True) -> QuantityValidationResult:
    """Validate quantity against lot size; optionally auto-correct."""
    lot = get_lot_size(symbol)
    if lot == 1:
        return QuantityValidationResult(
            is_valid=True,
            original_quantity=quantity,
            corrected_quantity=quantity,
            lots=quantity,
            warning=None,
        )
    full_lots, remainder = divmod(quantity, lot)
    is_valid = remainder == 0
    if is_valid:
        return QuantityValidationResult(
            is_valid=True,
            original_quantity=quantity,
            corrected_quantity=quantity,
            lots=full_lots,
            warning=None,
        )
    # Non-compliant quantity
    corrected = normalize_quantity(symbol, quantity, "nearest")
    corrected_lots = corrected // lot
    direction = "up" if corrected > quantity else "down"
    warning = (
        f"Quantity {quantity} not compliant with {symbol} lot size ({lot}). "
        f"Auto-corrected {direction} to {corrected} ({corrected_lots} lots)."
    )
    return QuantityValidationResult(
        is_valid=False,
        original_quantity=quantity,
        corrected_quantity=corrected if auto_correct else quantity,
        lots=corrected_lots if auto_correct else full_lots,
        warning=warning,
    )


# ─── Decimal precision helpers for PnL/MTM ───
def round2(value: float | Decimal | int) -> Decimal:
    """Round to 2 decimal places (banker's rounding avoided)."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def round_pnl(value: float | Decimal | int) -> float:
    """Round PnL/MTM to 2 decimals as float."""
    return float(round2(value))


# ─── Quantity/Lot Converter & Auto-Corrector ───
class InputMode:
    LOTS = "lots"
    QUANTITY = "quantity"


def convert_input_to_quantity(
    symbol: str,
    value: int,
    input_mode: str = InputMode.LOTS,
) -> tuple[int, int, str | None]:
    """
    Convert user input (lots or quantity) to exact quantity.
    
    Returns: (quantity, lots, warning)
    """
    lot_size = get_lot_size(symbol)
    
    if input_mode == InputMode.LOTS:
        # User entered lots
        quantity = value * lot_size
        return quantity, value, None
    
    elif input_mode == InputMode.QUANTITY:
        # User entered exact quantity - validate and auto-correct
        result = validate_quantity(symbol, value, auto_correct=True)
        return result.corrected_quantity, result.lots, result.warning
    
    else:
        raise ValueError(f"Invalid input_mode: {input_mode}. Use 'lots' or 'quantity'")


def convert_quantity_to_lots(symbol: str, quantity: int) -> tuple[int, int]:
    """Convert exact quantity to (full_lots, remainder)."""
    lot_size = get_lot_size(symbol)
    if lot_size == 1:
        return quantity, 0
    return divmod(quantity, lot_size)


def format_quantity_display(symbol: str, quantity: int, show_lots: bool = True) -> str:
    """Format quantity for display with lots and quantity."""
    lot_size = get_lot_size(symbol)
    lots, remainder = divmod(quantity, lot_size)
    
    if lot_size == 1:
        return f"{quantity} shares"
    
    if show_lots:
        if remainder == 0:
            return f"{lots} lots ({quantity} qty)"
        else:
            return f"{lots} lots + {remainder} ({quantity} qty) ⚠️ Non-compliant"
    return f"{quantity} qty"


# ─── Indian Transaction Charges Calculator ───
class TransactionCharges:
    """Calculate exact Indian transaction charges for equities/derivatives."""
    
    # Rates as of 2024-2025
    BROKERAGE_PER_ORDER = 20.0  # ₹20 per executed order (flat)
    STT_FUTURES = 0.0001  # 0.01% on sell side (futures)
    STT_OPTIONS = 0.000625  # 0.0625% on sell side (options premium)
    STT_EQUITY_DELIVERY = 0.001  # 0.1% on both sides (delivery)
    STT_EQUITY_INTRADAY = 0.00025  # 0.025% on sell side (intraday)
    
    EXCHANGE_TXN_FUTURES = 0.00002  # 0.002% (NSE) / 0.001% (BSE)
    EXCHANGE_TXN_OPTIONS = 0.0005  # 0.05% on premium (NSE)
    EXCHANGE_TXN_EQUITY = 0.0000325  # 0.00325% (NSE)
    
    GST_RATE = 0.18  # 18% on brokerage + exchange txn
    STAMP_DUTY = 0.00003  # 0.003% on buy side (uniform across states)
    
    # SEBI turnover fee
    SEBI_FEE = 0.000001  # ₹1 per crore = 0.0001%
    
    # Slippage
    DEFAULT_SLIPPAGE_PCT = 0.001  # 0.1% default slippage
    
    @classmethod
    def calculate_charges(
        cls,
        symbol: str,
        side: str,  # BUY/SELL
        quantity: int,
        price: float,
        product_type: str = "INTRADAY",  # INTRADAY, CARRYFORWARD (delivery)
        order_type: str = "MARKET",
        exchange: str = "NSE",
        is_option: bool = False,
        is_future: bool = False,
        slippage_pct: float | None = None,
    ) -> dict:
        """
        Calculate all transaction charges for a trade.
        
        Returns dict with breakdown of all charges and net amount.
        """
        turnover = quantity * price
        slippage_pct = slippage_pct or cls.DEFAULT_SLIPPAGE_PCT
        
        # 1. Brokerage
        brokerage = cls.BROKERAGE_PER_ORDER
        
        # 2. STT (Securities Transaction Tax)
        if is_option:
            stt = turnover * cls.STT_OPTIONS if side == "SELL" else 0
        elif is_future:
            stt = turnover * cls.STT_FUTURES if side == "SELL" else 0
        elif product_type in ("CARRYFORWARD", "DELIVERY", "CNC"):
            stt = turnover * cls.STT_EQUITY_DELIVERY
        else:  # INTRADAY / MIS
            stt = turnover * cls.STT_EQUITY_INTRADAY if side == "SELL" else 0
        
        # 3. Exchange Transaction Charges
        if is_option:
            exchange_txn = turnover * cls.EXCHANGE_TXN_OPTIONS
        elif is_future:
            exchange_txn = turnover * cls.EXCHANGE_TXN_FUTURES
        else:
            exchange_txn = turnover * cls.EXCHANGE_TXN_EQUITY
        
        # 4. GST (18% on brokerage + exchange txn)
        gst = (brokerage + exchange_txn) * cls.GST_RATE
        
        # 5. Stamp Duty (on buy side only)
        stamp_duty = turnover * cls.STAMP_DUTY if side == "BUY" else 0
        
        # 6. SEBI Turnover Fee
        sebi_fee = turnover * cls.SEBI_FEE
        
        # 7. Slippage Cost
        slippage_cost = turnover * slippage_pct
        
        # Total charges
        total_charges = brokerage + stt + exchange_txn + gst + stamp_duty + sebi_fee + slippage_cost
        
        # Net amount (for BUY: pay charges + amount; for SELL: receive amount - charges)
        if side == "BUY":
            net_amount = turnover + total_charges
        else:
            net_amount = turnover - total_charges
        
        return {
            "turnover": round_pnl(turnover),
            "brokerage": round_pnl(brokerage),
            "stt": round_pnl(stt),
            "exchange_transaction_charge": round_pnl(exchange_txn),
            "gst": round_pnl(gst),
            "stamp_duty": round_pnl(stamp_duty),
            "sebi_fee": round_pnl(sebi_fee),
            "slippage_cost": round_pnl(slippage_cost),
            "total_charges": round_pnl(total_charges),
            "net_amount": round_pnl(net_amount),
            "effective_price": round_pnl(net_amount / quantity) if quantity > 0 else 0,
            "charges_per_unit": round_pnl(total_charges / quantity) if quantity > 0 else 0,
        }
    
    @classmethod
    def calculate_round_trip(
        cls,
        symbol: str,
        quantity: int,
        buy_price: float,
        sell_price: float,
        product_type: str = "INTRADAY",
        is_option: bool = False,
        is_future: bool = False,
        slippage_pct: float | None = None,
    ) -> dict:
        """Calculate charges for a complete round trip (buy + sell)."""
        buy_charges = cls.calculate_charges(
            symbol, "BUY", quantity, buy_price, product_type, 
            is_option=is_option, is_future=is_future, slippage_pct=slippage_pct
        )
        sell_charges = cls.calculate_charges(
            symbol, "SELL", quantity, sell_price, product_type,
            is_option=is_option, is_future=is_future, slippage_pct=slippage_pct
        )
        
        gross_pnl = (sell_price - buy_price) * quantity
        total_charges = buy_charges["total_charges"] + sell_charges["total_charges"]
        net_pnl = gross_pnl - total_charges
        
        return {
            "buy": buy_charges,
            "sell": sell_charges,
            "gross_pnl": round_pnl(gross_pnl),
            "total_charges": round_pnl(total_charges),
            "net_pnl": round_pnl(net_pnl),
            "charges_as_pct_of_gross": round_pnl((total_charges / abs(gross_pnl)) * 100) if gross_pnl != 0 else 0,
            "breakeven_price_diff": round_pnl(total_charges / quantity) if quantity > 0 else 0,
        }