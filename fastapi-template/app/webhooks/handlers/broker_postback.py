"""Broker postback handler for order updates from Zerodha, Upstox, Angel One, Binance."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.webhooks.queue.redis_streams import QueuedWebhook
from app.db.session import get_db
from app.models.trading import OrderRecord, TradeRecord
from app.market_data.manager import ws_manager
from app.core.logging import get_logger
from app.core.monitoring import monitoring_sentinel

logger = get_logger("webhook.handlers.broker")


async def handle_broker_postback(webhook: QueuedWebhook) -> None:
    """Process broker order postback (Zerodha, Upstox, Angel One, Binance)"""
    envelope = webhook.envelope
    payload = envelope.payload
    
    broker_order_id = payload.get("broker_order_id") or payload.get("order_id")
    status = payload.get("status", "").upper()
    symbol = payload.get("symbol") or payload.get("tradingsymbol", "")
    filled_qty = int(payload.get("filled_quantity", 0))
    avg_price = float(payload.get("average_price", 0.0))
    
    if not broker_order_id:
        raise ValueError("Missing broker_order_id in payload")
    
    # Get DB session
    async for db in get_db():
        # Find order by broker_order_id
        stmt = select(OrderRecord).where(OrderRecord.broker_order_id == broker_order_id)
        result = await db.execute(stmt)
        order = result.scalar_one_or_none()
        
        if not order:
            logger.warning("Order not found for broker_order_id: %s", broker_order_id)
            # Not an error - could be race condition or test order
            return
        
        # Update order status
        order.status = status
        if filled_qty:
            order.filled_quantity = filled_qty
        if avg_price:
            order.filled_price = avg_price
        
        # Create trade record on fill
        if status == "FILLED":
            trade = TradeRecord(
                strategy_id=order.strategy_id,
                broker_order_id=broker_order_id,
                user_id=order.user_id,
                symbol=order.symbol,
                side=order.side,
                quantity=filled_qty or order.quantity,
                entry_price=avg_price or order.price or 0.0,
                status="CLOSED",
                exit_reason="BROKER_POSTBACK_FILL",
            )
            db.add(trade)
        
        await db.commit()
        
        # Broadcast to WebSocket clients
        await ws_manager.broadcast(
            f"order_update:{order.strategy_id}",
            {
                "event": "ORDER_STATUS_CHANGED",
                "order_id": order.id,
                "broker_order_id": broker_order_id,
                "status": status,
                "symbol": order.symbol,
                "filled_quantity": filled_qty,
                "average_price": avg_price,
            },
        )
        
        logger.info("Processed broker postback: order=%s status=%s symbol=%s", 
                    broker_order_id, status, symbol)


# Register handler
from app.webhooks.workers.pool import worker_pool, WorkerConfig

worker_pool.register_pool(WorkerConfig(
    pool_name="broker_critical",
    queue_names=["webhooks:broker:critical"],
    concurrency=10,  # High concurrency for critical path
    handler=handle_broker_postback,
))