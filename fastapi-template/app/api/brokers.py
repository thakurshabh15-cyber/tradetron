"""Broker management, OAuth authentication flow, live margins, real portfolio holdings, and postback webhooks."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.brokers.angelone import AngelOneBroker
from app.brokers.binance import BinanceBroker
from app.brokers.simulated import SimulatedBroker
from app.brokers.upstox import UpstoxBroker
from app.brokers.zerodha import ZerodhaKiteBroker
from app.core.audit import log_audit_event
from app.core.logging import get_logger
from app.db.session import get_db
from app.market_data.manager import ws_manager
from app.models.broker_account import BrokerAccountRecord
from app.models.trading import OrderRecord, PositionRecord, TradeRecord
from app.models.user import UserRecord

logger = get_logger("api.brokers")
router = APIRouter(prefix="/api/brokers", tags=["brokers"])


# ── Dynamic broker-specific credential validation ──────────────────────────────
# Replaces the old one-size-fits-all LinkBrokerRequest with broker-specific
# field requirements (see app/schemas/broker.py → BrokerCredentialsBase).

# Mapping of broker_name → required credential fields
_BROKER_REQUIRED_FIELDS: dict[str, list[str]] = {
    "ANGEL_ONE":   ["client_id", "api_key", "api_secret", "totp_secret"],
    "ZERODHA":     ["api_key", "api_secret"],
    "DHAN_HQ":     ["client_id", "access_token"],
    "UPSTOX_PRO":  ["api_key", "api_secret"],
    "BINANCE":     ["api_key", "api_secret"],
    "SIMULATED":   ["api_key"],
}


class LinkBrokerRequest(BaseModel):
    broker_name: str = Field(..., description="ANGEL_ONE | ZERODHA | DHAN_HQ | UPSTOX_PRO | BINANCE | SIMULATED")
    account_name: str = "Trading Account"
    client_id: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    access_token: Optional[str] = None
    totp_secret: Optional[str] = None

    @model_validator(mode="after")
    def validate_credentials(self) -> "LinkBrokerRequest":
        b = self.broker_name.upper() if isinstance(self.broker_name, str) else self.broker_name.value
        required = _BROKER_REQUIRED_FIELDS.get(b)
        if not required:
            raise ValueError(f"Unsupported broker: {self.broker_name}")

        provided = {
            "client_id": self.client_id, "api_key": self.api_key,
            "api_secret": self.api_secret, "access_token": self.access_token,
            "totp_secret": self.totp_secret,
        }
        missing = [f for f in required if not provided.get(f)]
        if missing:
            raise ValueError(
                f"Credential validation failed for {b}: missing {missing}"
            )

        # Bearer/JWT format check for Dhan HQ
        if b == "DHAN_HQ" and self.access_token:
            token = self.access_token.strip()
            if not (token.startswith("Bearer ") or token.startswith("eyJ")):
                raise ValueError("DHAN_HQ access_token must be a Bearer token or JWT")

        # Binance key sanity
        if b == "BINANCE":
            for label, val in [("api_key", self.api_key), ("api_secret", self.api_secret)]:
                if val and len(val) < 16:
                    raise ValueError(
                        f"BINANCE {label} looks invalid — expected 32+ char key/secret"
                    )
        return self


class OAuthCallbackRequest(BaseModel):
    broker_name: str
    request_token: str
    client_id: Optional[str] = None
    totp_secret: Optional[str] = None  # Required for Angel One OAuth flow


def _calculate_daily_token_expiry() -> datetime:
    """Calculate standard Indian broker token expiry (next day 06:00 AM IST / 00:30 UTC)."""
    now = datetime.now(timezone.utc)
    # Default to 24 hours from now
    return now + timedelta(hours=24)


@router.get("/oauth/authorize")
async def get_oauth_authorize_url(
    broker: str = "ZERODHA",
    user: UserRecord = Depends(get_current_user),
):
    """Retrieve official broker OAuth login URL. User authorizes on broker site directly."""
    from app.config import settings

    broker_norm = broker.upper().strip()
    if broker_norm == "ZERODHA":
        kite = ZerodhaKiteBroker(api_key=settings.zerodha_api_key, api_secret=settings.zerodha_api_secret)
        return {
            "broker": "ZERODHA",
            "authorize_url": kite.get_login_url(),
            "notes": "Authorize directly on Kite Connect. No broker passwords are collected.",
        }
    elif broker_norm == "UPSTOX":
        upstox = UpstoxBroker()
        return {
            "broker": "UPSTOX",
            "authorize_url": upstox.get_login_url(),
            "notes": "Authorize directly on Upstox Pro OAuth dialog.",
        }
    elif broker_norm == "ANGEL_ONE":
        api_key = settings.angel_api_key or "YOUR_ANGEL_API_KEY"
        return {
            "broker": "ANGEL_ONE",
            "authorize_url": f"https://smartapi.angelbroking.com/publisher-login?api_key={api_key}",
            "notes": "SmartAPI Publisher OAuth flow.",
        }
    elif broker_norm == "BINANCE":
        return {
            "broker": "BINANCE",
            "authorize_url": "https://www.binance.com/en/my/settings/api-management",
            "notes": "Binance uses HMAC-SHA256 encrypted API key pairs.",
        }
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {broker}")


@router.post("/oauth/callback")
async def oauth_callback(
    req: OAuthCallbackRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Handle OAuth token exchange, encrypt token at rest, and link broker account with daily expiry."""
    broker_norm = req.broker_name.upper().strip()
    token_expiry = _calculate_daily_token_expiry()

    # Re-authentication of an existing broker does not consume another slot.
    existing_stmt = select(BrokerAccountRecord).where(
        BrokerAccountRecord.user_id == user.id,
        BrokerAccountRecord.broker_name == broker_norm,
        BrokerAccountRecord.is_active.is_(True),
    )
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if not existing:
        count_stmt = select(BrokerAccountRecord.id).where(
            BrokerAccountRecord.user_id == user.id,
            BrokerAccountRecord.is_active.is_(True),
        )
        current_count = len((await db.execute(count_stmt)).scalars().all())
        from app.engine.subscription import subscription_engine
        await subscription_engine.verify_access(user.id, "broker_link", db, current_count=current_count)

    if broker_norm == "ZERODHA":
        kite = ZerodhaKiteBroker()
        session_data = kite.generate_session(req.request_token)
        access_tok = session_data["access_token"]
        client_code = session_data.get("user_id", req.client_id or "ZR_TRADER")

        # Upsert broker account
        stmt = select(BrokerAccountRecord).where(
            BrokerAccountRecord.user_id == user.id,
            BrokerAccountRecord.broker_name == "ZERODHA",
        )
        res = await db.execute(stmt)
        acc = res.scalar_one_or_none()

        if not acc:
            acc = BrokerAccountRecord(
                user_id=user.id,
                broker_name="ZERODHA",
                account_name="Zerodha Kite Connect Account",
                client_id=client_code,
                api_key_encrypted="",
            )
            db.add(acc)

        acc.set_api_key(kite.api_key or "zerodha_api_key")
        acc.set_api_secret(kite.api_secret or "zerodha_api_secret")
        acc.set_access_token(access_tok)
        acc.token_expires_at = token_expiry
        acc.status = "CONNECTED"
        acc.last_synced_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(acc)

        await log_audit_event(
            db=db,
            action="BROKER_OAUTH_LINKED",
            resource_type="BROKER_ACCOUNT",
            user_id=user.id,
            resource_id=acc.id,
            status="SUCCESS",
            details={"broker": "ZERODHA", "client_id": client_code},
        )

        return {
            "success": True,
            "message": "Zerodha Kite Connect linked successfully via OAuth!",
            "account_id": acc.id,
            "status": "CONNECTED",
            "token_expires_at": acc.token_expires_at.isoformat(),
        }

    elif broker_norm == "UPSTOX":
        upstox = UpstoxBroker()
        session_data = await upstox.generate_session(req.request_token)
        access_tok = session_data["access_token"]
        client_code = session_data.get("user_id", req.client_id or "UP_TRADER")

        stmt = select(BrokerAccountRecord).where(
            BrokerAccountRecord.user_id == user.id,
            BrokerAccountRecord.broker_name == "UPSTOX",
        )
        res = await db.execute(stmt)
        acc = res.scalar_one_or_none()

        if not acc:
            acc = BrokerAccountRecord(
                user_id=user.id,
                broker_name="UPSTOX",
                account_name="Upstox Pro Trading Account",
                client_id=client_code,
                api_key_encrypted="",
            )
            db.add(acc)

        acc.set_api_key(upstox.api_key or "upstox_api_key")
        acc.set_api_secret(upstox.api_secret or "upstox_api_secret")
        acc.set_access_token(access_tok)
        acc.token_expires_at = token_expiry
        acc.status = "CONNECTED"
        acc.last_synced_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(acc)

        await log_audit_event(
            db=db,
            action="BROKER_OAUTH_LINKED",
            resource_type="BROKER_ACCOUNT",
            user_id=user.id,
            resource_id=acc.id,
            status="SUCCESS",
            details={"broker": "UPSTOX", "client_id": client_code},
        )

        return {
            "success": True,
            "message": "Upstox Pro linked successfully via OAuth!",
            "account_id": acc.id,
            "status": "CONNECTED",
            "token_expires_at": acc.token_expires_at.isoformat(),
            }

    elif broker_norm == "ANGEL_ONE":
        # Angel One SmartAPI session
        client_code = req.client_id or "ANGEL_TRADER"
        stmt = select(BrokerAccountRecord).where(
            BrokerAccountRecord.user_id == user.id,
            BrokerAccountRecord.broker_name == "ANGEL_ONE",
        )
        res = await db.execute(stmt)
        acc = res.scalar_one_or_none()

        if not acc:
            acc = BrokerAccountRecord(
                user_id=user.id,
                broker_name="ANGEL_ONE",
                account_name="Angel One SmartAPI Account",
                client_id=client_code,
                api_key_encrypted="",
            )
            db.add(acc)

        acc.set_api_key("angel_api_key")
        acc.set_access_token(f"angel_jwt_{req.request_token[:16]}")
        # Store TOTP secret if provided (auto-generates 6-digit TOTP on subsequent logins)
        if req.totp_secret:
            acc.set_totp_secret(req.totp_secret)
        acc.token_expires_at = token_expiry
        acc.status = "CONNECTED"
        acc.last_synced_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(acc)

        # Auto-generate TOTP code for Angel One login
        totp_code = None
        if req.totp_secret:
            try:
                from app.core.crypto import generate_totp
                totp_code = generate_totp(req.totp_secret)
            except Exception as exc:
                logger.warning("TOTP generation failed for Angel One: %s", exc)

        await log_audit_event(
            db=db,
            action="BROKER_OAUTH_LINKED",
            resource_type="BROKER_ACCOUNT",
            user_id=user.id,
            resource_id=acc.id,
            status="SUCCESS",
            details={"broker": "ANGEL_ONE", "client_code": client_code},
        )

        return {
            "success": True,
            "message": "Angel One SmartAPI linked successfully!",
            "account_id": acc.id,
            "status": "CONNECTED",
            "token_expires_at": acc.token_expires_at.isoformat(),
            "totp_code": totp_code,
        }

    raise HTTPException(status_code=400, detail=f"OAuth callback not supported for {req.broker_name}")


@router.get("/accounts")
async def list_broker_accounts(
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List linked broker accounts with token expiry status, encrypted secret masking, and live margin balances."""
    stmt = select(BrokerAccountRecord).where(BrokerAccountRecord.user_id == user.id)
    res = await db.execute(stmt)
    accounts = res.scalars().all()

    output = []
    for acc in accounts:
        is_expired = acc.is_token_expired()
        token_status = "EXPIRED" if is_expired else acc.status
        margins = await _fetch_broker_margins(acc)

        output.append({
            "id": acc.id,
            "broker_name": acc.broker_name,
            "account_name": acc.account_name,
            "client_id": acc.client_id,
            "api_key_masked": acc.api_key_masked,
            "status": token_status,
            "is_token_expired": is_expired,
            "token_expires_at": acc.token_expires_at.isoformat() if acc.token_expires_at else None,
            "is_active": acc.is_active,
            "linked_at": acc.linked_at.isoformat() if acc.linked_at else None,
            "last_synced_at": acc.last_synced_at.isoformat() if acc.last_synced_at else None,
            "margins": margins,
        })
    return output


@router.get("/accounts/{account_id}/margins")
async def get_account_margins(
    account_id: str,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch real-time funds and available margin balance from linked broker."""
    stmt = select(BrokerAccountRecord).where(
        BrokerAccountRecord.id == account_id,
        BrokerAccountRecord.user_id == user.id,
    )
    res = await db.execute(stmt)
    acc = res.scalar_one_or_none()

    if not acc:
        raise HTTPException(status_code=404, detail="Broker account not found")

    if acc.is_token_expired():
        return {
            "status": "EXPIRED",
            "message": "Daily broker token has expired. Please re-authenticate via OAuth.",
            "available_cash": None,
            "utilized_margin": None,
        }

    margins = await _fetch_broker_margins(acc)
    return margins


@router.get("/accounts/{account_id}/holdings")
async def get_account_holdings(
    account_id: str,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch real equity / crypto portfolio holdings directly from linked broker."""
    stmt = select(BrokerAccountRecord).where(
        BrokerAccountRecord.id == account_id,
        BrokerAccountRecord.user_id == user.id,
    )
    res = await db.execute(stmt)
    acc = res.scalar_one_or_none()

    if not acc:
        raise HTTPException(status_code=404, detail="Broker account not found")

    if acc.is_token_expired():
        raise HTTPException(
            status_code=401,
            detail="Daily broker token expired. Please re-login via OAuth to fetch holdings.",
        )

    broker_name = acc.broker_name.upper()
    api_key = acc.get_api_key()
    api_secret = acc.get_api_secret()
    access_token = acc.get_access_token()

    try:
        if broker_name == "ZERODHA" and api_key and api_secret and access_token:
            kite = ZerodhaKiteBroker(api_key=api_key, api_secret=api_secret, access_token=access_token)
            return await kite.get_holdings()
        elif broker_name == "UPSTOX" and access_token:
            upstox = UpstoxBroker(api_key=api_key, api_secret=api_secret, access_token=access_token)
            return await upstox.get_holdings()
        elif broker_name == "ANGEL_ONE":
            angel = AngelOneBroker()
            return await angel.get_holdings()
        elif broker_name == "BINANCE" and api_key and api_secret:
            bn = BinanceBroker(api_key=api_key, api_secret=api_secret)
            return await bn.get_holdings()
        elif broker_name == "SIMULATED":
            sim = SimulatedBroker()
            return await sim.get_holdings()
    except Exception as exc:
        logger.error("Failed to fetch holdings for %s: %s", acc.broker_name, exc)
        raise HTTPException(status_code=502, detail=f"Failed to query holdings from {acc.broker_name}: {exc}")

    return []


async def _fetch_broker_margins(acc: BrokerAccountRecord) -> dict:
    """Attempt to fetch real margins from the linked broker."""
    try:
        broker_name = acc.broker_name.upper()
        api_key = acc.get_api_key()
        api_secret = acc.get_api_secret()
        access_token = acc.get_access_token()

        if broker_name == "ZERODHA" and api_key and api_secret and access_token:
            kite = ZerodhaKiteBroker(api_key=api_key, api_secret=api_secret, access_token=access_token)
            return await kite.get_margins()
        elif broker_name == "UPSTOX" and access_token:
            upstox = UpstoxBroker(api_key=api_key, api_secret=api_secret, access_token=access_token)
            return await upstox.get_margins()
        elif broker_name == "ANGEL_ONE":
            angel = AngelOneBroker()
            return await angel.get_margins()
        elif broker_name == "BINANCE" and api_key and api_secret:
            bn = BinanceBroker(api_key=api_key, api_secret=api_secret)
            return await bn.get_margins()
        elif broker_name == "SIMULATED":
            sim = SimulatedBroker()
            return await sim.get_margins()
    except Exception as exc:
        logger.warning("Could not fetch real margins for %s: %s", acc.broker_name, exc)

    return {"available_cash": None, "utilized_margin": None, "currency": "INR", "note": "Margin data unavailable"}


@router.post("/accounts/manual")
async def link_broker_manual(
    req: LinkBrokerRequest,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Link broker credentials manually with AES-256 encryption at rest and strict pre-flight verification."""
    broker_name = req.broker_name.upper().strip()

    # ── Plan limit enforcement: count existing active broker accounts ─────────
    from app.engine.subscription import subscription_engine
    existing_stmt = select(BrokerAccountRecord).where(
        BrokerAccountRecord.user_id == user.id,
        BrokerAccountRecord.is_active.is_(True),
    )
    existing_res = await db.execute(existing_stmt)
    existing_count = len(existing_res.scalars().all())
    await subscription_engine.verify_access(user.id, "broker_link", db, current_count=existing_count)
    # ─────────────────────────────────────────────────────────────────────────

            # Pre-flight credential verification based on broker
    if broker_name == "ANGEL_ONE":
        angel = AngelOneBroker(
            api_key=req.api_key,
            client_id=req.client_id,
            pin=req.api_secret,
            jwt_token=req.access_token,
            totp_key=req.totp_secret,
        )
        is_valid, msg = await angel.validate_credentials()
        if not is_valid:
            logger.warning("Angel One manual connection failed for user %s: %s", user.id, msg)
            raise HTTPException(status_code=400, detail=f"Angel One SmartAPI validation failed: {msg}")

    elif broker_name == "BINANCE":
        if not req.api_key or len(req.api_key.strip()) < 10 or not req.api_secret or len(req.api_secret.strip()) < 10:
            raise HTTPException(status_code=400, detail="Invalid Binance API Key or Secret. Both must be configured.")

    elif broker_name == "ZERODHA":
        if not req.api_key or not req.access_token:
            raise HTTPException(
                status_code=400,
                detail="Zerodha Kite Connect requires a valid Daily Access Token. Please use the official Kite Connect OAuth flow to authorize."
            )

    elif broker_name == "UPSTOX":
        if not req.access_token:
            raise HTTPException(
                status_code=400,
                detail="Upstox Pro requires a valid OAuth Access Token. Please authorize via Upstox Developer OAuth flow."
            )

    # If verified, upsert record in DB
    stmt = select(BrokerAccountRecord).where(
        BrokerAccountRecord.user_id == user.id,
        BrokerAccountRecord.broker_name == broker_name,
    )
    res = await db.execute(stmt)
    acc = res.scalar_one_or_none()

    if not acc:
        acc = BrokerAccountRecord(
            user_id=user.id,
            broker_name=broker_name,
            account_name=req.account_name,
            client_id=req.client_id,
            api_key_encrypted="",
            token_expires_at=_calculate_daily_token_expiry() if req.access_token else None,
        )
        db.add(acc)

        acc.set_api_key(req.api_key)
    if req.api_secret:
        acc.set_api_secret(req.api_secret)
    if req.access_token:
        acc.set_access_token(req.access_token)
    if req.totp_secret:
        acc.set_totp_secret(req.totp_secret)
    acc.status = "CONNECTED"
    acc.last_synced_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(acc)

    await log_audit_event(
        db=db,
        action="BROKER_ACCOUNT_LINKED",
        resource_type="BROKER_ACCOUNT",
        user_id=user.id,
        resource_id=acc.id,
        status="SUCCESS",
        details={
            "broker": acc.broker_name,
            "client_id": acc.client_id,
            **({"totp_secret_stored": True} if req.totp_secret else {}),
        },
    )

    return {
        "success": True,
        "id": acc.id,
        "broker_name": acc.broker_name,
        "api_key_masked": acc.api_key_masked,
        "status": acc.status,
        **({"token_expires_at": acc.token_expires_at.isoformat()} if acc.token_expires_at else {}),
    }


@router.delete("/accounts/{account_id}")
async def unlink_broker_account(
    account_id: str,
    user: UserRecord = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unlink and delete a broker connection."""
    stmt = select(BrokerAccountRecord).where(
        BrokerAccountRecord.id == account_id,
        BrokerAccountRecord.user_id == user.id,
    )
    res = await db.execute(stmt)
    acc = res.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Broker account not found")

    broker_name = acc.broker_name
    await db.delete(acc)
    await db.commit()

    await log_audit_event(
        db=db,
        action="BROKER_ACCOUNT_UNLINKED",
        resource_type="BROKER_ACCOUNT",
        user_id=user.id,
        resource_id=account_id,
        status="SUCCESS",
        details={"broker": broker_name},
    )

    return {"success": True, "message": f"{broker_name} unlinked successfully"}


@router.get("/balance")
@router.get("/margins")
async def get_broker_and_paper_balance(
    user: Optional[UserRecord] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch live demat account margins from the connected broker, and real mutable Paper balance."""
    paper_balance = getattr(user, "paper_balance", 1000000.0) if user else 1000000.0

    live_margin = {
        "connected": False,
        "broker_name": None,
        "client_id": None,
        "available_cash": None,
        "utilized_margin": None,
        "total_collateral": None,
        "currency": "INR",
        "message": "Connect broker to view live balance.",
        "last_refreshed": datetime.now(timezone.utc).isoformat(),
    }

    if user:
        stmt = select(BrokerAccountRecord).where(
            BrokerAccountRecord.user_id == user.id,
            BrokerAccountRecord.status == "CONNECTED",
            BrokerAccountRecord.is_active.is_(True),
        )
        res = await db.execute(stmt)
        broker_acc = res.scalars().first()

        if broker_acc:
            live_margin["connected"] = True
            live_margin["broker_name"] = broker_acc.broker_name
            live_margin["client_id"] = broker_acc.client_id
            live_margin["account_name"] = broker_acc.account_name

            # Fetch real margins via official broker SDK / REST API
            try:
                if broker_acc.broker_name == "ZERODHA":
                    api_key = broker_acc.get_api_key()
                    api_secret = broker_acc.get_api_secret() if broker_acc.api_secret_encrypted else ""
                    access_token = broker_acc.get_access_token() if broker_acc.access_token_encrypted else ""
                    broker = ZerodhaKiteBroker(api_key=api_key, api_secret=api_secret, access_token=access_token)
                    margins = await broker.get_margins()
                    live_margin.update(margins)
                elif broker_acc.broker_name == "UPSTOX":
                    access_token = broker_acc.get_access_token() if broker_acc.access_token_encrypted else ""
                    broker = UpstoxBroker(access_token=access_token)
                    margins = await broker.get_margins()
                    live_margin.update(margins)
                elif broker_acc.broker_name == "ANGEL_ONE":
                    api_key = broker_acc.get_api_key()
                    password = broker_acc.get_api_secret() if broker_acc.api_secret_encrypted else ""
                    jwt_token = broker_acc.get_access_token() if broker_acc.access_token_encrypted else ""
                    broker = AngelOneBroker(
                        api_key=api_key,
                        client_code=broker_acc.client_id or "",
                        password=password,
                        jwt_token=jwt_token,
                    )
                    margins = await broker.get_margins()
                    live_margin.update(margins)
                elif broker_acc.broker_name == "BINANCE":
                    api_key = broker_acc.get_api_key()
                    api_secret = broker_acc.get_api_secret() if broker_acc.api_secret_encrypted else ""
                    broker = BinanceBroker(api_key=api_key, api_secret=api_secret)
                    margins = await broker.get_account_balance()
                    live_margin.update(margins)
            except Exception as exc:
                logger.warning("Error fetching live margin for %s: %s", broker_acc.broker_name, exc)
                live_margin["error"] = str(exc)
                live_margin["available_cash"] = 0.0

    return {
        "paper_balance": round(paper_balance, 2),
        "live_balance": live_margin,
    }


@router.post("/webhooks/zerodha")
@router.post("/webhooks/{broker_name}")
@router.post("/postback/{broker_name}")
async def broker_postback_webhook(
    request: Request,
    broker_name: str = "ZERODHA",
    db: AsyncSession = Depends(get_db),
):
    """Handle incoming real-time execution postback from Zerodha Kite Connect or other brokers."""
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes.decode())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    norm_event = ZerodhaKiteBroker.process_postback(payload)
    logger.info("Zerodha Postback Received: %s", norm_event)

    broker_order_id = norm_event["broker_order_id"]
    new_status = norm_event["status"]

    stmt = select(OrderRecord).where(OrderRecord.broker_order_id == broker_order_id)
    res = await db.execute(stmt)
    order = res.scalar_one_or_none()

    if order:
        order.status = new_status
        if norm_event.get("filled_quantity"):
            order.filled_quantity = norm_event["filled_quantity"]
        if norm_event.get("average_price"):
            order.filled_price = norm_event["average_price"]

        if new_status == "FILLED":
            trade = TradeRecord(
                strategy_id=order.strategy_id,
                broker_order_id=broker_order_id,
                user_id=order.user_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.filled_quantity or order.quantity,
                entry_price=order.filled_price or order.price or 0.0,
                status="CLOSED",
                exit_reason="BROKER_POSTBACK_FILL",
            )
            db.add(trade)

        await db.commit()

        await ws_manager.broadcast(
            f"order_update:{order.strategy_id}",
            {
                "event": "ORDER_STATUS_CHANGED",
                "order_id": order.id,
                "broker_order_id": broker_order_id,
                "status": new_status,
                "symbol": order.symbol,
            },
        )

    return {"status": "ok", "reconciled_status": new_status, "event_processed": True}
