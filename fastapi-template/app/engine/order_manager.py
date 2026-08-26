"""Order & Position Lifecycle Manager.

Handles:
  - Mock broker connectivity and order dispatch
  - Pre-trade risk constraint validation (max position size, daily drawdown, max active positions)
  - In-flight position monitoring (Stop-Loss and Take-Profit automated exits on every tick)
  - Complete trade lifecycle state machine:
      SIGNAL -> RISK_CHECK -> BROKER_SUBMIT -> FILLED -> ACTIVE_POSITION ->
      (STOP_LOSS_EXIT | TAKE_PROFIT_EXIT | SIGNAL_EXIT) -> CLOSED
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from app.brokers.base import BrokerClient
from app.brokers.simulated import SimulatedBroker
from app.core.logging import get_logger
from app.schemas.trading import OrderRequest, Side

logger = get_logger("engine.order_manager")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Position:
    """Active open position with stop-loss and take-profit parameters."""

    position_id: str
    symbol: str
    side: Side
    quantity: int
    entry_price: float
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    status: str = "OPEN"  # OPEN, CLOSED
    opened_at: datetime = field(default_factory=_utcnow)
    closed_at: Optional[datetime] = None
    exit_reason: Optional[str] = None  # STOP_LOSS, TAKE_PROFIT, SIGNAL_REVERSAL, MANUAL

    def update_pnl(self, current_price: float) -> float:
        """Calculate and update unrealized PnL based on current market price."""
        if self.status != "OPEN":
            return self.realized_pnl

        if self.side == Side.BUY:
            self.unrealized_pnl = round((current_price - self.entry_price) * self.quantity, 2)
        else:
            self.unrealized_pnl = round((self.entry_price - current_price) * self.quantity, 2)
        return self.unrealized_pnl

    @property
    def pnl_pct(self) -> float:
        """Percentage return on position."""
        if self.entry_price == 0:
            return 0.0
        curr_pnl = self.realized_pnl if self.status == "CLOSED" else self.unrealized_pnl
        return round((curr_pnl / (self.entry_price * self.quantity)) * 100, 2)

    @property
    def duration_seconds(self) -> float:
        """Total duration of the position in seconds."""
        end_time = self.closed_at or _utcnow()
        return round((end_time - self.opened_at).total_seconds(), 2)


@dataclass
class TradeExecution:
    """Execution event record for trade lifecycle tracking."""

    trade_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    action_type: str  # ENTRY, STOP_LOSS_EXIT, TAKE_PROFIT_EXIT, REVERSAL_EXIT
    pnl: Optional[float] = None
    timestamp: datetime = field(default_factory=_utcnow)


class OrderManager:
    """Manages trade orders, positions, risk validation, and stop-loss/take-profit exits."""

    def __init__(
        self,
        broker: Optional[BrokerClient] = None,
        max_position_size: int = 100,
        default_stop_loss_pct: float = 0.02,  # 2% stop-loss
        default_take_profit_pct: float = 0.05,  # 5% take-profit
        max_daily_loss: float = 10_000.0,
    ) -> None:
        self.broker: BrokerClient = broker or SimulatedBroker()
        self.max_position_size = max_position_size
        self.default_stop_loss_pct = default_stop_loss_pct
        self.default_take_profit_pct = default_take_profit_pct
        self.max_daily_loss = max_daily_loss

        # Active open positions: symbol -> Position
        self.active_positions: dict[str, Position] = {}
        # History of closed positions
        self.closed_positions: list[Position] = []
        # Audit log of all executions
        self.execution_history: list[TradeExecution] = []
        # Cumulative realized PnL
        self.realized_pnl: float = 0.0

    # ── Risk Validation ──────────────────────────────────────────────────────────

    def validate_risk(
        self,
        symbol: str,
        side: Side,
        quantity: int,
    ) -> tuple[bool, str]:
        """Validate pre-trade risk constraints before placing an order."""
        # 1. Daily Loss Circuit Breaker
        if self.realized_pnl <= -self.max_daily_loss:
            return False, f"Circuit breaker active: Realized loss (${abs(self.realized_pnl):.2f}) exceeds max daily loss (${self.max_daily_loss:.2f})"

        # 2. Max Position Size Limit
        current_pos = self.active_positions.get(symbol)
        current_qty = current_pos.quantity if current_pos and current_pos.side == side else 0
        if current_qty + quantity > self.max_position_size:
            return False, f"Max position size exceeded: {symbol} projected {current_qty + quantity} > limit {self.max_position_size}"

        return True, "Risk validation passed"

    # ── Stop-Loss & Take-Profit Trigger Checks ───────────────────────────────────

    async def check_triggers(self, symbol: str, current_price: float) -> list[TradeExecution]:
        """Check open position for Stop-Loss or Take-Profit breaches and auto-exit."""
        position = self.active_positions.get(symbol)
        if not position or position.status != "OPEN":
            return []

        # Keep unrealized PnL current
        position.update_pnl(current_price)

        executions: list[TradeExecution] = []

        # 1. Stop-Loss Trigger Check
        is_stop_loss = False
        if position.stop_loss_price is not None:
            if position.side == Side.BUY and current_price <= position.stop_loss_price:
                is_stop_loss = True
            elif position.side == Side.SELL and current_price >= position.stop_loss_price:
                is_stop_loss = True

        if is_stop_loss:
            logger.warning(
                "STOP-LOSS TRIGGERED for %s %s @ %.2f (SL Threshold: %.2f)",
                position.side.value,
                symbol,
                current_price,
                position.stop_loss_price,
            )
            exec_event = await self._close_position(
                position=position,
                exit_price=current_price,
                exit_reason="STOP_LOSS",
                action_type="STOP_LOSS_EXIT",
            )
            if exec_event:
                executions.append(exec_event)
            return executions

        # 2. Take-Profit Trigger Check
        is_take_profit = False
        if position.take_profit_price is not None:
            if position.side == Side.BUY and current_price >= position.take_profit_price:
                is_take_profit = True
            elif position.side == Side.SELL and current_price <= position.take_profit_price:
                is_take_profit = True

        if is_take_profit:
            logger.info(
                "TAKE-PROFIT TRIGGERED for %s %s @ %.2f (TP Threshold: %.2f)",
                position.side.value,
                symbol,
                current_price,
                position.take_profit_price,
            )
            exec_event = await self._close_position(
                position=position,
                exit_price=current_price,
                exit_reason="TAKE_PROFIT",
                action_type="TAKE_PROFIT_EXIT",
            )
            if exec_event:
                executions.append(exec_event)

        return executions

    # ── Signal Processing & Lifecycle Execution ─────────────────────────────────

    async def process_signal(
        self,
        symbol: str,
        signal: str,  # "BUY" or "SELL"
        current_price: float,
        quantity: int = 10,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
    ) -> Optional[TradeExecution]:
        """Process incoming trade signal through full risk check, broker fill, and position creation."""
        target_side = Side.BUY if signal.upper() == "BUY" else Side.SELL
        sl_pct = stop_loss_pct if stop_loss_pct is not None else self.default_stop_loss_pct
        tp_pct = take_profit_pct if take_profit_pct is not None else self.default_take_profit_pct

        # 1. If we hold an existing position on this symbol:
        current_pos = self.active_positions.get(symbol)
        if current_pos and current_pos.status == "OPEN":
            if current_pos.side == target_side:
                # Already positioned in signal direction
                logger.debug(
                    "Position already aligned with signal %s for %s, skipping entry",
                    signal,
                    symbol,
                )
                return None
            else:
                # Signal reversal: Close current opposite position first
                logger.info(
                    "Signal reversal detected for %s. Closing existing %s position before entering %s",
                    symbol,
                    current_pos.side.value,
                    target_side.value,
                )
                await self._close_position(
                    position=current_pos,
                    exit_price=current_price,
                    exit_reason="SIGNAL_REVERSAL",
                    action_type="REVERSAL_EXIT",
                )

        # 2. Validate Pre-Trade Risk Constraints
        allowed, reason = self.validate_risk(symbol, target_side, quantity)
        if not allowed:
            logger.warning("ORDER REJECTED by Risk Manager: %s", reason)
            return None

        # 3. Update simulated price if applicable
        if hasattr(self.broker, "update_price"):
            self.broker.update_price(symbol, current_price)

        # 4. Submit Order to Broker
        order_req = OrderRequest(
            symbol=symbol,
            side=target_side,
            quantity=quantity,
            order_type="MARKET",
        )

        try:
            fill_result = await self.broker.place_order(order_req)
        except Exception as exc:
            from app.core.monitoring import monitoring_sentinel
            monitoring_sentinel.capture_order_failure(
                user_id=0,
                order_id=str(uuid.uuid4()),
                symbol=symbol,
                broker=getattr(self.broker, "broker_name", "UNKNOWN"),
                reason=str(exc),
                price=current_price,
                quantity=quantity,
                side=target_side.value,
                raw_error=str(exc),
            )
            logger.error("Broker execution failed for %s %s: %s", target_side.value, symbol, exc)
            return None

        filled_price = float(fill_result.get("filled_price", current_price))
        broker_order_id = str(fill_result.get("broker_order_id", uuid.uuid4().hex[:10]))

        # 5. Calculate Stop-Loss and Take-Profit Thresholds
        if target_side == Side.BUY:
            sl_price = round(filled_price * (1.0 - sl_pct), 2)
            tp_price = round(filled_price * (1.0 + tp_pct), 2)
        else:
            sl_price = round(filled_price * (1.0 + sl_pct), 2)
            tp_price = round(filled_price * (1.0 - tp_pct), 2)

        # 6. Open and Track New Active Position
        position = Position(
            position_id=str(uuid.uuid4()),
            symbol=symbol,
            side=target_side,
            quantity=quantity,
            entry_price=filled_price,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
        )
        self.active_positions[symbol] = position

        # 7. Record Execution Event
        execution = TradeExecution(
            trade_id=str(uuid.uuid4()),
            order_id=broker_order_id,
            symbol=symbol,
            side=target_side,
            quantity=quantity,
            price=filled_price,
            action_type="ENTRY",
        )
        self.execution_history.append(execution)

        logger.info(
            "POSITION OPENED: %s %s %d @ %.2f | SL=%.2f (%.1f%%), TP=%.2f (%.1f%%)",
            target_side.value,
            symbol,
            quantity,
            filled_price,
            sl_price,
            sl_pct * 100,
            tp_price,
            tp_pct * 100,
        )

        return execution

    # ── Internal Position Closure ────────────────────────────────────────────────

    async def _close_position(
        self,
        position: Position,
        exit_price: float,
        exit_reason: str,
        action_type: str,
    ) -> Optional[TradeExecution]:
        """Close an active position, calculate realized PnL, and dispatch exit order to broker."""
        close_side = Side.SELL if position.side == Side.BUY else Side.BUY

        # Sync simulated price if applicable
        if hasattr(self.broker, "update_price"):
            self.broker.update_price(position.symbol, exit_price)

        order_req = OrderRequest(
            symbol=position.symbol,
            side=close_side,
            quantity=position.quantity,
            order_type="MARKET",
        )

        try:
            fill_result = await self.broker.place_order(order_req)
        except Exception as exc:
            logger.error("Failed to execute close order on broker: %s", exc)
            return None

        actual_exit_price = float(fill_result.get("filled_price", exit_price))
        broker_order_id = str(fill_result.get("broker_order_id", uuid.uuid4().hex[:10]))

        # Calculate Final Realized PnL
        if position.side == Side.BUY:
            pnl = round((actual_exit_price - position.entry_price) * position.quantity, 2)
        else:
            pnl = round((position.entry_price - actual_exit_price) * position.quantity, 2)

        position.realized_pnl = pnl
        position.unrealized_pnl = 0.0
        position.status = "CLOSED"
        position.closed_at = _utcnow()
        position.exit_reason = exit_reason

        self.realized_pnl += pnl

        # ── TradeThrone Auto-Pilot Risk Guard hook ───────────────────
        # Feed closed-trade P&L into the engine risk manager so the
        # auto-pilot can trip the kill-switch on loss streaks/drawdown.
        # Defensive: risk_manager may not be wired in standalone usage.
        _rm = getattr(self, "risk_manager", None)
        if _rm is not None and hasattr(_rm, "record_trade_result"):
            try:
                _rm.record_trade_result(pnl)
            except Exception as exc:  # never break trade flow
                logger.debug("Auto-pilot risk feed skipped: %s", exc)

        # Move from active to closed
        self.active_positions.pop(position.symbol, None)
        self.closed_positions.append(position)

        # Record Execution
        execution = TradeExecution(
            trade_id=str(uuid.uuid4()),
            order_id=broker_order_id,
            symbol=position.symbol,
            side=close_side,
            quantity=position.quantity,
            price=actual_exit_price,
            action_type=action_type,
            pnl=pnl,
        )
        self.execution_history.append(execution)

        logger.info(
            "POSITION CLOSED [%s]: %s %s %d @ %.2f (Entry: %.2f) | PnL: $%+.2f | Total Realized: $%+.2f",
            exit_reason,
            close_side.value,
            position.symbol,
            position.quantity,
            actual_exit_price,
            position.entry_price,
            pnl,
            self.realized_pnl,
        )

        return execution

    # ── Summary & Metrics ────────────────────────────────────────────────────────

    def get_portfolio_summary(self) -> dict[str, Any]:
        """Return a snapshot of active positions, total realized/unrealized PnL, and trade stats."""
        unrealized = sum(p.unrealized_pnl for p in self.active_positions.values())
        return {
            "active_positions_count": len(self.active_positions),
            "closed_positions_count": len(self.closed_positions),
            "total_trades_count": len(self.execution_history),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(unrealized, 2),
            "net_pnl": round(self.realized_pnl + unrealized, 2),
            "active_positions": {
                sym: {
                    "side": pos.side.value,
                    "quantity": pos.quantity,
                    "entry_price": pos.entry_price,
                    "stop_loss_price": pos.stop_loss_price,
                    "take_profit_price": pos.take_profit_price,
                    "unrealized_pnl": pos.unrealized_pnl,
                }
                for sym, pos in self.active_positions.items()
            },
        }
