"""Marketplace and Strategy Deployment ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class MarketplaceStrategyRecord(Base):
    """Published marketplace strategy catalog item."""

    __tablename__ = "marketplace_strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    creator_name: Mapped[str] = mapped_column(String(100), default="QuantLab Pro")
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="Trend Following")
    pricing_type: Mapped[str] = mapped_column(String(20), default="FREE")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    min_capital: Mapped[float] = mapped_column(Float, default=5000.0)
    win_rate: Mapped[float] = mapped_column(Float, default=74.5)
    total_return_pct: Mapped[float] = mapped_column(Float, default=38.2)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=6.8)
    subscribers_count: Mapped[int] = mapped_column(Integer, default=142)
    rating: Mapped[float] = mapped_column(Float, default=4.9)
    symbols_json: Mapped[str] = mapped_column(Text, default="[\"AAPL\", \"MSFT\", \"NVDA\"]")
    strategy_config_json: Mapped[str] = mapped_column(Text, default="{}")
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class StrategyDeploymentRecord(Base):
    """Deployed instance of a marketplace or custom strategy."""

    __tablename__ = "strategy_deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    marketplace_strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    strategy_name: Mapped[str] = mapped_column(String(150), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), default="PAPER")
    broker_name: Mapped[str] = mapped_column(String(50), default="Simulated")
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    capital_allocated: Mapped[float] = mapped_column(Float, default=10000.0)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
