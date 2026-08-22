"""Pydantic request / response schemas for the trading platform.

These are the API-facing data contracts — kept separate from SQLAlchemy ORM
models so the boundary between HTTP layer and persistence is explicit.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ── Enums ────────────────────────────────────────────────────────────────────


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Indicator(str, Enum):
    PRICE = "PRICE"
    RSI = "RSI"
    SMA = "SMA"
    EMA = "EMA"


class Operator(str, Enum):
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    EQ = "eq"
    CROSS_ABOVE = "cross_above"
    CROSS_BELOW = "cross_below"


# ── Strategy schemas ─────────────────────────────────────────────────────────


from pydantic import BaseModel, ConfigDict, Field, model_validator


class Condition(BaseModel):
    """A single indicator-based condition in a strategy rule."""

    indicator: Indicator = Field(default=Indicator.PRICE)
    operator: Operator = Field(default=Operator.GT)
    value: Decimal = Field(default=Decimal("0.0"), ge=0, description="Comparison threshold")
    period: int = Field(default=14, ge=1, le=1000, description="Lookback period")

    @model_validator(mode="before")
    @classmethod
    def normalize_condition(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            if "value" not in d and "threshold" in d:
                d["value"] = d["threshold"]
            if "value" not in d or d["value"] is None:
                d["value"] = 0.0
            if "indicator" in d:
                ind_str = str(d["indicator"]).upper()
                if "SMA" in ind_str:
                    d["indicator"] = "SMA"
                elif "EMA" in ind_str:
                    d["indicator"] = "EMA"
                elif "RSI" in ind_str:
                    d["indicator"] = "RSI"
                elif "PRICE" in ind_str:
                    d["indicator"] = "PRICE"
                else:
                    d["indicator"] = "PRICE"
            if "operator" in d:
                op_str = str(d["operator"]).lower()
                op_map = {">": "gt", "<": "lt", ">=": "gte", "<=": "lte", "=": "eq", "==": "eq"}
                d["operator"] = op_map.get(op_str, op_str)
            return d
        return data


class Action(BaseModel):
    """What to do when a strategy triggers."""

    side: Side
    quantity: int = Field(..., gt=0)
    order_type: str = Field(default="MARKET", pattern="^(MARKET|LIMIT)$")


class StrategyCreate(BaseModel):
    """Payload for creating a new strategy."""

    name: str = Field(..., min_length=1, max_length=100)
    symbols: list[str] = Field(..., min_length=1)
    conditions: list[Condition] = Field(..., min_length=1)
    action: Action
    enabled: bool = False
    execution_mode: str = Field("PAPER", description="PAPER or LIVE")
    broker_account_id: Optional[str] = None
    capital_allocated: float = Field(10000.0, ge=100.0)


class StrategyUpdate(BaseModel):
    """Partial update payload for a strategy."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    symbols: Optional[list[str]] = Field(None, min_length=1)
    conditions: Optional[list[Condition]] = Field(None, min_length=1)
    action: Optional[Action] = None
    enabled: Optional[bool] = None
    execution_mode: Optional[str] = None
    broker_account_id: Optional[str] = None
    capital_allocated: Optional[float] = None


class StrategyRead(BaseModel):
    """Strategy data returned from the API."""

    id: str
    name: str
    symbols: list[str]
    conditions: list[Condition]
    action: Action
    enabled: bool
    execution_mode: str = "PAPER"
    broker_account_id: Optional[str] = None
    capital_allocated: float = 10000.0
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ── Market data ──────────────────────────────────────────────────────────────


class Tick(BaseModel):
    """A single price tick from the market data feed."""

    symbol: str
    price: Decimal
    change: Decimal = Decimal("0")
    change_pct: Decimal = Decimal("0")
    volume: int = 0
    timestamp: datetime


class MarketSnapshot(BaseModel):
    """Current market data for the REST endpoint."""

    timestamp: datetime
    market: list[Tick]


# ── Order / Trade ────────────────────────────────────────────────────────────


class OrderRequest(BaseModel):
    """Internal order request schema."""

    symbol: str
    side: Side
    quantity: int = Field(..., gt=0)
    order_type: str = "MARKET"
    strategy_id: Optional[str] = None


class TradeRead(BaseModel):
    """Trade record returned from the API."""

    id: str
    order_id: Optional[str] = None
    strategy_name: Optional[str] = None
    symbol: str
    side: Side
    quantity: int
    price: Decimal
    pnl: Optional[Decimal] = None
    executed_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TradeStats(BaseModel):
    """Aggregated trade statistics."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    win_rate: float = 0.0


# ── Risk ─────────────────────────────────────────────────────────────────────


class RiskStatus(BaseModel):
    """Current risk exposure snapshot."""

    daily_pnl: Decimal = Decimal("0")
    max_daily_loss: Decimal = Decimal("0")
    open_positions: int = 0
    orders_this_minute: int = 0
    max_orders_per_minute: int = 0
    circuit_breaker_active: bool = False
