"""Unit tests for Production Relational Database Schema, Encryption at Rest, and Audit Trail."""

import asyncio
import time
import uuid
from sqlalchemy import select
from app.db.session import init_db, SessionLocal
from app.models.user import UserRecord
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import StrategyRecord, OrderRecord, PositionRecord, TradeRecord
from app.models.billing import SubscriptionRecord, PaymentRecord, InvoiceRecord
from app.models.audit import AuditLogRecord
from app.core.crypto import encrypt_secret, decrypt_secret, mask_secret
from app.core.audit import log_audit_event


async def test_production_schema_suite():
    await init_db()

    async with SessionLocal() as db:
        uid = int(time.time() * 1000) % 1000000
        user_id = str(uuid.uuid4())
        user_email = f"schema_user_{uid}@tradetron.io"

        # 1. Test User with KYC Status & Role
        user = UserRecord(
            id=user_id,
            email=user_email,
            phone=f"+919999{uid:06d}",
            full_name="Institutional Trader",
            role="creator",
            kyc_status="VERIFIED",
        )
        db.add(user)
        await db.commit()

        # 2. Test Broker Account with Encryption at Rest
        raw_api_key = "angel_prod_api_key_998877665544"
        raw_api_secret = "super_secret_broker_token_xyz"
        broker_acc = BrokerAccountRecord(
            user_id=user.id,
            broker_name="ANGEL_ONE",
            account_name="Angel One Live F&O",
            client_id="ANGEL12345",
            api_key_encrypted="",
        )
        broker_acc.set_api_key(raw_api_key)
        broker_acc.set_api_secret(raw_api_secret)
        db.add(broker_acc)
        await db.commit()
        await db.refresh(broker_acc)

        # Verify encryption: raw text is NOT stored directly
        assert broker_acc.api_key_encrypted != raw_api_key
        assert broker_acc.get_api_key() == raw_api_key
        assert broker_acc.get_api_secret() == raw_api_secret
        assert broker_acc.api_key_masked == "ang****5544"

        # 3. Test Strategy & Positions
        strategy = StrategyRecord(
            user_id=user.id,
            name="BankNifty Gamma Scalper",
            symbols_json='["BANKNIFTY"]',
            conditions_json='[{"indicator":"RSI","period":14,"operator":">","value":60}]',
            action_json='{"action":"BUY","quantity":25}',
            enabled=True,
        )
        db.add(strategy)
        await db.commit()
        await db.refresh(strategy)

        position = PositionRecord(
            user_id=user.id,
            strategy_id=strategy.id,
            broker_account_id=broker_acc.id,
            symbol="BANKNIFTY",
            side="BUY",
            quantity=25,
            entry_price=48500.0,
            current_price=48950.0,
            stop_loss_price=48200.0,
            take_profit_price=49200.0,
            unrealized_pnl=11250.0,
            status="OPEN",
        )
        db.add(position)
        await db.commit()

        # 4. Test Orders and Trades
        order = OrderRecord(
            user_id=user.id,
            strategy_id=strategy.id,
            broker_account_id=broker_acc.id,
            symbol="BANKNIFTY",
            side="BUY",
            quantity=25,
            price=48500.0,
            status="FILLED",
            broker_order_id="BRK-ORD-991200",
        )
        db.add(order)
        await db.commit()
        await db.refresh(order)

        trade = TradeRecord(
            user_id=user.id,
            order_id=order.id,
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            symbol="BANKNIFTY",
            side="BUY",
            quantity=25,
            price=48500.0,
            entry_price=48500.0,
            exit_price=48950.0,
            pnl=11250.0,
            pnl_pct=0.92,
            exit_reason="TAKE_PROFIT",
        )
        db.add(trade)
        await db.commit()

        # 5. Test Subscriptions, Payments & Invoices
        sub = SubscriptionRecord(
            user_id=user.id,
            plan_name="PRO",
            status="ACTIVE",
            billing_cycle="YEARLY",
            amount=19999.0,
            currency="INR",
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

        payment = PaymentRecord(
            user_id=user.id,
            subscription_id=sub.id,
            gateway="RAZORPAY",
            payment_ref=f"pay_rzp_{uid}",
            amount=19999.0,
            currency="INR",
            status="SUCCESS",
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)

        invoice = InvoiceRecord(
            user_id=user.id,
            payment_id=payment.id,
            invoice_number=f"INV-2026-{uid}",
            amount=16948.30,
            tax_gst=3050.70,
            total_amount=19999.0,
            currency="INR",
            status="PAID",
        )
        db.add(invoice)
        await db.commit()

        # 6. Test Audit Log
        audit_entry = await log_audit_event(
            db=db,
            action="ORDER_PLACED",
            resource_type="ORDER",
            user_id=user.id,
            resource_id=order.id,
            ip_address="192.168.1.100",
            status="SUCCESS",
            details={"symbol": "BANKNIFTY", "quantity": 25, "broker": "ANGEL_ONE"},
        )
        assert audit_entry.id is not None
        assert audit_entry.action == "ORDER_PLACED"

        # Verify Querying via Indexed columns
        audit_stmt = select(AuditLogRecord).where(AuditLogRecord.user_id == user.id)
        audit_res = await db.execute(audit_stmt)
        user_audits = audit_res.scalars().all()
        assert len(user_audits) >= 1
