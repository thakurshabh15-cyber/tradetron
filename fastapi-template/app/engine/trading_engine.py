"""Core trading engine & strategy executor.

Features:
  - ``OrderManager``: Pre-trade risk validation, position tracking, stop-loss/take-profit
    automated lifecycle management, and mock broker dispatch.
  - ``StrategyExecutor``: High-performance SMA crossover strategy (50/200) with bounded
    `collections.deque` buffers, integrated with `OrderManager` for end-to-end trade execution.
  - ``TradingEngine``: Async strategy orchestrator integrating market data feeds,
    risk management gates, broker execution, SQLite persistence, and real-time WebSocket broadcasting.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from decimal import Decimal
from typing import Any, Optional

from app.brokers import BrokerModeBlockedError, assert_live_dispatch_allowed, get_broker_adapter
from app.brokers.base import BrokerClient
from app.brokers.simulated import SimulatedBroker
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.engine.order_manager import OrderManager, Position, TradeExecution
from app.engine.risk_manager import RiskManager
from app.engine.strategy_evaluator import StrategyEvaluator
from app.market_data.manager import ws_manager
from app.models.trading import OrderRecord, StrategyRecord, TradeRecord
from app.schemas.trading import OrderRequest, Side

logger = get_logger("engine.core")


class StrategyExecutor:
    """High-performance SMA Crossover (50/200) strategy executor with Trade Lifecycle Management.

    Uses `collections.deque` with bounded maxlen for fast O(1) price ingestion
    and rolling average calculations. Connects directly with `OrderManager` to validate
    risk constraints and execute the complete trade lifecycle (Entries, Stop-Loss, Take-Profit).
    """

    def __init__(
        self,
        fast_period: int = 50,
        slow_period: int = 200,
        buffer_size: int = 300,
        order_manager: Optional[OrderManager] = None,
        trade_quantity: int = 10,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.05,
    ) -> None:
        if fast_period >= slow_period:
            raise ValueError("fast_period must be strictly less than slow_period")

        self.fast_period = fast_period
        self.slow_period = slow_period
        self.buffer_size = max(buffer_size, slow_period + 50)
        self.trade_quantity = trade_quantity
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

        # Integrated Order & Risk Lifecycle Manager
        self.order_manager = order_manager or OrderManager(
            default_stop_loss_pct=stop_loss_pct,
            default_take_profit_pct=take_profit_pct,
        )

        # symbol -> deque of price history with fixed bounded capacity
        self.price_buffers: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.buffer_size)
        )

        # symbol -> previous SMA comparison state ('above', 'below', or None)
        self.crossover_states: dict[str, Optional[str]] = {}

        # symbol -> (prev_fast_sma, prev_slow_sma)
        self.last_smas: dict[str, tuple[Optional[float], Optional[float]]] = {}

    def push_price(self, symbol: str, price: float) -> None:
        """Append the latest price to the symbol's bounded deque buffer."""
        self.price_buffers[symbol].append(float(price))

    def calculate_sma(self, symbol: str, period: int) -> Optional[float]:
        """Compute the Simple Moving Average over the specified period using deque slice."""
        buffer = self.price_buffers.get(symbol)
        if not buffer or len(buffer) < period:
            return None

        # Slice the last `period` elements from the deque
        recent_prices = [buffer[i] for i in range(len(buffer) - period, len(buffer))]
        return sum(recent_prices) / period

    async def on_tick(self, symbol: str, price: float) -> dict[str, Any]:
        """Process incoming price tick, evaluate stop-loss triggers, SMA crossover, and execute trades.

        Workflow:
          1. Check open position for Stop-Loss and Take-Profit breach (auto-exit if triggered).
          2. Ingest price into bounded deque.
          3. Calculate 50 and 200 period SMAs.
          4. Detect Golden Cross (BUY) or Death Cross (SELL).
          5. Dispatch signals through `OrderManager` to validate risk and open/reverse positions.

        Returns:
            Dict containing signal, trigger_executions, new_execution, and indicator state.
        """
        current_price = float(price)
        result: dict[str, Any] = {
            "symbol": symbol,
            "price": current_price,
            "signal": None,
            "trigger_executions": [],
            "new_execution": None,
            "active_position": None,
        }

        # ── Step 1: Check In-Flight Stop-Loss & Take-Profit Triggers ────────────────
        trigger_exits = await self.order_manager.check_triggers(symbol, current_price)
        if trigger_exits:
            result["trigger_executions"] = trigger_exits

        # ── Step 2: Update Price History in Deque ────────────────────────────────────
        self.push_price(symbol, current_price)

        # ── Step 3: Compute Fast and Slow SMAs ──────────────────────────────────────
        fast_sma = self.calculate_sma(symbol, self.fast_period)
        slow_sma = self.calculate_sma(symbol, self.slow_period)

        if fast_sma is None or slow_sma is None:
            # Insufficient data for 200 SMA lookback
            result["active_position"] = self.order_manager.active_positions.get(symbol)
            return result

        prev_state = self.crossover_states.get(symbol)
        current_state = "above" if fast_sma > slow_sma else "below"

        signal: Optional[str] = None

        # ── Step 4: Evaluate Crossover Signals ──────────────────────────────────────
        if prev_state is not None:
            # Golden Cross: Fast crosses above Slow -> BUY
            if prev_state == "below" and current_state == "above":
                signal = "BUY"
                logger.info(
                    "GOLDEN CROSS detected for %s: 50-SMA (%.2f) crossed ABOVE 200-SMA (%.2f)",
                    symbol,
                    fast_sma,
                    slow_sma,
                )
            # Death Cross: Fast crosses below Slow -> SELL
            elif prev_state == "above" and current_state == "below":
                signal = "SELL"
                logger.info(
                    "DEATH CROSS detected for %s: 50-SMA (%.2f) crossed BELOW 200-SMA (%.2f)",
                    symbol,
                    fast_sma,
                    slow_sma,
                )

        # Update state trackers
        self.crossover_states[symbol] = current_state
        self.last_smas[symbol] = (fast_sma, slow_sma)
        result["signal"] = signal

        # ── Step 5: Route Signal Through OrderManager for Execution ─────────────────
        if signal:
            execution = await self.order_manager.process_signal(
                symbol=symbol,
                signal=signal,
                current_price=current_price,
                quantity=self.trade_quantity,
                stop_loss_pct=self.stop_loss_pct,
                take_profit_pct=self.take_profit_pct,
            )
            result["new_execution"] = execution

        result["active_position"] = self.order_manager.active_positions.get(symbol)
        return result

    def get_indicator_summary(self, symbol: str) -> dict[str, Any]:
        """Return diagnostic metrics for a symbol's current moving averages and active position."""
        buffer = self.price_buffers.get(symbol)
        count = len(buffer) if buffer else 0
        fast_sma = self.calculate_sma(symbol, self.fast_period)
        slow_sma = self.calculate_sma(symbol, self.slow_period)
        active_pos = self.order_manager.active_positions.get(symbol)

        return {
            "symbol": symbol,
            "buffer_count": count,
            "required_count": self.slow_period,
            "ready": count >= self.slow_period,
            f"sma_{self.fast_period}": round(fast_sma, 4) if fast_sma is not None else None,
            f"sma_{self.slow_period}": round(slow_sma, 4) if slow_sma is not None else None,
            "current_state": self.crossover_states.get(symbol),
            "has_active_position": active_pos is not None,
            "active_position": {
                "side": active_pos.side.value,
                "quantity": active_pos.quantity,
                "entry_price": active_pos.entry_price,
                "stop_loss_price": active_pos.stop_loss_price,
                "take_profit_price": active_pos.take_profit_price,
                "unrealized_pnl": active_pos.unrealized_pnl,
            }
            if active_pos
            else None,
        }


class TradingEngine:
    """Main trading engine orchestrator."""

    def __init__(
        self,
        broker: BrokerClient,
        tick_queue: asyncio.Queue,
    ) -> None:
        self._broker = broker
        self._tick_queue = tick_queue
        self._evaluator = StrategyEvaluator()
        self._order_manager = OrderManager(
            broker=broker,
            max_position_size=100,
            default_stop_loss_pct=0.02,
            default_take_profit_pct=0.05,
        )
        self._sma_executor = StrategyExecutor(
            fast_period=50,
            slow_period=200,
            order_manager=self._order_manager,
            trade_quantity=10,
        )
        self._risk = RiskManager()
        self._task: asyncio.Task | None = None
        self._running = False

        # In-memory cache of active user-defined strategies
        self._strategies: dict[str, dict[str, Any]] = {}

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load strategies from DB and begin processing ticks."""
        await self._load_strategies()
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info(
            "Trading engine started — %d dynamic strategies loaded, SMA(50/200) StrategyExecutor & OrderManager active",
            len(self._strategies),
        )

    async def stop(self) -> None:
        """Gracefully shut down the engine."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Trading engine stopped")

    # ── Strategy management ──────────────────────────────────────────

    async def _load_strategies(self) -> None:
        """Load active strategies from the database into memory."""
        try:
            async with SessionLocal() as session:
                from sqlalchemy import select

                result = await session.execute(
                    select(StrategyRecord).where(StrategyRecord.enabled == True)  # noqa: E712
                )
                rows = result.scalars().all()

                self._strategies.clear()
                for row in rows:
                    self._strategies[row.id] = {
                        "id": row.id,
                        "user_id": row.user_id,
                        "name": row.name,
                        "symbols": json.loads(row.symbols_json),
                        "conditions": json.loads(row.conditions_json),
                        "action": json.loads(row.action_json),
                        "enabled": row.enabled,
                        "execution_mode": getattr(row, "execution_mode", "PAPER") or "PAPER",
                        "broker_account_id": getattr(row, "broker_account_id", None),
                        "capital_allocated": getattr(row, "capital_allocated", 10000.0),
                    }
        except Exception as exc:
            logger.warning("Notice on loading active strategies from DB: %s", exc)

    async def reload_strategies(self) -> None:
        """Reload strategies from DB (called after CRUD operations)."""
        await self._load_strategies()
        logger.info("Strategies reloaded — %d active", len(self._strategies))

    @property
    def risk_manager(self) -> RiskManager:
        """Expose risk manager for API endpoints."""
        return self._risk

    @property
    def order_manager(self) -> OrderManager:
        """Expose OrderManager for position and risk lifecycle queries."""
        return self._order_manager

    @property
    def sma_executor(self) -> StrategyExecutor:
        """Expose SMA StrategyExecutor."""
        return self._sma_executor

    # ── Core tick processing loop ────────────────────────────────────

    async def _tick_loop(self) -> None:
        """Main event loop — consumes ticks and evaluates strategies."""
        try:
            while self._running:
                try:
                    tick = await asyncio.wait_for(
                        self._tick_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.05)
                    continue

                symbol = tick["symbol"]
                price = float(tick["price"])

                # 1. Update StrategyEvaluator price history
                self._evaluator.update_price(symbol, price)

                # 2. Run StrategyExecutor with integrated OrderManager (Stop-loss, Take-profit, SMA Crossover)
                exec_result = await self._sma_executor.on_tick(symbol, price)

                # Persist and broadcast any executions generated by OrderManager
                all_executions = (
                    exec_result.get("trigger_executions", [])
                    + ([exec_result["new_execution"]] if exec_result.get("new_execution") else [])
                )
                for exc in all_executions:
                    trade_data = await self._persist_trade(
                        strategy_id="builtin-sma-crossover",
                        strategy_name=f"SMA 50/200 ({symbol} {exc.action_type})",
                        symbol=symbol,
                        side=exc.side.value,
                        quantity=exc.quantity,
                        price=exc.price,
                        broker_order_id=exc.order_id,
                        pnl=exc.pnl,
                        mode="PAPER",
                    )
                    from app.engine.alerts import notify_sl_tp, notify_trade_fill
                    if exc.action_type in ("STOP_LOSS_EXIT", "TAKE_PROFIT_EXIT"):
                        await notify_sl_tp(
                            trade_data.get("user_id"),
                            event=exc.action_type.replace("_", " "),
                            symbol=exc.symbol,
                            side=exc.side.value,
                            quantity=exc.quantity,
                            price=exc.price,
                            mode="PAPER",
                            pnl=exc.pnl,
                        )
                    else:
                        await notify_trade_fill(
                            trade_data.get("user_id"),
                            symbol=exc.symbol,
                            side=exc.side.value,
                            quantity=exc.quantity,
                            price=exc.price,
                            mode="PAPER",
                        )
                    await ws_manager.broadcast("trades", trade_data)

                # 3. Evaluate custom user-defined strategies watching this symbol
                for strat_id, strat in list(self._strategies.items()):
                    if not strat["enabled"]:
                        continue
                    if symbol not in strat["symbols"]:
                        continue

                    triggered = self._evaluator.evaluate(
                        strat_id,
                        symbol,
                        strat["conditions"],
                    )

                    if triggered:
                        await self._execute_signal(strat, symbol, price)

                # Yield control briefly to ensure HTTP requests never queue up
                await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            logger.debug("Tick loop cancelled")
            raise

    # ── Signal execution pipeline for custom strategies ─────────────

    async def _execute_signal(
        self,
        strategy: dict[str, Any],
        symbol: str,
        price: float,
    ) -> None:
        """Process a triggered custom strategy signal through risk -> broker -> persist."""
        action = strategy["action"]
        side_str = action["side"]
        quantity = int(action["quantity"])
        mode = strategy.get("execution_mode", "PAPER")
        broker_account_id = strategy.get("broker_account_id")
        user_id = strategy.get("user_id")

        order_req = OrderRequest(
            symbol=symbol,
            side=Side(side_str),
            quantity=quantity,
            order_type=action.get("order_type", "MARKET"),
            strategy_id=strategy.get("id"),
        )

        # ── 1. Global Risk & Kill-Switch Gate ────────────────────────
        allowed, reason = self._risk.check(order_req)
        if not allowed:
            logger.warning(
                "[%s] RISK/KILL-SWITCH BLOCKED: %s %s %d — %s",
                mode,
                side_str,
                symbol,
                quantity,
                reason,
            )
            # Record blocked order in DB
            await self._persist_rejected_order(
                strategy_id=strategy.get("id"),
                user_id=user_id,
                broker_account_id=broker_account_id,
                symbol=symbol,
                side=side_str,
                quantity=quantity,
                price=price,
                mode=mode,
                reason=reason,
            )
            await ws_manager.broadcast("trades", {
                "event": "order_rejected",
                "strategy_id": strategy.get("id"),
                "symbol": symbol,
                "reason": reason,
                "mode": mode,
            })
            return

        # ── 2. Live Multi-Broker Client & Margin Gating ──────────────
        target_broker = self._broker
        if mode == "LIVE":
            from app.models.broker_account import BrokerAccountRecord
            from app.brokers.zerodha import ZerodhaKiteBroker
            from app.brokers.upstox import UpstoxBroker
            from app.brokers.angelone import AngelOneBroker
            from app.brokers.binance import BinanceBroker
            from sqlalchemy import select

            # LIVE orders must always be scoped to an owning user and a broker
            # account that the SAME user owns.  Falling back to any connected
            # account in the database would trade with another user's money —
            # a multi-tenant safety violation — so no such fallback exists.
            if not user_id:
                err_msg = "Live execution requires an authenticated owner (user_id missing)"
                logger.error("[LIVE] Order blocked: %s", err_msg)
                await self._persist_rejected_order(strategy.get("id"), user_id, broker_account_id, symbol, side_str, quantity, price, mode, err_msg)
                return

            async with SessionLocal() as session:
                if broker_account_id:
                    stmt = select(BrokerAccountRecord).where(
                        BrokerAccountRecord.id == broker_account_id,
                        BrokerAccountRecord.user_id == user_id,
                    )
                else:
                    stmt = select(BrokerAccountRecord).where(
                        BrokerAccountRecord.user_id == user_id,
                        BrokerAccountRecord.status == "CONNECTED",
                    )

                res = await session.execute(stmt)
                broker_rec = res.scalar_one_or_none()

            if not broker_rec:
                err_msg = "No active broker account linked for Live execution"
                logger.error("[LIVE] Order blocked: %s", err_msg)
                await self._persist_rejected_order(strategy.get("id"), user_id, broker_account_id, symbol, side_str, quantity, price, mode, err_msg)
                return

            if broker_rec.is_token_expired():
                err_msg = f"Live broker token for {broker_rec.broker_name} has expired"
                logger.error("[LIVE] Order blocked: %s", err_msg)
                await self._persist_rejected_order(strategy.get("id"), user_id, broker_account_id, symbol, side_str, quantity, price, mode, err_msg)
                return

            # Instantiate broker client with decrypted credentials
            target_broker = get_broker_adapter(broker_rec)

            # ── PHASE-3 LIVE/Paper separation guard ──────────────────────
            # Even with a connected broker, LIVE execution must be gated by
            # BROKER_MODE=live.  If not live, reject the order (never dispatch
            # blind against real funds in a simulated/scoped deployment).
            try:
                assert_live_dispatch_allowed()
            except BrokerModeBlockedError as guard_exc:
                logger.error("[LIVE] Order blocked by broker-mode guard: %s", guard_exc)
                await self._persist_rejected_order(
                    strategy.get("id"), user_id, broker_account_id,
                    symbol, side_str, quantity, price, mode, str(guard_exc),
                )
                return

            # Pre-trade live broker margin check.
            # FAIL-SAFETY: if margin verification cannot be completed (broker
            # API failure, timeout, malformed response) the order is REJECTED
            # rather than dispatched blind against live funds.
            try:
                margins = await target_broker.get_margins()
                avail_cash = float(margins.get("available_cash") or 0.0)
                req_cash = price * quantity
                ok, margin_reason = self._risk.check_margin(avail_cash, req_cash)
                if not ok:
                    logger.warning("[LIVE] MARGIN REJECTION: %s %s %d @ %.2f — %s", side_str, symbol, quantity, price, margin_reason)
                    await self._persist_rejected_order(strategy.get("id"), user_id, broker_account_id, symbol, side_str, quantity, price, mode, margin_reason)
                    return
            except Exception as m_exc:
                err_msg = f"Live margin verification failed — order rejected for safety: {m_exc}"
                logger.error("[LIVE] %s", err_msg)
                await self._persist_rejected_order(strategy.get("id"), user_id, broker_account_id, symbol, side_str, quantity, price, mode, err_msg)
                return

        # ── 3. Place order via broker ─────────────────────────────────
        try:
            result = await target_broker.place_order(order_req)
        except Exception as exc:
            logger.error(
                "[%s] Order placement failed: %s %s %d — %s",
                mode,
                side_str,
                symbol,
                quantity,
                exc,
            )
            await self._persist_rejected_order(strategy.get("id"), user_id, broker_account_id, symbol, side_str, quantity, price, mode, str(exc))
            return

        filled_price = result.get("filled_price", price)
        broker_order_id = result.get("broker_order_id", f"ORD_{mode[:3]}_{int(price)}")

        # ── 4. Update risk state ──────────────────────────────────────
        self._risk.record_fill(symbol, side_str, quantity, filled_price)

        # ── 5. Persist to database ────────────────────────────────────
        trade_data = await self._persist_trade(
            strategy_id=strategy.get("id"),
            strategy_name=strategy["name"],
            symbol=symbol,
            side=side_str,
            quantity=quantity,
            price=filled_price,
            broker_order_id=broker_order_id,
            user_id=user_id,
            broker_account_id=broker_account_id,
            mode=mode,
        )

        # ── 6. Broadcast to WebSocket ─────────────────────────────────
        await ws_manager.broadcast("trades", trade_data)

        logger.info(
            "[%s] STRATEGY TRADE EXECUTED: [%s] %s %s %d @ %.2f [broker_id=%s]",
            mode,
            strategy["name"],
            side_str,
            symbol,
            quantity,
            filled_price,
            broker_order_id,
        )

    async def _persist_rejected_order(
        self,
        strategy_id: Optional[str],
        user_id: Optional[str],
        broker_account_id: Optional[str],
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        mode: str,
        reason: str,
    ) -> None:
        """Record rejected order in database for audit and dashboard feedback."""
        async with SessionLocal() as session:
            async with session.begin():
                order = OrderRecord(
                    user_id=user_id,
                    strategy_id=strategy_id,
                    broker_account_id=broker_account_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    order_type="MARKET",
                    status="REJECTED",
                    mode=mode,
                    error_message=reason,
                )
                session.add(order)

    async def _persist_trade(
        self,
        strategy_id: Optional[str],
        strategy_name: str,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        broker_order_id: str,
        pnl: Optional[float] = None,
        user_id: Optional[str] = None,
        broker_account_id: Optional[str] = None,
        mode: str = "PAPER",
    ) -> dict[str, Any]:
        """Write order + trade records to SQLite database."""
        async with SessionLocal() as session:
            async with session.begin():
                order = OrderRecord(
                    user_id=user_id,
                    strategy_id=strategy_id,
                    broker_account_id=broker_account_id,
                    broker_order_id=broker_order_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    filled_price=price,
                    filled_quantity=quantity,
                    order_type="MARKET",
                    status="FILLED",
                    mode=mode,
                )
                session.add(order)
                await session.flush()

                trade = TradeRecord(
                    user_id=user_id,
                    order_id=order.id,
                    strategy_id=strategy_id,
                    strategy_name=strategy_name,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    pnl=pnl,
                    mode=mode,
                )
                session.add(trade)
                event_type = "trade_closed" if pnl is not None else "order_executed"
                from datetime import datetime, timezone
                exec_time = trade.executed_at.isoformat() if trade.executed_at else datetime.now(timezone.utc).isoformat()
                return {
                    "event": event_type,
                    "id": trade.id,
                    "order_id": order.id,
                    "broker_order_id": broker_order_id,
                    "strategy_name": strategy_name,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "price": price,
                    "pnl": pnl,
                    "user_id": user_id,
                    "mode": mode,
                    "executed_at": exec_time,
                }

