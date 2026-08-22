"""Billing, Subscriptions, Plans, Payments, and Invoices ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class PlanRecord(Base):
    """Database-backed subscription plan tiers (Free, Pro, Elite)."""

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)  # FREE, PRO, ELITE
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    price_monthly: Mapped[float] = mapped_column(Float, default=0.0)
    price_yearly: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    features_json: Mapped[str] = mapped_column(Text, default="{}")  # Stored entitlements
    razorpay_plan_id_monthly: Mapped[str | None] = mapped_column(String(100), nullable=True)
    razorpay_plan_id_yearly: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SubscriptionRecord(Base):
    """User membership subscription plan and status."""

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_name: Mapped[str] = mapped_column(String(50), default="FREE")  # FREE, PRO, ELITE
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE, CANCELLED, EXPIRED
    billing_cycle: Mapped[str] = mapped_column(String(20), default="MONTHLY")  # MONTHLY, YEARLY
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    razorpay_customer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_subscriptions_user_status", "user_id", "status"),
    )


class PaymentRecord(Base):
    """Payment transaction records across gateways (Razorpay, Stripe, UPI, Netbanking)."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subscription_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    gateway: Mapped[str] = mapped_column(String(30), default="RAZORPAY")  # RAZORPAY, STRIPE, UPI
    payment_ref: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)  # razorpay_payment_id
    order_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)  # razorpay_order_id
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING, SUCCESS, FAILED
    method: Mapped[str | None] = mapped_column(String(50), nullable=True)  # upi, card, netbanking
    error_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_payments_user_created", "user_id", "created_at"),
    )


class InvoiceRecord(Base):
    """GST/Tax compliant invoices for billing auditability."""

    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payment_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("payments.id", ondelete="SET NULL"), nullable=True
    )
    invoice_number: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    plan_name: Mapped[str] = mapped_column(String(50), default="PRO")
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    tax_gst: Mapped[float] = mapped_column(Float, default=0.0)  # 18% GST in India
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    pdf_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(50), nullable=True)
    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PAID")  # PAID, VOID
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_invoices_user_issued", "user_id", "issued_at"),
    )
