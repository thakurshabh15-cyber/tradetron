"""Billing, Razorpay Checkout, Webhook Processing, GST Tax Invoices, and Subscription Lifecycle."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.config import settings
from app.core.audit import log_audit_event
from app.core.logging import get_logger
from app.core.payment_gateway import razorpay_gateway
from app.db.session import get_db
from app.models.billing import InvoiceRecord, PaymentRecord, PlanRecord, SubscriptionRecord
from app.models.user import UserRecord

logger = get_logger("api.billing")
router = APIRouter(prefix="/api/billing", tags=["billing"])


# ── Schemas ──────────────────────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    plan_name: str  # PRO, ELITE
    billing_cycle: str = "MONTHLY"  # MONTHLY, YEARLY


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    plan_name: str
    billing_cycle: str = "MONTHLY"


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("/plans")
async def list_plans(db: AsyncSession = Depends(get_db)):
    """Retrieve all active membership plan tiers from database."""
    stmt = select(PlanRecord).where(PlanRecord.is_active == True).order_by(PlanRecord.price_monthly)  # noqa: E712
    res = await db.execute(stmt)
    plans = res.scalars().all()

    result = []
    for p in plans:
        features = {}
        try:
            features = json.loads(p.features_json) if p.features_json else {}
        except Exception:
            pass

        result.append({
            "id": p.id,
            "name": p.name,
            "display_name": p.display_name,
            "description": p.description,
            "price_monthly": p.price_monthly,
            "price_yearly": p.price_yearly,
            "currency": p.currency,
            "features": features,
        })

    return result


@router.get("/subscription")
async def get_user_subscription(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve authenticated user's current subscription, renewal date, and entitlements."""
    stmt = (
        select(SubscriptionRecord)
        .where(SubscriptionRecord.user_id == user.id)
        .order_by(desc(SubscriptionRecord.created_at))
    )
    res = await db.execute(stmt)
    sub = res.scalars().first()

    if not sub:
        # Default starter free tier
        return {
            "plan_name": "FREE",
            "status": "ACTIVE",
            "billing_cycle": "MONTHLY",
            "amount": 0.0,
            "currency": "INR",
            "start_date": datetime.now(timezone.utc).isoformat(),
            "end_date": None,
            "features": {
                "max_live_strategies": 1,
                "max_brokers": 1,
                "tick_speed": "1s",
                "historical_candles": "15m",
                "priority_support": False,
                "vip_vps": False,
            },
        }

    # Fetch plan entitlements
    plan_stmt = select(PlanRecord).where(PlanRecord.name == sub.plan_name.upper())
    plan_res = await db.execute(plan_stmt)
    plan_record = plan_res.scalar_one_or_none()

    features = {}
    if plan_record and plan_record.features_json:
        try:
            features = json.loads(plan_record.features_json)
        except Exception:
            pass

    return {
        "id": sub.id,
        "plan_name": sub.plan_name,
        "status": sub.status,
        "billing_cycle": sub.billing_cycle,
        "amount": sub.amount,
        "currency": sub.currency,
        "start_date": sub.start_date.isoformat() if sub.start_date else None,
        "end_date": sub.end_date.isoformat() if sub.end_date else None,
        "features": features,
    }


@router.post("/create-order")
async def create_checkout_order(
    req: CreateOrderRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a Razorpay Order ID for frontend checkout."""
    plan_name_norm = req.plan_name.upper().strip()
    stmt = select(PlanRecord).where(PlanRecord.name == plan_name_norm, PlanRecord.is_active == True)  # noqa: E712
    res = await db.execute(stmt)
    plan = res.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan {plan_name_norm} not found or inactive")

    is_yearly = req.billing_cycle.upper().strip() == "YEARLY"
    amount = plan.price_yearly if is_yearly else plan.price_monthly

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Free tier does not require checkout order")

    # Call Razorpay Gateway to create real order
    notes = {
        "user_id": user.id,
        "user_email": user.email,
        "plan_name": plan_name_norm,
        "billing_cycle": req.billing_cycle.upper(),
    }
    order_data = await razorpay_gateway.create_order(
        amount=amount,
        currency=plan.currency,
        receipt=f"rcpt_{user.id[:8]}_{int(datetime.now().timestamp())}",
        notes=notes,
    )

    # Store pending payment record
    pay_record = PaymentRecord(
        user_id=user.id,
        gateway="RAZORPAY",
        payment_ref=f"pending_{order_data['id']}",
        order_id=order_data["id"],
        amount=amount,
        currency=plan.currency,
        status="PENDING",
    )
    db.add(pay_record)
    await db.commit()

    return {
        "key_id": razorpay_gateway.key_id or "rzp_test_tradetron_mock_key",
        "order_id": order_data["id"],
        "amount": order_data["amount"],  # in paise
        "amount_rupees": amount,
        "currency": plan.currency,
        "plan_name": plan_name_norm,
        "billing_cycle": req.billing_cycle.upper(),
        "user_details": {
            "name": user.full_name,
            "email": user.email,
        },
    }


@router.post("/verify-payment")
async def verify_payment(
    req: VerifyPaymentRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify Razorpay HMAC-SHA256 signature, upgrade user plan, and generate GST invoice."""
    is_valid = razorpay_gateway.verify_payment_signature(
        razorpay_order_id=req.razorpay_order_id,
        razorpay_payment_id=req.razorpay_payment_id,
        razorpay_signature=req.razorpay_signature,
    )

    if not is_valid:
        logger.warning(
            "Razorpay signature mismatch for user %s [order=%s, payment=%s]",
            user.email,
            req.razorpay_order_id,
            req.razorpay_payment_id,
        )
        raise HTTPException(
            status_code=400,
            detail="Payment verification failed: Invalid transaction signature",
        )

    plan_name_norm = req.plan_name.upper().strip()
    stmt = select(PlanRecord).where(PlanRecord.name == plan_name_norm)
    res = await db.execute(stmt)
    plan = res.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="Selected plan not found")

    is_yearly = req.billing_cycle.upper() == "YEARLY"
    amount = plan.price_yearly if is_yearly else plan.price_monthly
    duration_days = 365 if is_yearly else 30

    # ── Payment/plan integrity gate ─────────────────────────────────────
    # The client supplies plan_name/billing_cycle in the verify request, so
    # cross-check them against the pending PaymentRecord created at
    # create-order time.  Without this gate a user could pay for a cheap plan
    # and call verify-payment claiming an expensive plan.
    order_stmt = select(PaymentRecord).where(
        PaymentRecord.order_id == req.razorpay_order_id,
        PaymentRecord.user_id == user.id,
    )
    order_rec = (await db.execute(order_stmt)).scalar_one_or_none()
    if not order_rec:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unknown Razorpay order — call /api/billing/create-order first "
                "and verify the same order you paid for"
            ),
        )
    if order_rec.user_id != user.id:
        raise HTTPException(status_code=403, detail="Payment order does not belong to this account")
    expected_amount = order_rec.amount or 0.0
    if expected_amount > 0 and abs(expected_amount - amount) > 0.01:
        logger.warning(
            "Payment plan/amount mismatch for user %s: order %s was created for %.2f but verify claims %s (%.2f)",
            user.email, req.razorpay_order_id, expected_amount, plan_name_norm, amount,
        )
        raise HTTPException(
            status_code=400,
            detail="Plan/billing-cycle does not match the order that was created and paid for",
        )

    now = datetime.now(timezone.utc)
    end_date = now + timedelta(days=duration_days)

    # 1. Update / Create Subscription
    sub_stmt = select(SubscriptionRecord).where(
        SubscriptionRecord.user_id == user.id,
        SubscriptionRecord.status == "ACTIVE",
    )
    sub_res = await db.execute(sub_stmt)
    sub = sub_res.scalar_one_or_none()

    if not sub:
        sub = SubscriptionRecord(
            user_id=user.id,
            plan_name=plan_name_norm,
            status="ACTIVE",
            billing_cycle=req.billing_cycle.upper(),
            amount=amount,
            currency=plan.currency,
            start_date=now,
            end_date=end_date,
        )
        db.add(sub)
    else:
        sub.plan_name = plan_name_norm
        sub.status = "ACTIVE"
        sub.billing_cycle = req.billing_cycle.upper()
        sub.amount = amount
        sub.start_date = now
        sub.end_date = end_date

    await db.flush()

    # 2. Record Payment
    pay_stmt = select(PaymentRecord).where(PaymentRecord.order_id == req.razorpay_order_id)
    pay_res = await db.execute(pay_stmt)
    pay_rec = pay_res.scalar_one_or_none()

    if not pay_rec:
        pay_rec = PaymentRecord(
            user_id=user.id,
            subscription_id=sub.id,
            gateway="RAZORPAY",
            payment_ref=req.razorpay_payment_id,
            order_id=req.razorpay_order_id,
            amount=amount,
            currency=plan.currency,
            status="SUCCESS",
            method="RAZORPAY_CHECKOUT",
        )
        db.add(pay_rec)
    else:
        pay_rec.payment_ref = req.razorpay_payment_id
        pay_rec.subscription_id = sub.id
        pay_rec.status = "SUCCESS"
        pay_rec.method = "RAZORPAY_CHECKOUT"

    await db.flush()

    # 3. Generate GST Tax Invoice
    inv_num = f"INV-{now.year}-{uuid.uuid4().hex[:8].upper()}"
    tax_gst = round(amount * 0.18 / 1.18, 2)  # Inclusive 18% GST calculation
    base_price = round(amount - tax_gst, 2)

    invoice = InvoiceRecord(
        user_id=user.id,
        payment_id=pay_rec.id,
        invoice_number=inv_num,
        plan_name=plan_name_norm,
        amount=base_price,
        tax_gst=tax_gst,
        total_amount=amount,
        currency=plan.currency,
        status="PAID",
        issued_at=now,
    )
    db.add(invoice)

    await db.commit()
    await db.refresh(sub)
    await db.refresh(invoice)

    # Log audit event
    await log_audit_event(
        db=db,
        action="SUBSCRIPTION_UPGRADED",
        resource_type="SUBSCRIPTION",
        user_id=user.id,
        resource_id=sub.id,
        status="SUCCESS",
        details={
            "plan": plan_name_norm,
            "cycle": req.billing_cycle.upper(),
            "amount": amount,
            "payment_id": req.razorpay_payment_id,
            "invoice_number": inv_num,
        },
    )

    logger.info("User %s successfully upgraded to %s (Invoice: %s)", user.email, plan_name_norm, inv_num)

    return {
        "success": True,
        "message": f"Successfully subscribed to {plan.display_name}",
        "subscription": {
            "id": sub.id,
            "plan_name": sub.plan_name,
            "status": sub.status,
            "end_date": sub.end_date.isoformat(),
        },
        "invoice_number": inv_num,
    }


@router.post("/cancel-subscription")
async def cancel_subscription(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel active recurring membership and revert to Free tier at end of period."""
    stmt = (
        select(SubscriptionRecord)
        .where(
            SubscriptionRecord.user_id == user.id,
            SubscriptionRecord.status == "ACTIVE",
        )
    )
    res = await db.execute(stmt)
    sub = res.scalar_one_or_none()

    if not sub:
        raise HTTPException(status_code=404, detail="No active subscription found to cancel")

    sub.status = "CANCELLED"
    await db.commit()

    await log_audit_event(
        db=db,
        action="SUBSCRIPTION_CANCELLED",
        resource_type="SUBSCRIPTION",
        user_id=user.id,
        resource_id=sub.id,
        status="SUCCESS",
        details={"previous_plan": sub.plan_name},
    )

    logger.info("Subscription cancelled for user %s (%s)", user.email, sub.plan_name)
    return {
        "success": True,
        "message": "Subscription cancelled successfully. You will retain access until the end of your billing cycle.",
        "status": "CANCELLED",
    }


@router.post("/webhook/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Handle server-side Razorpay webhook events with HMAC signature verification."""
    body_bytes = await request.body()

    if not x_razorpay_signature:
        raise HTTPException(status_code=400, detail="Missing X-Razorpay-Signature header")

    is_valid = razorpay_gateway.verify_webhook_signature(body_bytes, x_razorpay_signature)
    # Fail-closed HMAC enforcement.  The historical "mock_webhook_sig*" prefix
    # bypass allowed ANY caller who knew the public repo string to forge
    # payment.captured events and upgrade themselves for free.  The bypass is
    # now only honoured outside production (local dev / test suites); in
    # production every webhook MUST carry a valid HMAC signature.
    mock_bypass_allowed = settings.environment != "production"
    if not is_valid and not (mock_bypass_allowed and x_razorpay_signature.startswith("mock_webhook_sig")):
        logger.error("Razorpay webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        event = json.loads(body_bytes.decode())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}")

    event_type = event.get("event")
    payload = event.get("payload", {})
    logger.info("Processing Razorpay webhook event: %s", event_type)

    if event_type == "payment.captured":
        payment_entity = payload.get("payment", {}).get("entity", {})
        pay_id = payment_entity.get("id")
        order_id = payment_entity.get("order_id")
        notes = payment_entity.get("notes", {})
        user_id = notes.get("user_id")
        plan_name = (notes.get("plan_name") or "PRO").upper().strip()
        billing_cycle = (notes.get("billing_cycle") or "MONTHLY").upper().strip()
        amount_paise = payment_entity.get("amount", 0)
        amount = amount_paise / 100.0 if amount_paise else 1999.0

        if user_id:
            now = datetime.now(timezone.utc)
            duration_days = 365 if billing_cycle == "YEARLY" else 30
            end_date = now + timedelta(days=duration_days)

            # 1. Update / Create Subscription
            sub_stmt = select(SubscriptionRecord).where(
                SubscriptionRecord.user_id == user_id,
                SubscriptionRecord.status == "ACTIVE",
            )
            sub_res = await db.execute(sub_stmt)
            sub = sub_res.scalar_one_or_none()

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

            # 2. Reconcile or create PaymentRecord safely
            p_rec = None
            if order_id:
                p_stmt = select(PaymentRecord).where(PaymentRecord.order_id == order_id)
                p_res = await db.execute(p_stmt)
                p_rec = p_res.scalar_one_or_none()
            if not p_rec and pay_id:
                p_stmt = select(PaymentRecord).where(PaymentRecord.payment_ref == pay_id)
                p_res = await db.execute(p_stmt)
                p_rec = p_res.scalar_one_or_none()

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
                if pay_id and p_rec.payment_ref != pay_id:
                    existing_ref = (await db.execute(select(PaymentRecord).where(PaymentRecord.payment_ref == pay_id, PaymentRecord.id != p_rec.id))).scalar_one_or_none()
                    if not existing_ref:
                        p_rec.payment_ref = pay_id
                p_rec.subscription_id = sub.id
                p_rec.method = "RAZORPAY_WEBHOOK"

            await db.flush()

            # 3. Create GST Tax Invoice Record
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

            logger.info("Webhook reconciled: User %s upgraded to %s (Invoice: %s, PayID: %s)", user_id, plan_name, inv_num, pay_id)

    elif event_type == "payment.failed":
        payment_entity = payload.get("payment", {}).get("entity", {})
        order_id = payment_entity.get("order_id")
        error_desc = payment_entity.get("error_description", "Payment failed")
        if order_id:
            stmt = select(PaymentRecord).where(PaymentRecord.order_id == order_id)
            res = await db.execute(stmt)
            p_rec = res.scalar_one_or_none()
            if p_rec:
                p_rec.status = "FAILED"
                p_rec.error_reason = error_desc
                await db.commit()

    elif event_type == "subscription.charged":
        # Auto-renew: extend the subscription billing period by 30/365 days
        sub_entity = payload.get("subscription", {}).get("entity", {})
        rzp_sub_id = sub_entity.get("id")
        notes = sub_entity.get("notes") or {}
        billing_cycle = (notes.get("billing_cycle") or "MONTHLY").upper()
        if rzp_sub_id:
            stmt = select(SubscriptionRecord).where(
                SubscriptionRecord.razorpay_subscription_id == rzp_sub_id
            )
            res = await db.execute(stmt)
            s_rec = res.scalar_one_or_none()
            if s_rec and s_rec.status == "ACTIVE":
                duration_days = 365 if billing_cycle == "YEARLY" else 30
                now = datetime.now(timezone.utc)
                # Extend from existing end_date (or now) to avoid losing remaining days
                base = s_rec.end_date if (s_rec.end_date and s_rec.end_date > now) else now
                s_rec.end_date = base + timedelta(days=duration_days)
                await db.commit()
                logger.info(
                    "[Webhook] subscription.charged: extended sub %s until %s",
                    rzp_sub_id, s_rec.end_date.isoformat(),
                )

    elif event_type in ("subscription.halted", "subscription.cancelled", "payment.refunded"):
        # Downgrade user to FREE plan on cancellation or payment halt
        sub_entity = payload.get("subscription", {}).get("entity", {})
        rzp_sub_id = sub_entity.get("id")
        if rzp_sub_id:
            stmt = select(SubscriptionRecord).where(
                SubscriptionRecord.razorpay_subscription_id == rzp_sub_id
            )
            res = await db.execute(stmt)
            s_rec = res.scalar_one_or_none()
            if s_rec:
                s_rec.status = "CANCELLED"
                s_rec.plan_name = "FREE"
                await db.commit()
                logger.info(
                    "[Webhook] %s: user sub %s downgraded to FREE plan",
                    event_type, rzp_sub_id,
                )

    return {"status": "ok", "event": event_type}



@router.get("/invoices")
async def list_invoices(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve user's GST tax invoice history."""
    stmt = (
        select(InvoiceRecord)
        .where(InvoiceRecord.user_id == user.id)
        .order_by(desc(InvoiceRecord.issued_at))
    )
    res = await db.execute(stmt)
    invoices = res.scalars().all()

    return [
        {
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "plan_name": inv.plan_name,
            "amount": inv.amount,
            "tax_gst": inv.tax_gst,
            "total_amount": inv.total_amount,
            "currency": inv.currency,
            "status": inv.status,
            "issued_at": inv.issued_at.isoformat(),
            "download_url": f"/api/billing/invoices/{inv.id}/download",
        }
        for inv in invoices
    ]


@router.get("/invoices/{invoice_id}/download")
async def download_invoice(
    invoice_id: str,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate and return authentic, printable GST Tax Invoice HTML."""
    stmt = select(InvoiceRecord).where(
        InvoiceRecord.id == invoice_id,
        InvoiceRecord.user_id == user.id,
    )
    res = await db.execute(stmt)
    inv = res.scalar_one_or_none()

    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Fetch linked payment
    pay_id = "PAY_DIRECT"
    if inv.payment_id:
        p_stmt = select(PaymentRecord).where(PaymentRecord.id == inv.payment_id)
        p_res = await db.execute(p_stmt)
        p_rec = p_res.scalar_one_or_none()
        if p_rec:
            pay_id = p_rec.payment_ref

    html_content = razorpay_gateway.generate_tax_invoice_html(
        invoice_number=inv.invoice_number,
        user_name=user.full_name,
        user_email=user.email,
        plan_name=inv.plan_name,
        amount=inv.total_amount,
        tax_gst=inv.tax_gst,
        total_amount=inv.total_amount,
        payment_id=pay_id,
        issued_at=inv.issued_at,
    )

    return Response(
        content=html_content,
        media_type="text/html",
        headers={
            "Content-Disposition": f"inline; filename=Tradetron_Invoice_{inv.invoice_number}.html"
        },
    )
