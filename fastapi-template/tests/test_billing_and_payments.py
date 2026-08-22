"""Unit tests for Razorpay Payment Gateway, Database Plans, Subscriptions, Webhooks, and GST Invoices."""

import time
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.db.session import init_db
from app.core.payment_gateway import razorpay_gateway


async def test_billing_and_payments_suite():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Register Auth User
        uid = int(time.time() * 1000) % 1000000
        reg_res = await client.post("/api/auth/register", json={
            "email": f"billing_trader_{uid}@tradetron.io",
            "password": "SecurePassword123!",
            "full_name": "Billing Test User",
        })
        assert reg_res.status_code == 201
        reg_data = reg_res.json()
        token = reg_data["access_token"]
        user_id_str = reg_data["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Test Get Plans from Database
        plans_res = await client.get("/api/billing/plans")
        assert plans_res.status_code == 200
        plans = plans_res.json()
        assert len(plans) >= 3
        plan_names = [p["name"] for p in plans]
        assert "FREE" in plan_names
        assert "PRO" in plan_names
        assert "ELITE" in plan_names

        # 3. Test Initial Default Free Subscription
        sub_res = await client.get("/api/billing/subscription", headers=headers)
        assert sub_res.status_code == 200
        sub_data = sub_res.json()
        assert sub_data["plan_name"] == "FREE"
        assert sub_data["status"] == "ACTIVE"

        # 4. Test Create Razorpay Order
        order_res = await client.post("/api/billing/create-order", json={
            "plan_name": "PRO",
            "billing_cycle": "MONTHLY",
        }, headers=headers)
        assert order_res.status_code == 200
        order_data = order_res.json()
        assert "order_id" in order_data
        assert order_data["amount_rupees"] == 1999.0
        assert order_data["amount"] == 199900  # paise
        assert order_data["currency"] == "INR"
        order_id = order_data["order_id"]

        # 5. Test Payment Verification with Invalid Signature -> Must Fail
        bad_verify_res = await client.post("/api/billing/verify-payment", json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_fake_998877",
            "razorpay_signature": "invalid_signature_hash",
            "plan_name": "PRO",
            "billing_cycle": "MONTHLY",
        }, headers=headers)
        assert bad_verify_res.status_code == 400
        assert "Invalid transaction signature" in bad_verify_res.json()["detail"]

        # 6. Test Payment Verification with Valid HMAC Signature -> Must Succeed & Upgrade
        payment_id = f"pay_{uid}_valid"
        valid_signature = razorpay_gateway.generate_mock_signature(order_id, payment_id)

        good_verify_res = await client.post("/api/billing/verify-payment", json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": valid_signature,
            "plan_name": "PRO",
            "billing_cycle": "MONTHLY",
        }, headers=headers)
        assert good_verify_res.status_code == 200
        verify_data = good_verify_res.json()
        assert verify_data["success"] is True
        assert verify_data["subscription"]["plan_name"] == "PRO"
        assert "invoice_number" in verify_data
        invoice_number = verify_data["invoice_number"]

        # 7. Test User Subscription is now PRO with unlocked features
        upgraded_sub_res = await client.get("/api/billing/subscription", headers=headers)
        assert upgraded_sub_res.status_code == 200
        upgraded_data = upgraded_sub_res.json()
        assert upgraded_data["plan_name"] == "PRO"
        assert upgraded_data["features"]["max_live_strategies"] == 10
        assert upgraded_data["features"]["priority_support"] is True

        # 8. Test Invoices List & Download
        invoices_res = await client.get("/api/billing/invoices", headers=headers)
        assert invoices_res.status_code == 200
        invoices = invoices_res.json()
        assert len(invoices) >= 1
        target_invoice = invoices[0]
        assert target_invoice["invoice_number"] == invoice_number
        assert target_invoice["total_amount"] == 1999.0

        download_res = await client.get(f"/api/billing/invoices/{target_invoice['id']}/download", headers=headers)
        assert download_res.status_code == 200
        assert "TAX INVOICE" in download_res.text
        assert "GSTIN" in download_res.text
        assert invoice_number in download_res.text

        # 9. Test Cancel Subscription
        cancel_res = await client.post("/api/billing/cancel-subscription", headers=headers)
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "CANCELLED"

        # 10. Test Razorpay Webhook Processing (Signature verified -> Updates DB subscription & creates invoice)
        webhook_payload = {
            "entity": "event",
            "account_id": "acc_tradetron_test",
            "event": "payment.captured",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_webhook_live_test_7788",
                        "order_id": order_id,
                        "status": "captured",
                        "amount": 199900,
                        "currency": "INR",
                        "notes": {
                            "user_id": user_id_str,
                            "plan_name": "ELITE",
                            "billing_cycle": "MONTHLY",
                        },
                    }
                }
            },
            "created_at": int(time.time()),
        }
        import json
        payload_bytes = json.dumps(webhook_payload).encode()
        import hmac, hashlib
        sig = hmac.new(b"rzp_test_tradetron_webhook_secret", payload_bytes, hashlib.sha256).hexdigest()

        webhook_res = await client.post(
            "/api/billing/webhook/razorpay",
            content=payload_bytes,
            headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
        )
        assert webhook_res.status_code == 200
        assert webhook_res.json()["status"] == "ok"
        assert webhook_res.json()["event"] == "payment.captured"
