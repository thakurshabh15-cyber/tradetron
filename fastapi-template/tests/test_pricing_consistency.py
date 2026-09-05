"""P2 canonical-pricing regression suite.

Business rule (decided 2026-09): the lowest customer-facing PAID plan starts at
₹7,999/mo. The database is the authoritative runtime amount source, annual
pricing preserves the established ~20% discount (yearly = 9.6 × monthly, floored),
and the legacy ₹1,499 / ₹1,999 / ₹4,999 rates must not exist as active runtime
pricing anywhere.

Razorpay rule: ₹7,999/mo must become exactly 799900 paise server-side (the
gateway converts rupees->paise, so the client can never dictate the amount).

Ladder (canonical):  FREE 0/0 · PRO 7,999/76,790 · CREATOR 14,999/1,43,990
                      INSTITUTIONAL 24,999/2,39,990 (ELITE = legacy alias).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.payment_gateway import razorpay_gateway
from app.db.session import init_db
from app.engine.subscription import PLAN_LIMITS
from app.main import app

BASE_DIR = Path(__file__).resolve().parent.parent

# Canonical catalogue — (monthly, yearly); yearly = floor(monthly × 9.6) = 20%-off.
CANONICAL: dict[str, tuple[int, int]] = {
    "FREE": (0, 0),
    "PRO": (7999, 76790),
    "CREATOR": (14999, 143990),
    "INSTITUTIONAL": (24999, 239990),
    "ELITE": (24999, 239990),  # legacy alias -> INSTITUTIONAL
}

LEGACY_PRICE_TOKENS = ("1499", "14390", "1999", "19190", "4999", "47990", "49990")

# Active runtime pricing sources (excludes historical docs/migrations/tests).
RUNTIME_SOURCES = (
    BASE_DIR / "app/engine/subscription.py",
    BASE_DIR / "app/db/session.py",
    BASE_DIR / "app/api/billing.py",
    BASE_DIR / "app/api/payouts.py",
    BASE_DIR / "app/webhooks/handlers/billing.py",
    BASE_DIR / "client/src/pages/Pricing.jsx",
)


def _yearly_from_monthly(monthly: int) -> int:
    """Established annual model: monthly * 12 * 0.8 == monthly * 9.6 (floored)."""
    return monthly * 96 // 10


async def _api_plans() -> tuple[list[dict], list[dict]]:
    """Return (/api/billing/plans, /api/subscriptions/plans) after boot-seeding."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        billing = (await client.get("/api/billing/plans")).json()
        subs = (await client.get("/api/subscriptions/plans")).json()
    return billing, subs


async def test_engine_plan_limits_match_canonical():
    for plan, (monthly, yearly) in CANONICAL.items():
        assert plan in PLAN_LIMITS, f"{plan} missing from engine PLAN_LIMITS"
        assert PLAN_LIMITS[plan]["price_monthly"] == monthly, plan
        assert PLAN_LIMITS[plan]["price_yearly"] == yearly, plan


async def test_lowest_paid_plan_is_7999_in_db_and_api():
    billing, subs = await _api_plans()
    paid = [p["price_monthly"] for p in billing if p["price_monthly"] > 0]
    assert min(paid) == 7999, f"lowest paid monthly price must be ₹7,999, got {min(paid)}"

    pro_billing = next(p for p in billing if p["name"] == "PRO")
    assert pro_billing["price_monthly"] == 7999.0
    assert pro_billing["price_yearly"] == 76790.0

    sub_by_code = {p["code"]: p for p in subs}  # /api/subscriptions/plans excludes ELITE alias
    assert sub_by_code["PRO"]["price_monthly"] == 7999
    assert sub_by_code["PRO"]["price_yearly"] == 76790
    assert sub_by_code["INSTITUTIONAL"]["price_monthly"] == 24999
    assert sub_by_code["INSTITUTIONAL"]["price_yearly"] == 239990


async def test_razorpay_7999_converts_to_799900_paise():
    order = await razorpay_gateway.create_order(amount=7999)
    assert order["amount"] == 799900
    assert order["amount"] == int(round(7999 * 100))
    # Full ladder through the gateway (rupees -> paise, server-side only).
    assert (await razorpay_gateway.create_order(amount=14999))["amount"] == 1499900
    assert (await razorpay_gateway.create_order(amount=24999))["amount"] == 2499900
    assert (await razorpay_gateway.create_order(amount=76790))["amount"] == 7679000
    assert (await razorpay_gateway.create_order(amount=143990))["amount"] == 14399000
    assert (await razorpay_gateway.create_order(amount=239990))["amount"] == 23999000


async def test_backend_amount_is_authoritative_client_cannot_override():
    """A hostile client cannot force a cheaper checkout amount."""
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uid = int(time.time() * 1000) % 1000000
        reg = await client.post(
            "/api/auth/register",
            json={
                "email": f"pricing_guard_{uid}@tradetron.io",
                "password": "SecurePassword123!",
                "full_name": "Pricing Guard",
            },
        )
        assert reg.status_code == 201
        token = reg.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Extra client-supplied amount fields must be ignored by the backend.
        order_res = await client.post(
            "/api/billing/create-order",
            json={"plan_name": "PRO", "billing_cycle": "MONTHLY", "amount": 1, "amount_rupees": 1},
            headers=headers,
        )
        assert order_res.status_code == 200
        data = order_res.json()
        assert data["amount_rupees"] == 7999.0
        assert data["amount"] == 799900  # paise — from DB plan, never the client


def test_legacy_pricing_absent_from_runtime_sources():
    pattern = re.compile(r"\b(?:%s)\b" % "|".join(LEGACY_PRICE_TOKENS))
    hits: list[str] = []
    for path in RUNTIME_SOURCES:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            hits.append(f"{path.relative_to(BASE_DIR)}:{line_no}: {match.group(0)}")
    assert not hits, "legacy active-pricing tokens found in runtime sources:\n" + "\n".join(hits)


async def test_db_seed_annual_pricing_preserves_20_percent_discount():
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.billing import PlanRecord

    await init_db()
    async with SessionLocal() as session:
        rows = (await session.execute(select(PlanRecord))).scalars().all()
    assert rows, "expected seeded plan rows"
    for plan in rows:
        if plan.price_monthly <= 0:
            continue
        assert int(plan.price_yearly) == _yearly_from_monthly(int(plan.price_monthly)), plan.name


async def test_db_pricing_reconcile_converges_stale_rows():
    """An existing DB holding legacy prices must converge on the next boot."""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.billing import PlanRecord

    await init_db()
    async with SessionLocal() as session:
        pro = (await session.execute(select(PlanRecord).where(PlanRecord.name == "PRO"))).scalar_one()
        pro.price_monthly = 1499.0  # simulate a legacy pre-P2 database row
        pro.price_yearly = 14390.0
        await session.commit()

    await init_db()  # boot reconcile must converge pricing back to canonical

    async with SessionLocal() as session:
        pro = (await session.execute(select(PlanRecord).where(PlanRecord.name == "PRO"))).scalar_one()
        assert pro.price_monthly == 7999.0
        assert pro.price_yearly == 76790.0
        elite = (await session.execute(select(PlanRecord).where(PlanRecord.name == "ELITE"))).scalar_one()
        assert elite.price_monthly == 24999.0
        assert elite.price_yearly == 239990.0


def test_frontend_fallback_matches_backend_canonical():
    src = (BASE_DIR / "client/src/pages/Pricing.jsx").read_text(encoding="utf-8", errors="replace")
    entries = re.findall(r'code: "([A-Z]+)".*?price_monthly: (\d+), price_yearly: (\d+)', src)
    assert entries, "could not parse fallback plan entries from Pricing.jsx"
    by_code = {code: (int(monthly), int(yearly)) for code, monthly, yearly in entries}
    for code in ("FREE", "PRO", "CREATOR", "INSTITUTIONAL"):
        assert by_code.get(code) == CANONICAL[code], code
    assert "ELITE" not in by_code  # frontend catalogue uses the INSTITUTIONAL name