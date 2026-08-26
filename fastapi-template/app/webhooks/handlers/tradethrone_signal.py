"""TradeThrone signal handler for processing trading signals from TradeThrone.

Supports full signal payload:
- signal: entry_long, entry_short, exit_long, exit_short, reverse_long, reverse_short, close_all
- symbol: Trading symbol (e.g., NIFTY24AUG25000CE)
- action: BUY, SELL, BUY_TO_OPEN, SELL_TO_CLOSE, etc.
- quantity: Number of lots or contracts
- price: Limit price (optional)
- strategy_name: Name of the strategy generating the signal
- signal_type: Type of signal classification
- order_type: MARKET, LIMIT, SL, SL-M
- product_type: INTRADAY, CARRYFORWARD, CO, OCO, MIS, NRML
- exchange: NSE, BSE, NFO, BFO, MCX, CDS
- trigger_price: Trigger price for SL/SL-M orders
- validity: DAY, IOC, GTD
- tag: Custom tag for tracking
- auth_token: Authentication token
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.webhooks.queue.redis_streams import QueuedWebhook
from app.webhooks.validation.schemas import TradeThronePayload
from app.engine.order_manager import OrderManager
from app.compliance.lot_sizes import (
    validate_quantity,
    get_lot_size,
    TransactionCharges,
    convert_input_to_quantity,
    resolve_symbol,
)
from app.schemas.trading import OrderRequest, Side
from app.db.audit import save_audit_log
from app.core.logging import get_logger
from app.config import settings

logger = get_logger("webhook.handlers.tradethrone")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_side(action: str) -> Side:
    """Convert action string to Side enum."""
    action_upper = action.upper()
    if action_upper in ("BUY", "BUY_TO_OPEN", "BUY_TO_CLOSE"):
        return Side.BUY
    return Side.SELL


def _parse_signal_type(signal: str) -> str:
    """Classify signal for routing."""
    signal_lower = signal.lower()
    if signal_lower in ("entry_long", "entry_short"):
        return "entry"
    elif signal_lower in ("exit_long", "exit_short"):
        return "exit"
    elif signal_lower in ("reverse_long", "reverse_short"):
        return "reverse"
    elif signal_lower == "close_all":
        return "close_all"
    return "unknown"


async def handle_tradethrone_signal(webhook: QueuedWebhook) -> None:
    """Process TradeThrone signal webhook - place order and save audit log."""
    envelope = webhook.envelope
    raw_payload = envelope.payload
    
    # Validate and parse payload
    try:
        payload = TradeThronePayload(**raw_payload)
    except Exception as e:
        logger.error("TradeThrone payload validation failed: %s", e)
        await save_audit_log(
            provider=envelope.provider,
            payload=raw_payload,
            execution={"status": "validation_failed", "error": str(e)},
        )
        return

    logger.info(
        "Processing TradeThrone signal: signal=%s symbol=%s action=%s quantity=%d strategy=%s",
        payload.signal,
        payload.symbol,
        payload.action,
        payload.quantity,
        payload.strategy_name,
    )

    # Resolve symbol and validate lot size compliance
    from app.compliance.lot_sizes import resolve_symbol
    canonical_symbol, exchange = resolve_symbol(payload.symbol)
    
    # Convert lots to quantity if needed (payload.quantity could be lots or qty)
    # For now, assume payload.quantity is lots for index options, qty for stocks
    lot_size = get_lot_size(canonical_symbol)
    is_index = canonical_symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
    
    # Validate quantity against lot size
    validation = validate_quantity(canonical_symbol, payload.quantity, auto_correct=True)
    if not validation.is_valid and validation.warning:
        logger.warning("Quantity auto-corrected: %s", validation.warning)
    
    final_quantity = validation.corrected_quantity
    
    # Calculate estimated charges for risk management
    estimated_price = payload.price or 0  # Will be filled from market data if market order
    is_option = "CE" in canonical_symbol.upper() or "PE" in canonical_symbol.upper()
    is_future = not is_option and canonical_symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
    
    # Get order manager and risk manager from trading engine
    from app.main import get_engine
    engine = get_engine()
    if engine is None:
        logger.error("Trading engine not initialized")
        execution_result = {
            "status": "error",
            "error": "Trading engine not initialized",
            "order_id": None,
            "symbol": canonical_symbol,
        }
        await save_audit_log(
            provider=envelope.provider,
            payload=raw_payload,
            execution=execution_result,
        )
        return
    
    order_manager = engine._order_manager
    risk_manager = order_manager.risk_manager
    
    # Build order request
    side = _parse_side(payload.action)
    
    order_request = OrderRequest(
        symbol=canonical_symbol,
        side=side,
        quantity=final_quantity,
        order_type=payload.order_type,
        price=payload.price,
        trigger_price=payload.trigger_price,
        product_type=payload.product_type,
        validity=payload.validity,
        tag=payload.tag or f"tradethrone:{payload.strategy_name or 'unknown'}",
    )
    
    # Pre-trade risk check
    allowed, reason = risk_manager.check(order_request)
    if not allowed:
        logger.warning("Risk check failed for TradeThrone signal: %s", reason)
        execution_result = {
            "status": "rejected",
            "reason": reason,
            "order_id": None,
            "symbol": canonical_symbol,
        }
        await save_audit_log(
            provider=envelope.provider,
            payload=raw_payload,
            execution=execution_result,
        )
        return
    
    # Estimate charges for logging
    if estimated_price > 0:
        charges = TransactionCharges.calculate_charges(
            symbol=canonical_symbol,
            side=side.value,
            quantity=final_quantity,
            price=estimated_price,
            product_type=payload.product_type,
            is_option=is_option,
            is_future=is_future,
        )
        logger.info(
            "Estimated charges for %s %s x%d @ %.2f: Total ₹%.2f (Brokerage: ₹%.2f, STT: ₹%.2f, GST: ₹%.2f)",
            side.value, canonical_symbol, final_quantity, estimated_price,
            charges["total_charges"], charges["brokerage"], charges["stt"], charges["gst"]
        )
    
    # Place order via order manager (which uses broker)
    try:
        execution_result = await order_manager.place_order(order_request)
        
        # Add strategy metadata to execution result
        if isinstance(execution_result, dict):
            execution_result["strategy_name"] = payload.strategy_name
            execution_result["signal_type"] = _parse_signal_type(payload.signal)
            execution_result["signal"] = payload.signal
            execution_result["tag"] = payload.tag
        
        logger.info(
            "TradeThrone signal processed successfully: order_id=%s status=%s symbol=%s qty=%d",
            execution_result.get("order_id"),
            execution_result.get("status"),
            canonical_symbol,
            final_quantity,
        )
        
    except Exception as e:
        logger.error("Failed to execute TradeThrone signal: %s", e)
        execution_result = {
            "status": "error",
            "error": str(e),
            "order_id": None,
            "symbol": canonical_symbol,
        }
    
    # Save audit log
    await save_audit_log(
        provider=envelope.provider,
        payload=raw_payload,
        execution=execution_result,
    )


# Register handler for tradethrone pools
from app.webhooks.workers.pool import worker_pool, WorkerConfig

# Critical pool for risk alerts
worker_pool.register_pool(WorkerConfig(
    pool_name="tradethrone_critical",
    queue_names=["webhooks:tradethrone:critical"],
    concurrency=10,
    handler=handle_tradethrone_signal,
))

# High priority pool for signals
worker_pool.register_pool(WorkerConfig(
    pool_name="tradethrone_high",
    queue_names=["webhooks:tradethrone:high"],
    concurrency=10,
    handler=handle_tradethrone_signal,
))

# Normal pool for position updates, strategy status
worker_pool.register_pool(WorkerConfig(
    pool_name="tradethrone_normal",
    queue_names=["webhooks:tradethrone:normal"],
    concurrency=5,
    handler=handle_tradethrone_signal,
))


# Register handler for custom_normal pool
from app.webhooks.workers.pool import worker_pool, WorkerConfig

worker_pool.register_pool(WorkerConfig(
    pool_name="custom_normal",
    queue_names=["webhooks:custom:normal"],
    concurrency=5,
    handler=handle_tradethrone_signal,
))