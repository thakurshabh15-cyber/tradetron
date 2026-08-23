"""No-code option strategy evaluation and multi-leg execution primitives."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from app.schemas.trading import OrderRequest, Side


class VisualStrategyEngine:
    """Evaluate visual rules and execute option legs through a broker client."""

    def __init__(self, history_size: int = 200) -> None:
        self._prices: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history_size))
        self._vwap: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
        self._previous: dict[str, float] = {}

    def update_tick(self, tick: dict[str, Any]) -> None:
        symbol = str(tick["symbol"]).upper()
        price = float(tick["price"])
        volume = float(tick.get("volume", 0) or 0)
        self._prices[symbol].append(price)
        value, total_volume = self._vwap[symbol]
        self._vwap[symbol] = (value + price * volume, total_volume + volume)

    def evaluate_conditions(
        self, strategy_id: str, conditions: list[dict[str, Any]], tick: dict[str, Any]
    ) -> bool:
        """Return True when every visual entry condition matches the current tick."""
        self.update_tick(tick)
        symbol = str(tick["symbol"]).upper()
        current_price = float(tick["price"])
        now = tick.get("timestamp") or datetime.now()
        if isinstance(now, str):
            now = datetime.fromisoformat(now.replace("Z", "+00:00"))

        for condition in conditions:
            indicator = str(condition.get("indicator", "PRICE")).upper()
            operator = str(condition.get("operator", "gt")).lower()
            if indicator == "TIME":
                if str(condition.get("value", "09:20")) != now.strftime("%H:%M"):
                    return False
                continue
            threshold = float(condition.get("value", 0))
            period = max(2, int(condition.get("period", 14)))
            value = self._indicator(symbol, indicator, period, current_price)
            if value is None:
                return False
            previous = self._previous.get(f"{strategy_id}:{indicator}")
            matched = self._compare(operator, value, threshold, previous)
            self._previous[f"{strategy_id}:{indicator}"] = value
            if not matched:
                return False
        return True

    def _indicator(self, symbol: str, indicator: str, period: int, price: float) -> float | None:
        prices = list(self._prices[symbol])
        if indicator == "TIME":
            return 0.0
        if indicator in {"PRICE", "VWAP"}:
            if indicator == "VWAP":
                total, volume = self._vwap[symbol]
                return total / volume if volume else price
            return price
        if len(prices) < period:
            return None
        if indicator == "RSI":
            changes = [prices[i] - prices[i - 1] for i in range(len(prices) - period, len(prices))]
            gains = sum(max(change, 0) for change in changes)
            losses = sum(max(-change, 0) for change in changes)
            return 100.0 if losses == 0 else 100 - (100 / (1 + gains / losses))
        if indicator == "SMA":
            return sum(prices[-period:]) / period
        return None

    @staticmethod
    def _compare(operator: str, current: float, threshold: float, previous: float | None) -> bool:
        if operator in {"gt", ">"}: return current > threshold
        if operator in {"gte", ">="}: return current >= threshold
        if operator in {"lt", "<"}: return current < threshold
        if operator in {"lte", "<="}: return current <= threshold
        if operator in {"cross_above", "crossover"}: return previous is not None and previous <= threshold < current
        if operator == "cross_below": return previous is not None and previous >= threshold > current
        return abs(current - threshold) < 1e-9

    async def execute_legs(
        self, broker: Any, underlying: str, legs: list[dict[str, Any]], lot_size: int = 1
    ) -> list[dict[str, Any]]:
        """Submit every configured CE/PE leg and return normalized broker fills."""
        fills = []
        for leg in legs:
            option_symbol = f"{underlying.upper()}-{leg['strike']}-{leg['type'].upper()}"
            request = OrderRequest(
                symbol=option_symbol,
                side=Side.BUY if str(leg["action"]).upper() == "BUY" else Side.SELL,
                quantity=max(1, int(leg.get("lots", 1)) * lot_size),
                order_type="MARKET",
            )
            result = await broker.place_order(request)
            fills.append({"symbol": option_symbol, "action": leg["action"], "quantity": request.quantity, **result})
        return fills

    @staticmethod
    def should_exit(entry_value: float, current_value: float, exit_conditions: dict[str, Any]) -> bool:
        """Apply aggregate target-profit and max-loss rules to a strategy position."""
        pnl = current_value - entry_value
        target = float(exit_conditions.get("target_profit", 0) or 0)
        max_loss = float(exit_conditions.get("max_loss", 0) or 0)
        return (target > 0 and pnl >= target) or (max_loss > 0 and pnl <= -max_loss)


visual_strategy_engine = VisualStrategyEngine()