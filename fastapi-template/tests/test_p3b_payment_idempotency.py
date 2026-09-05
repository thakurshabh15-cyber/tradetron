"""Phase 3-B regression tests: payment verification idempotency (replay defense).

Verifies the P1 fix in ``app/api/billing.py`` → ``verify_payment()``:

- the FIRST verified payment grants the subscription term exactly once
- REPLAYING the same verified order (same signature) must NOT extend the
  subscription term or mint duplicate invoices — it returns the existing grant
- the existing billing flow (create-order → verify → invoice) is unchanged

Before the fix, each verified verify-payment call reset ``start_date=now``,
pushed ``end_date`` forward another 30/365 days and stamped a new PAID invoice,
letting a single payment farm unlimited plan time and corrupt invoice history.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.payment_gateway import razorpay_gateway
from app.db.session import init_db
from app.main import app


@pytest.mark.asyncio
async def test_verify_payment_replay_is_idempotent():
    """Replaying the same verified order must not re-grant or duplicate invoices."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uid = int(time.time() * 1000) % 1000000
        reg_res = await client.post(
            "/api/auth/register",
            json={
                "email": f"p3b_replay_{uid}@tradetron.io",
                "password": "SecurePassword123!",
                "full_name": "P3B Replay User",
            },
        )
        assert reg_res.status_code == 201, reg_res.text
        headers = {"Authorization": f"Bearer {reg_res.json()['access_token']}"}

        # 1. Create a real checkout order
        order_res = await client.post(
            "/api/billing/create-order",
            json={"plan_name": "PRO", "billing_cycle": "MONTHLY"},
            headers=headers,
        )
        assert order_res.status_code == 200, order_res.text
        order_id = order_res.json()["order_id"]

        # 2. Verify once with a valid HMAC signature
        payment_id = f"pay_p3b_{uid}"
        sig = razorpay_gateway.generate_mock_signature(order_id, payment_id)
        payload = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": sig,
            "plan_name": "PRO",
            "billing_cycle": "MONTHLY",
        }
        first = await client.post("/api/billing/verify-payment", json=payload, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["subscription"]["plan_name"] == "PRO"
        first_invoice = first.json()["invoice_number"]
        assert first_invoice, "first verification must produce an invoice"

        # 3. Snapshot granted subscription term + invoice ledger
        sub_before = (await client.get("/api/billing/subscription", headers=headers)).json()
        assert sub_before["plan_name"] == "PRO"
        start_before = sub_before["start_date"]
        end_before = sub_before["end_date"]

        # 4. REPLAY the exact same verified payment
        time.sleep(1.1)  # ensure a re-grant would observably push end_date forward
        second = await client.post("/api/billing/verify-payment", json=payload, headers=headers)
        assert second.status_code == 200, second.text
        assert second.json()["subscription"]["plan_name"] == "PRO"
        assert second.json()["invoice_number"] == first_invoice, (
            "replay must return the existing invoice, not mint a duplicate"
        )

        # 5. The granted term must NOT have been extended or reset
        sub_after = (await client.get("/api/billing/subscription", headers=headers)).json()
        assert sub_after["start_date"] == start_before, (
            "replay must not reset the subscription start date"
        )
        assert sub_after["end_date"] == end_before, (
            "replay must not push the subscription end date forward"
        )

        # 6. The invoice ledger must still hold exactly one invoice for this grant
        invoices = (await client.get("/api/billing/invoices", headers=headers)).json()
        assert len(invoices) == 1, "replay must not create duplicate invoices"
        assert invoices[0]["invoice_number"] == first_invoice


@pytest.mark.asyncio
async def test_verify_payment_ownership_boundary():
    """A foreign order id must not activate a plan for the caller."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uid = int(time.time() * 1000) % 1000000

        # Victim creates their own order
        reg_a = await client.post(
            "/api/auth/register",
            json={"email": f"p3b_own_a_{uid}@tradetron.io", "password": "SecurePassword123!"},
        )
        headers_a = {"Authorization": f"Bearer {reg_a.json()['access_token']}"}
        order_res = await client.post(
            "/api/billing/create-order",
            json={"plan_name": "PRO", "billing_cycle": "MONTHLY"},
            headers=headers_a,
        )
        order_id = order_res.json()["order_id"]

        # Attacker verifies the victim's order with a forged plan claim
        reg_b = await client.post(
            "/api/auth/register",
            json={"email": f"p3b_own_b_{uid}@tradetron.io", "password": "SecurePassword123!"},
        )
        headers_b = {"Authorization": f"Bearer {reg_b.json()['access_token']}"}
        forged = await client.post(
            "/api/billing/verify-payment",
            json={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": f"pay_forged_{uid}",
                "razorpay_signature": razorpay_gateway.generate_mock_signature(order_id, f"pay_forged_{uid}"),
                "plan_name": "PRO",
                "billing_cycle": "MONTHLY",
            },
            headers=headers_b,
        )
        # A caller may never activate a plan with a foreign order: the server
        # rejects it either as an unknown order (400 — no resource enumeration)
        # or, defensively, as a cross-tenant violation (403).
        assert forged.status_code in (400, 403), forged.text

        # The attacker's subscription must remain untouched on FREE
        sub_b = (await client.get("/api/billing/subscription", headers=headers_b)).json()
        assert sub_b["plan_name"] == "FREE"


@pytest.mark.asyncio
async def test_create_order_never_dials_real_razorpay(monkeypatch):
    """Test-mode checkout must be fully sandboxed — no outbound HTTP to Razorpay.

    Guards against environment drift where env-supplied Razorpay keys would
    otherwise flip the gateway into live mode and hit api.razorpay.com during
    tests (mission rule: tests never contact Razorpay production).
    """
    import httpx

    real_post = httpx.AsyncClient.post

    async def _guarded_post(self, *args, **kwargs):
        url = kwargs.get("url") or (args[0] if args else "")
        if "api.razorpay.com" in str(url):
            raise AssertionError("RAZORPAY NETWORK CALL ATTEMPTED DURING TESTS")
        return await real_post(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _guarded_post)

    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uid = int(time.time() * 1000) % 1000000
        reg = await client.post(
            "/api/auth/register",
            json={
                "email": f"p3b_sandbox_{uid}@tradetron.io",
                "password": "SecurePassword123!",
            },
        )
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        order = await client.post(
            "/api/billing/create-order",
            json={"plan_name": "PRO", "billing_cycle": "MONTHLY"},
            headers=headers,
        )
        assert order.status_code == 200, order.text
        data = order.json()
        assert data["order_id"].startswith("order_"), "sandbox order id expected"
        assert data["amount"] == 799900  # paise — backend-authoritative pricing