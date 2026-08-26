"""Webhook payload schemas for validation."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from typing import Any, Literal
from datetime import datetime


# Base webhook envelope
class WebhookEnvelope(BaseModel):
    """Standard envelope for all incoming webhooks"""
    event_id: str = Field(..., min_length=1, max_length=100)
    event_type: str = Field(..., min_length=1, max_length=100)
    timestamp: datetime
    provider: str = Field(..., min_length=1, max_length=50)
    payload: dict[str, Any]
    signature: str | None = None
    idempotency_key: str | None = Field(None, max_length=100)


# Provider-specific payload schemas
class RazorpayPaymentCapturedPayload(BaseModel):
    payment: dict[str, Any]
    order: dict[str, Any] | None = None


class ZerodhaPostbackPayload(BaseModel):
    order_id: str
    status: str
    tradingsymbol: str
    filled_quantity: int = 0
    average_price: float = 0.0
    checksum: str


class UpstoxOrderUpdatePayload(BaseModel):
    order_id: str
    status: str
    symbol: str
    filled_quantity: int
    average_price: float


class AngelOneOrderUpdatePayload(BaseModel):
    order_id: str
    status: str
    symbol: str
    filled_quantity: int
    average_price: float


class BinanceOrderUpdatePayload(BaseModel):
    order_id: str
    status: str
    symbol: str
    filled_quantity: int
    average_price: float


class TradeThronePayload(BaseModel):
    """TradeThrone signal webhook payload schema
    
    Supports JSON variables:
    - auth_token: Authentication token for the webhook
    - symbol: Trading symbol (e.g., NIFTY24AUG25000CE)
    - action: Trading action (BUY, SELL, BUY_TO_OPEN, SELL_TO_CLOSE, etc.)
    - quantity: Number of lots or contracts
    - price: Limit price (optional, for limit orders)
    - strategy_name: Name of the strategy generating the signal
    - signal_type: Type of signal (entry_long, entry_short, exit_long, exit_short, etc.)
    - order_type: Order type (MARKET, LIMIT, SL, SL-M)
    - product_type: Product type (INTRADAY, CARRYFORWARD, CO, OCO)
    - exchange: Exchange (NSE, BSE, NFO, BFO, MCX)
    - trigger_price: Trigger price for SL/SL-M orders
    - validity: Order validity (DAY, IOC, GTD)
    - tag: Custom tag for tracking
    """
    # Required fields
    signal: str = Field(..., description="Signal type: entry_long, entry_short, exit_long, exit_short, etc.")
    symbol: str = Field(..., min_length=1, max_length=50, description="Trading symbol")
    action: str = Field(..., description="Trading action: BUY, SELL, BUY_TO_OPEN, SELL_TO_CLOSE, etc.")
    
    # Optional fields with defaults
    quantity: int = Field(default=1, ge=1, description="Number of lots or contracts")
    price: float | None = Field(default=None, ge=0, description="Limit price (optional)")
    strategy_name: str | None = Field(default=None, max_length=100, description="Strategy name")
    signal_type: str | None = Field(default=None, max_length=50, description="Signal type classification")
    order_type: str = Field(default="MARKET", description="Order type: MARKET, LIMIT, SL, SL-M")
    product_type: str = Field(default="INTRADAY", description="Product type: INTRADAY, CARRYFORWARD, CO, OCO")
    exchange: str = Field(default="NFO", description="Exchange: NSE, BSE, NFO, BFO, MCX")
    trigger_price: float | None = Field(default=None, ge=0, description="Trigger price for SL/SL-M orders")
    validity: str = Field(default="DAY", description="Order validity: DAY, IOC, GTD")
    tag: str | None = Field(default=None, max_length=50, description="Custom tag for tracking")
    
    # Authentication (optional, can be in header instead)
    auth_token: str | None = Field(default=None, max_length=200, description="Authentication token")
    
    @field_validator('action')
    @classmethod
    def validate_action(cls, v: str) -> str:
        valid_actions = {'BUY', 'SELL', 'BUY_TO_OPEN', 'SELL_TO_CLOSE', 'BUY_TO_CLOSE', 'SELL_TO_OPEN'}
        v_upper = v.upper()
        if v_upper not in valid_actions:
            raise ValueError(f"Invalid action: {v}. Must be one of {valid_actions}")
        return v_upper
    
    @field_validator('signal')
    @classmethod
    def validate_signal(cls, v: str) -> str:
        valid_signals = {'entry_long', 'entry_short', 'exit_long', 'exit_short', 'reverse_long', 'reverse_short', 'close_all'}
        v_lower = v.lower()
        if v_lower not in valid_signals:
            raise ValueError(f"Invalid signal: {v}. Must be one of {valid_signals}")
        return v_lower
    
    @field_validator('order_type')
    @classmethod
    def validate_order_type(cls, v: str) -> str:
        valid_types = {'MARKET', 'LIMIT', 'SL', 'SL-M'}
        v_upper = v.upper()
        if v_upper not in valid_types:
            raise ValueError(f"Invalid order_type: {v}. Must be one of {valid_types}")
        return v_upper
    
    @field_validator('product_type')
    @classmethod
    def validate_product_type(cls, v: str) -> str:
        valid_types = {'INTRADAY', 'CARRYFORWARD', 'CO', 'OCO', 'MIS', 'NRML'}
        v_upper = v.upper()
        if v_upper not in valid_types:
            raise ValueError(f"Invalid product_type: {v}. Must be one of {valid_types}")
        return v_upper
    
    @field_validator('exchange')
    @classmethod
    def validate_exchange(cls, v: str) -> str:
        valid_exchanges = {'NSE', 'BSE', 'NFO', 'BFO', 'MCX', 'CDS'}
        v_upper = v.upper()
        if v_upper not in valid_exchanges:
            raise ValueError(f"Invalid exchange: {v}. Must be one of {valid_exchanges}")
        return v_upper
    
    @field_validator('validity')
    @classmethod
    def validate_validity(cls, v: str) -> str:
        valid_validity = {'DAY', 'IOC', 'GTD'}
        v_upper = v.upper()
        if v_upper not in valid_validity:
            raise ValueError(f"Invalid validity: {v}. Must be one of {valid_validity}")
        return v_upper


# Validation function
async def validate_webhook_payload(
    provider: str,
    event_type: str,
    payload: dict[str, Any]
) -> tuple[bool, str | None]:
    """Validate webhook payload against provider-specific schema"""
    validators = {
        ("razorpay", "payment.captured"): RazorpayPaymentCapturedPayload,
        ("zerodha", "order_update"): ZerodhaPostbackPayload,
        ("upstox", "order_update"): UpstoxOrderUpdatePayload,
        ("angel_one", "order_update"): AngelOneOrderUpdatePayload,
        ("binance", "order_update"): BinanceOrderUpdatePayload,
        ("tradethrone", "signal"): TradeThronePayload,
    }
    
    validator = validators.get((provider.lower(), event_type.lower()))
    if not validator:
        return True, None  # No schema defined, allow through
    
    try:
        validator(**payload)
        return True, None
    except Exception as e:
        return False, f"Schema validation failed: {e}"