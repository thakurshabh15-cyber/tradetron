"""Pre-trade risk management.

Enforces position limits, daily loss circuit breakers, and order rate
limiting.  The risk manager is consulted before every order is submitted
to the broker.
"""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal

from app.config import settings
from app.core.logging import get_logger
from app.schemas.trading import OrderRequest, RiskStatus

logger = get_logger("engine.risk")


class RiskManager:
    """Stateful pre-trade risk gate."""

    def __init__(self) -> None:
        self._daily_pnl: float = 0.0
        self._order_timestamps: deque[float] = deque()
        self._position_sizes: dict[str, int] = {}
        self._circuit_breaker: bool = False
        self._kill_switch: bool = False
        self._kill_switch_reason: str = ""

    # ── Public API ───────────────────────────────────────────────────

    def trigger_kill_switch(self, reason: str = "Manual Emergency Kill-Switch Activated") -> None:
        """Trigger emergency halt on all incoming order flow."""
        self._kill_switch = True
        self._kill_switch_reason = reason
        logger.critical("EMERGENCY KILL-SWITCH TRIGGERED: %s", reason)

    def reset_kill_switch(self) -> None:
        """Resume normal order flow after kill-switch inspection."""
        self._kill_switch = False
        self._kill_switch_reason = ""
        logger.info("Kill-switch released. Normal order routing resumed.")

    def check_margin(self, available_margin: float, required_margin: float) -> tuple[bool, str]:
        """Pre-trade margin gate ensuring sufficient collateral before order dispatch."""
        if available_margin < required_margin:
            return False, f"Insufficient margin: Required ₹{required_margin:,.2f}, Available ₹{available_margin:,.2f}"
        return True, "OK"

    def check(self, order: OrderRequest) -> tuple[bool, str]:
        """Return (allowed, reason).  ``allowed=False`` blocks the order."""
        if self._kill_switch:
            return False, f"Emergency Kill-Switch active: {self._kill_switch_reason}"

        if self._circuit_breaker:
            return False, "Circuit breaker active — daily loss limit hit"

        # 1. Daily loss check
        if self._daily_pnl <= -settings.max_daily_loss:
            self._circuit_breaker = True
            logger.critical(
                "CIRCUIT BREAKER TRIGGERED: daily P&L %.2f exceeds limit %.2f",
                self._daily_pnl,
                -settings.max_daily_loss,
            )
            return False, f"Daily loss limit exceeded ({self._daily_pnl:.2f})"

        # 2. Position size check
        current = self._position_sizes.get(order.symbol, 0)
        projected = current + order.quantity
        if projected > settings.max_position_size:
            return False, (
                f"Position limit: {order.symbol} would be {projected} "
                f"(max {settings.max_position_size})"
            )

        # 3. Order rate limit
        now = time.monotonic()
        self._prune_old_orders(now)
        if len(self._order_timestamps) >= settings.max_orders_per_minute:
            return False, (
                f"Rate limit: {len(self._order_timestamps)} orders in last 60s "
                f"(max {settings.max_orders_per_minute})"
            )

        return True, "OK"

    def record_fill(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        pnl: float = 0.0,
    ) -> None:
        """Update internal state after an order is filled."""
        now = time.monotonic()
        self._order_timestamps.append(now)

        if side == "BUY":
            self._position_sizes[symbol] = (
                self._position_sizes.get(symbol, 0) + quantity
            )
        else:
            self._position_sizes[symbol] = max(
                0, self._position_sizes.get(symbol, 0) - quantity
            )

        self._daily_pnl += pnl

    def reset_daily(self) -> None:
        """Reset daily counters (call at market open)."""
        self._daily_pnl = 0.0
        self._circuit_breaker = False
        self._order_timestamps.clear()
        logger.info("Daily risk counters reset")

    def get_status(self) -> RiskStatus:
        """Snapshot of current risk exposure."""
        self._prune_old_orders(time.monotonic())
        return RiskStatus(
            daily_pnl=Decimal(str(round(self._daily_pnl, 2))),
            max_daily_loss=Decimal(str(settings.max_daily_loss)),
            open_positions=sum(
                1 for q in self._position_sizes.values() if q > 0
            ),
            orders_this_minute=len(self._order_timestamps),
            max_orders_per_minute=settings.max_orders_per_minute,
            circuit_breaker_active=self._circuit_breaker,
        )

    # ── Internal ─────────────────────────────────────────────────────

    def _prune_old_orders(self, now: float) -> None:
        """Remove order timestamps older than 60 seconds."""
        cutoff = now - 60
        while self._order_timestamps and self._order_timestamps[0] < cutoff:
            self._order_timestamps.popleft()
