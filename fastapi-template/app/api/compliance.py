"""TradeThrone Compliance API — SEBI/NSE/BSE/MCX Market Rules.

Exposes lot-size validation, strike ladders, and expiry calendars
for the frontend strategy builder and order panels.
"""

from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field

from app.compliance.lot_sizes import (
    LOT_SIZES,
    STRIKE_STEPS,
    get_lot_size,
    get_strike_step,
    normalize_quantity,
    lots_to_quantity,
    quantity_to_lots,
    validate_quantity,
    resolve_symbol,
    round_pnl,
    # New imports for quantity converter and charges
    InputMode,
    convert_input_to_quantity,
    convert_quantity_to_lots,
    format_quantity_display,
    TransactionCharges,
)
from app.compliance.strikes import (
    build_strike_ladder,
    get_expiry_list,
    nearest_expiry,
    resolve_strike,
)

router = APIRouter(prefix="/api/compliance", tags=["compliance"])


# ─── Request/Response Models ───

class QuantityValidateRequest(BaseModel):
    symbol: str = Field(..., description="Trading symbol (e.g., NIFTY, RELIANCE)")
    quantity: int = Field(..., ge=1, description="Raw quantity to validate")
    auto_correct: bool = Field(True, description="Auto-correct to nearest valid lot")


class QuantityValidateResponse(BaseModel):
    is_valid: bool
    original_quantity: int
    corrected_quantity: int
    lots: int
    lot_size: int
    warning: str | None = None


class LotsConvertRequest(BaseModel):
    symbol: str
    lots: int = Field(..., ge=1)


class LotsConvertResponse(BaseModel):
    symbol: str
    lots: int
    lot_size: int
    quantity: int


class QuantityConvertRequest(BaseModel):
    symbol: str
    quantity: int = Field(..., ge=1)


class QuantityConvertResponse(BaseModel):
    symbol: str
    quantity: int
    lot_size: int
    full_lots: int
    remainder: int


class StrikeLadderRequest(BaseModel):
    symbol: str
    spot_price: float = Field(..., gt=0)
    levels: int = Field(5, ge=1, le=10)
    expiry: str | None = None


class StrikeLadderResponse(BaseModel):
    atm_strike: int
    strikes: list[dict]
    spot_price: float
    step: int | float
    expiry: str
    levels: int


class ExpiryListResponse(BaseModel):
    symbol: str
    expiries: list[dict]


class ResolveStrikeRequest(BaseModel):
    symbol: str
    spot_price: float = Field(..., gt=0)
    strike_selector: str = Field(..., description="ATM, ITM1-5, OTM1-5")


class ResolveStrikeResponse(BaseModel):
    symbol: str
    spot_price: float
    selector: str
    strike: int


# New models for quantity/lot converter
class ConvertInputRequest(BaseModel):
    symbol: str
    value: int = Field(..., ge=1, description="Number of lots or exact quantity")
    input_mode: str = Field("lots", pattern="^(lots|quantity)$", description="Input mode: 'lots' or 'quantity'")


class ConvertInputResponse(BaseModel):
    symbol: str
    input_mode: str
    input_value: int
    quantity: int
    lots: int
    lot_size: int
    warning: str | None = None
    display: str


# New models for transaction charges
class ChargesRequest(BaseModel):
    symbol: str
    side: str = Field(..., pattern="^(BUY|SELL)$")
    quantity: int = Field(..., ge=1)
    price: float = Field(..., gt=0)
    product_type: str = Field("INTRADAY", pattern="^(INTRADAY|CARRYFORWARD|DELIVERY|CNC|MIS|NRML)$")
    order_type: str = Field("MARKET", pattern="^(MARKET|LIMIT|SL|SL-M)$")
    exchange: str = Field("NSE", pattern="^(NSE|BSE|NFO|BFO|MCX|CDS)$")
    is_option: bool = False
    is_future: bool = False
    slippage_pct: float | None = Field(None, ge=0, le=0.1)


class ChargesResponse(BaseModel):
    turnover: float
    brokerage: float
    stt: float
    exchange_transaction_charge: float
    gst: float
    stamp_duty: float
    sebi_fee: float
    slippage_cost: float
    total_charges: float
    net_amount: float
    effective_price: float
    charges_per_unit: float


class RoundTripRequest(BaseModel):
    symbol: str
    quantity: int = Field(..., ge=1)
    buy_price: float = Field(..., gt=0)
    sell_price: float = Field(..., gt=0)
    product_type: str = Field("INTRADAY", pattern="^(INTRADAY|CARRYFORWARD|DELIVERY|CNC|MIS|NRML)$")
    is_option: bool = False
    is_future: bool = False
    slippage_pct: float | None = Field(None, ge=0, le=0.1)


class RoundTripResponse(BaseModel):
    buy: ChargesResponse
    sell: ChargesResponse
    gross_pnl: float
    total_charges: float
    net_pnl: float
    charges_as_pct_of_gross: float
    breakeven_price_diff: float


# ─── Endpoints ───

@router.get("/lot-sizes", summary="Get all lot sizes")
async def list_lot_sizes() -> dict[str, int]:
    """Return the complete lot-size mapping for all supported symbols."""
    return LOT_SIZES


@router.get("/lot-size/{symbol}", summary="Get lot size for a symbol")
async def get_lot_size_endpoint(symbol: str) -> dict:
    """Return lot size and strike step for a specific symbol."""
    canonical, exchange = resolve_symbol(symbol)
    return {
        "symbol": canonical,
        "exchange": exchange,
        "lot_size": get_lot_size(canonical),
        "strike_step": get_strike_step(canonical),
    }


@router.post("/validate-quantity", response_model=QuantityValidateResponse, summary="Validate & auto-correct quantity")
async def validate_quantity_endpoint(req: QuantityValidateRequest) -> QuantityValidateResponse:
    """Validate quantity against SEBI lot-size rules; auto-correct if needed."""
    canonical, _ = resolve_symbol(req.symbol)
    result = validate_quantity(canonical, req.quantity, req.auto_correct)
    return QuantityValidateResponse(
        is_valid=result.is_valid,
        original_quantity=result.original_quantity,
        corrected_quantity=result.corrected_quantity,
        lots=result.lots,
        lot_size=get_lot_size(canonical),
        warning=result.warning,
    )


@router.post("/lots-to-quantity", response_model=LotsConvertResponse, summary="Convert lots to quantity")
async def lots_to_quantity_endpoint(req: LotsConvertRequest) -> LotsConvertResponse:
    """Convert lots to exact exchange-compliant quantity."""
    canonical, _ = resolve_symbol(req.symbol)
    qty = lots_to_quantity(canonical, req.lots)
    return LotsConvertResponse(
        symbol=canonical,
        lots=req.lots,
        lot_size=get_lot_size(canonical),
        quantity=qty,
    )


@router.post("/quantity-to-lots", response_model=QuantityConvertResponse, summary="Convert quantity to lots")
async def quantity_to_lots_endpoint(req: QuantityConvertRequest) -> QuantityConvertResponse:
    """Decompose quantity into full lots + remainder."""
    canonical, _ = resolve_symbol(req.symbol)
    full_lots, remainder = quantity_to_lots(canonical, req.quantity)
    return QuantityConvertResponse(
        symbol=canonical,
        quantity=req.quantity,
        lot_size=get_lot_size(canonical),
        full_lots=full_lots,
        remainder=remainder,
    )


@router.post("/normalize-quantity", summary="Normalize quantity to nearest lot multiple")
async def normalize_quantity_endpoint(
    symbol: str = Query(...),
    quantity: int = Query(..., ge=1),
    mode: str = Query("nearest", pattern="^(round_down|round_up|nearest)$"),
) -> dict:
    """Round quantity to nearest valid lot multiple."""
    canonical, _ = resolve_symbol(symbol)
    normalized = normalize_quantity(canonical, quantity, mode)  # type: ignore[arg-type]
    return {
        "symbol": canonical,
        "original_quantity": quantity,
        "normalized_quantity": normalized,
        "lot_size": get_lot_size(canonical),
        "mode": mode,
    }


@router.post("/strike-ladder", response_model=StrikeLadderResponse, summary="Build ATM/ITM/OTM strike ladder")
async def strike_ladder_endpoint(req: StrikeLadderRequest) -> StrikeLadderResponse:
    """Build strike ladder for strategy construction (Visual Builder)."""
    canonical, _ = resolve_symbol(req.symbol)
    ladder = build_strike_ladder(canonical, req.spot_price, req.levels, req.expiry)
    return StrikeLadderResponse(**ladder)


@router.get("/expiries", response_model=ExpiryListResponse, summary="Get upcoming expiries for a symbol")
async def expiries_endpoint(
    symbol: str = Query(...),
    count: int = Query(6, ge=1, le=12),
) -> ExpiryListResponse:
    """Return upcoming weekly/monthly expiries with metadata."""
    canonical, _ = resolve_symbol(symbol)
    expiries = get_expiry_list(canonical, count=count)
    return ExpiryListResponse(symbol=canonical, expiries=expiries)


@router.get("/nearest-expiry", summary="Get nearest expiry date")
async def nearest_expiry_endpoint(
    symbol: str = Query(...),
    expiry_type: str = Query("weekly", pattern="^(weekly|monthly)$"),
) -> dict:
    """Return nearest expiry date string for a symbol."""
    canonical, _ = resolve_symbol(symbol)
    exp_date = nearest_expiry(canonical, expiry_type)  # type: ignore[arg-type]
    return {"symbol": canonical, "expiry_type": expiry_type, "expiry_date": exp_date}


@router.post("/resolve-strike", response_model=ResolveStrikeResponse, summary="Resolve strike selector to price")
async def resolve_strike_endpoint(req: ResolveStrikeRequest) -> ResolveStrikeResponse:
    """Convert ATM/ITM1-5/OTM1-5 selector to actual strike price."""
    canonical, _ = resolve_symbol(req.symbol)
    strike = resolve_strike(canonical, req.spot_price, req.strike_selector)
    return ResolveStrikeResponse(
        symbol=canonical,
        spot_price=req.spot_price,
        selector=req.strike_selector.upper(),
        strike=strike,
    )


@router.get("/strike-step/{symbol}", summary="Get strike step for a symbol")
async def strike_step_endpoint(symbol: str) -> dict:
    """Return strike step (tick size) for a symbol."""
    canonical, _ = resolve_symbol(symbol)
    return {
        "symbol": canonical,
        "strike_step": get_strike_step(canonical),
    }


@router.post("/round-pnl", summary="Round PnL/MTM to 2 decimals (banker-safe)")
async def round_pnl_endpoint(value: float = Query(...)) -> dict:
    """Round floating-point PnL to exactly 2 decimal places."""
    return {"original": value, "rounded": round_pnl(value)}


# ─── New Endpoints: Quantity/Lot Converter ───

@router.post("/convert-input", response_model=ConvertInputResponse, summary="Convert lots/quantity input to exact quantity")
async def convert_input_endpoint(req: ConvertInputRequest) -> ConvertInputResponse:
    """
    Convert user input (lots or exact quantity) to exchange-compliant quantity.
    
    Input modes:
    - 'lots': User enters number of lots (e.g., 2 lots of NIFTY = 130 qty)
    - 'quantity': User enters exact quantity (auto-corrected to nearest lot multiple)
    """
    canonical, _ = resolve_symbol(req.symbol)
    quantity, lots, warning = convert_input_to_quantity(canonical, req.value, req.input_mode)
    display = format_quantity_display(canonical, quantity)
    
    return ConvertInputResponse(
        symbol=canonical,
        input_mode=req.input_mode,
        input_value=req.value,
        quantity=quantity,
        lots=lots,
        lot_size=get_lot_size(canonical),
        warning=warning,
        display=display,
    )


@router.post("/convert-quantity-to-lots", response_model=dict, summary="Convert exact quantity to lots + remainder")
async def convert_quantity_to_lots_endpoint(req: QuantityConvertRequest) -> dict:
    """Convert exact quantity to (full_lots, remainder) for display."""
    canonical, _ = resolve_symbol(req.symbol)
    full_lots, remainder = convert_quantity_to_lots(canonical, req.quantity)
    return {
        "symbol": canonical,
        "quantity": req.quantity,
        "lot_size": get_lot_size(canonical),
        "full_lots": full_lots,
        "remainder": remainder,
        "display": format_quantity_display(canonical, req.quantity),
    }


# ─── New Endpoints: Transaction Charges Calculator ───

@router.post("/calculate-charges", response_model=ChargesResponse, summary="Calculate Indian transaction charges for a trade")
async def calculate_charges_endpoint(req: ChargesRequest) -> ChargesResponse:
    """
    Calculate exact Indian transaction charges including:
    - Brokerage (₹20/order)
    - STT (Securities Transaction Tax)
    - Exchange Transaction Charges
    - GST (18% on brokerage + exchange charges)
    - Stamp Duty (0.003% on buy side)
    - SEBI Turnover Fee (₹1 per crore)
    - Slippage (default 0.1%)
    """
    charges = TransactionCharges.calculate_charges(
        symbol=req.symbol,
        side=req.side,
        quantity=req.quantity,
        price=req.price,
        product_type=req.product_type,
        order_type=req.order_type,
        exchange=req.exchange,
        is_option=req.is_option,
        is_future=req.is_future,
        slippage_pct=req.slippage_pct,
    )
    return ChargesResponse(**charges)


@router.post("/calculate-round-trip", response_model=RoundTripResponse, summary="Calculate charges for round-trip trade (buy + sell)")
async def calculate_round_trip_endpoint(req: RoundTripRequest) -> RoundTripResponse:
    """
    Calculate total charges and net PnL for a complete round trip.
    Useful for strategy backtesting and breakeven analysis.
    """
    result = TransactionCharges.calculate_round_trip(
        symbol=req.symbol,
        quantity=req.quantity,
        buy_price=req.buy_price,
        sell_price=req.sell_price,
        product_type=req.product_type,
        is_option=req.is_option,
        is_future=req.is_future,
        slippage_pct=req.slippage_pct,
    )
    
    # Convert to response models
    return RoundTripResponse(
        buy=ChargesResponse(**result["buy"]),
        sell=ChargesResponse(**result["sell"]),
        gross_pnl=result["gross_pnl"],
        total_charges=result["total_charges"],
        net_pnl=result["net_pnl"],
        charges_as_pct_of_gross=result["charges_as_pct_of_gross"],
        breakeven_price_diff=result["breakeven_price_diff"],
    )


@router.get("/charges-breakdown/{symbol}", summary="Get charges breakdown reference for a symbol")
async def charges_breakdown_endpoint(
    symbol: str,
    side: str = Query("BUY", pattern="^(BUY|SELL)$"),
    quantity: int = Query(1, ge=1),
    price: float = Query(100.0, gt=0),
    product_type: str = Query("INTRADAY", pattern="^(INTRADAY|CARRYFORWARD|DELIVERY|CNC|MIS|NRML)$"),
    is_option: bool = False,
    is_future: bool = False,
) -> dict:
    """Reference endpoint showing all charge components for a hypothetical trade."""
    canonical, _ = resolve_symbol(symbol)
    charges = TransactionCharges.calculate_charges(
        symbol=canonical,
        side=side,
        quantity=quantity,
        price=price,
        product_type=product_type,
        is_option=is_option,
        is_future=is_future,
    )
    
    # Add human-readable explanations
    explanations = {
        "brokerage": "Flat ₹20 per executed order (discount broker standard)",
        "stt": "Securities Transaction Tax - varies by instrument and side",
        "exchange_transaction_charge": "Exchange turnover fee (NSE/BSE/MCX)",
        "gst": "18% GST on (brokerage + exchange charges)",
        "stamp_duty": "0.003% on buy side (uniform across states)",
        "sebi_fee": "₹1 per crore turnover (0.0001%)",
        "slippage_cost": "Estimated 0.1% slippage (configurable)",
    }
    
    return {
        "symbol": canonical,
        "parameters": {
            "side": side,
            "quantity": quantity,
            "price": price,
            "product_type": product_type,
            "is_option": is_option,
            "is_future": is_future,
        },
        "charges": charges,
        "explanations": explanations,
    }