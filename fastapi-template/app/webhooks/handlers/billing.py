"""Razorpay billing webhook handler."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.webhooks.queue.redis_streams import QueuedWebhook
from app.db.session import get_db
from app.models.billing import SubscriptionRecord, PaymentRecord, InvoiceRecord, PlanRecord
from app.core.logging import get_logger
from app.core.audit import log_audit_event

logger = get_logger("webhook.handlers.billing")


async def handle_razorpay_webhook(webhook: QueuedWebhook) -> None:
    """Process Razorpay billing webhooks"""
    envelope = webhook.envelope
    event_type = envelope.event_type
    payload = envelope.payload
    
    async for db in get_db():
        if event_type == "payment.captured":
            await _handle_payment_captured(db, payload)
        elif event_type == "payment.failed":
            await _handle_payment_failed(db, payload)
        elif event_type == "subscription.charged":
            await _handle_subscription_charged(db, payload)
        elif event_type in ("subscription.halted", "subscription.cancelled", "payment.refunded"):
            await _handle_subscription_cancelled(db, payload, event_type)
        else:
            logger.info("Unhandled Razorpay event type: %s", event_type)


async def _handle_payment_captured(db: AsyncSession, payload: dict) -> None:
    payment_entity = payload.get("payment", {}).get("entity", {})
    pay_id = payment_entity.get("id")
    order_id = payment_entity.get("order_id")
    notes = payment_entity.get("notes", {})
    user_id = notes.get("user_id")
    plan_name = (notes.get("plan_name") or "PRO").upper()
    billing_cycle = (notes.get("billing_cycle") or "MONTHLY").upper()
    amount_paise = payment_entity.get("amount", 0)
    amount = amount_paise / 100.0 if amount_paise else 1999.0
    
    if not user_id:
        raise ValueError("Missing user_id in payment notes")
    
    now = datetime.now(timezone.utc)
    duration_days = 365 if billing_cycle == "YEARLY" else 30
    end_date = now + timedelta(days=duration_days)
    
    # Upsert subscription
    stmt = select(SubscriptionRecord).where(
        SubscriptionRecord.user_id == user_id,
        SubscriptionRecord.status == "ACTIVE",
    )
    result = await db.execute(stmt)
    sub = result.scalar_one_or_none()
    
    if not sub:
        sub = SubscriptionRecord(
            user_id=user_id,
            plan_name=plan_name,
            status="ACTIVE",
            billing_cycle=billing_cycle,
            amount=amount,
            currency="INR",
            start_date=now,
            end_date=end_date,
        )
        db.add(sub)
    else:
        sub.plan_name = plan_name
        sub.status = "ACTIVE"
        sub.billing_cycle = billing_cycle
        sub.amount = amount
        sub.start_date = now
        sub.end_date = end_date
    
    await db.flush()
    
    # Record payment
    p_rec = None
    if order_id:
        p_stmt = select(PaymentRecord).where(PaymentRecord.order_id == order_id)
        p_result = await db.execute(p_stmt)
        p_rec = p_result.scalar_one_or_none()
    if not p_rec and pay_id:
        p_stmt = select(PaymentRecord).where(PaymentRecord.payment_ref == pay_id)
        p_result = await db.execute(p_stmt)
        p_rec = p_result.scalar_one_or_none()
    
    if not p_rec:
        p_rec = PaymentRecord(
            user_id=user_id,
            subscription_id=sub.id,
            gateway="RAZORPAY",
            payment_ref=pay_id or f"pay_{uuid.uuid4().hex[:14]}",
            order_id=order_id or f"ord_{uuid.uuid4().hex[:14]}",
            amount=amount,
            currency="INR",
            status="SUCCESS",
            method="RAZORPAY_WEBHOOK",
        )
        db.add(p_rec)
    else:
        p_rec.status = "SUCCESS"
        p_rec.subscription_id = sub.id
        p_rec.method = "RAZORPAY_WEBHOOK"
    
    await db.flush()
    
    # Create GST invoice
    inv_num = f"INV-{now.year}-{uuid.uuid4().hex[:8].upper()}"
    tax_gst = round(amount * 0.18 / 1.18, 2)
    base_price = round(amount - tax_gst, 2)
    
    invoice = InvoiceRecord(
        user_id=user_id,
        payment_id=p_rec.id,
        invoice_number=inv_num,
        plan_name=plan_name,
        amount=base_price,
        tax_gst=tax_gst,
        total_amount=amount,
        currency="INR",
        status="PAID",
        issued_at=now,
    )
    db.add(invoice)
    
    await db.commit()
    
    await log_audit_event(
        db=db,
        action="SUBSCRIPTION_UPGRADED_VIA_WEBHOOK",
        resource_type="SUBSCRIPTION",
        user_id=user_id,
        resource_id=sub.id,
        status="SUCCESS",
        details={"plan": plan_name, "cycle": billing_cycle, "amount": amount, "invoice": inv_num},
    )
    
    logger.info("Webhook reconciled: User %s upgraded to %s (Invoice: %s)", user_id, plan_name, inv_num)


async def _handle_payment_failed(db: AsyncSession, payload: dict) -> None:
    payment_entity = payload.get("payment", {}).get("entity", {})
    pay_id = payment_entity.get("id")
    order_id = payment_entity.get("order_id")
    notes = payment_entity.get("notes", {})
    user_id = notes.get("user_id")
    error_code = payment_entity.get("error_code")
    error_description = payment_entity.get("error_description")
    
    if not user_id:
        logger.warning("Payment failed but no user_id in notes: %s", pay_id)
        return
    
    # Record failed payment
    p_rec = PaymentRecord(
        user_id=user_id,
        gateway="RAZORPAY",
        payment_ref=pay_id or f"pay_{uuid.uuid4().hex[:14]}",
        order_id=order_id or f"ord_{uuid.uuid4().hex[:14]}",
        amount=payment_entity.get("amount", 0) / 100.0,
        currency="INR",
        status="FAILED",
        method="RAZORPAY_WEBHOOK",
        error_code=error_code,
        error_description=error_description,
    )
    db.add(p_rec)
    await db.commit()
    
    await log_audit_event(
        db=db,
        action="PAYMENT_FAILED_VIA_WEBHOOK",
        resource_type="PAYMENT",
        user_id=user_id,
        resource_id=p_rec.id,
        status="FAILED",
        details={"error_code": error_code, "error_description": error_description},
    )
    
    logger.warning("Payment failed for user %s: %s - %s", user_id, error_code, error_description)


async def _handle_subscription_charged(db: AsyncSession, payload: dict) -> None:
    """Handle recurring subscription charge"""
    subscription_entity = payload.get("subscription", {}).get("entity", {})
    sub_id = subscription_entity.get("id")
    pay_id = subscription_entity.get("current_payment_id")
    notes = subscription_entity.get("notes", {})
    user_id = notes.get("user_id")
    plan_name = (notes.get("plan_name") or "PRO").upper()
    billing_cycle = (notes.get("billing_cycle") or "MONTHLY").upper()
    amount_paise = subscription_entity.get("amount", 0)
    amount = amount_paise / 100.0 if amount_paise else 1999.0
    
    if not user_id:
        logger.warning("Subscription charged but no user_id: %s", sub_id)
        return
    
    now = datetime.now(timezone.utc)
    duration_days = 365 if billing_cycle == "YEARLY" else 30
    end_date = now + timedelta(days=duration_days)
    
    # Update subscription
    stmt = select(SubscriptionRecord).where(
        SubscriptionRecord.user_id == user_id,
        SubscriptionRecord.status == "ACTIVE",
    )
    result = await db.execute(stmt)
    sub = result.scalar_one_or_none()
    
    if sub:
        sub.end_date = end_date
        sub.amount = amount
        await db.flush()
    
    # Record payment
    p_rec = PaymentRecord(
        user_id=user_id,
        subscription_id=sub.id if sub else None,
        gateway="RAZORPAY",
        payment_ref=pay_id or f"pay_{uuid.uuid4().hex[:14]}",
        order_id=subscription_entity.get("order_id") or f"ord_{uuid.uuid4().hex[:14]}",
        amount=amount,
        currency="INR",
        status="SUCCESS",
        method="RAZORPAY_SUBSCRIPTION",
    )
    db.add(p_rec)
    await db.flush()
    
    # Create GST invoice
    inv_num = f"INV-{now.year}-{uuid.uuid4().hex[:8].upper()}"
    tax_gst = round(amount * 0.18 / 1.18, 2)
    base_price = round(amount - tax_gst, 2)
    
    invoice = InvoiceRecord(
        user_id=user_id,
        payment_id=p_rec.id,
        invoice_number=inv_num,
        plan_name=plan_name,
        amount=base_price,
        tax_gst=tax_gst,
        total_amount=amount,
        currency="INR",
        status="PAID",
        issued_at=now,
    )
    db.add(invoice)
    
    await db.commit()
    
    logger.info("Subscription renewed: User %s plan %s (Invoice: %s)", user_id, plan_name, inv_num)


async def _handle_subscription_cancelled(db: AsyncSession, payload: dict, event_type: str) -> None:
    subscription_entity = payload.get("subscription", {}).get("entity", {})
    sub_id = subscription_entity.get("id")
    notes = subscription_entity.get("notes", {})
    user_id = notes.get("user_id")
    
    if not user_id:
        logger.warning("Subscription cancelled but no user_id: %s", sub_id)
        return
    
    # Update subscription status
    stmt = select(SubscriptionRecord).where(
        SubscriptionRecord.user_id == user_id,
        SubscriptionRecord.status == "ACTIVE",
    )
    result = await db.execute(stmt)
    sub = result.scalar_one_or_none()
    
    if sub:
        sub.status = "CANCELLED"
        sub.cancelled_at = datetime.now(timezone.utc)
        sub.cancellation_reason = event_type
        await db.commit()
    
    await log_audit_event(
        db=db,
        action="SUBSCRIPTION_CANCELLED_VIA_WEBHOOK",
        resource_type="SUBSCRIPTION",
        user_id=user_id,
        resource_id=sub.id if sub else None,
        status="SUCCESS",
        details={"reason": event_type, "razorpay_subscription_id": sub_id},
    )
    
    logger.info("Subscription cancelled for user %s: %s", user_id, event_type)


# Register handler
from app.webhooks.workers.pool import worker_pool, WorkerConfig

worker_pool.register_pool(WorkerConfig(
    pool_name="billing_high",
    queue_names=["webhooks:billing:high"],
    concurrency=5,
    handler=handle_razorpay_webhook,
))