"""Broker credential schemas with broker-specific dynamic validation.

Each broker (Angel One, Zerodha Kite, Dhan HQ, Upstox Pro, Binance) has a
unique set of required credentials validated here via model_validator.
"""

from __future__ import annotations

from enum import Enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── Supported brokers ────────────────────────────────────────────────────────

class BrokerName(str, Enum):
    """Canonical broker identifier enum."""
    ANGEL_ONE = "ANGEL_ONE"
    ZERODHA = "ZERODHA"
    DHAN_HQ = "DHAN_HQ"
    UPSTOX_PRO = "UPSTOX_PRO"
    BINANCE = "BINANCE"
    SIMULATED = "SIMULATED"


# ── Base & dynamic credential schemas ────────────────────────────────────────

class BrokerCredentialsBase(BaseModel):
    """Common base with dynamic validation dispatched by broker_name."""

    broker_name: BrokerName = Field(..., description="Broker identifier")
    account_name: str = Field("Trading Account", max_length=100)
    client_id: Optional[str] = Field(None, max_length=100)
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    access_token: Optional[str] = None
    totp_secret: Optional[str] = None
    refresh_token: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)

    @model_validator(mode="after")
    def validate_broker_fields(self) -> "BrokerCredentialsBase":
        b = self.broker_name
        if isinstance(b, BrokerName):
            b = b.value

        required: dict[str, list[str]] = {
            "ANGEL_ONE": ["client_id", "api_key", "api_secret", "totp_secret"],
            "ZERODHA": ["api_key", "api_secret"],
            "DHAN_HQ": ["client_id", "access_token"],
            "UPSTOX_PRO": ["api_key", "api_secret"],
            "BINANCE": ["api_key", "api_secret"],
            "SIMULATED": ["api_key"],
        }

        present = {k: v for k in ["client_id", "api_key", "api_secret",
                                   "access_token", "totp_secret"] if (v := getattr(self, k))}
        missing = [f for f in required.get(b, []) if not present.get(f)]
        if missing:
            raise ValueError(
                f"Credential validation failed for {b}: missing {missing}"
            )

        # Bearer-token format check for Dhan HQ
        if b == "DHAN_HQ" and self.access_token:
            token = self.access_token.strip()
            if not (token.startswith("Bearer ") or token.startswith("eyJ")):
                raise ValueError(
                    "DHAN_HQ access_token must be a Bearer token or JWT"
                )

        # Binance HMAC / Ed25519 key sanity
        if b == "BINANCE":
            for label, val in [("api_key", self.api_key), ("api_secret", self.api_secret)]:
                if val and len(val) < 16:
                    raise ValueError(
                        f"BINANCE {label} looks invalid — expected 32+ char key/secret"
                    )

        return self


class BrokerCredentials(BrokerCredentialsBase):
    """Submitted by frontend for manual credential entry."""
    pass


class BrokerCredentialsOut(BaseModel):
    """Masked / non-sensitive representation returned to frontend."""

    id: str
    broker_name: str
    account_name: str
    client_id: Optional[str]
    api_key_masked: str
    token_expires_at: Optional[datetime] = None
    status: str = "CONNECTED"
    is_active: bool = True


class OAuthCallbackRequest(BaseModel):
    broker_name: str
    request_token: str
    client_id: Optional[str] = None


class BrokerAccountRead(BaseModel):
    """Full account record summary for frontend tables."""

    id: str
    broker_name: str
    account_name: str
    client_id: Optional[str]
    api_key_masked: str
    is_token_expired: bool
    token_expires_at: Optional[datetime]
    status: str
    connected: bool = True
