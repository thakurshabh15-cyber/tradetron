"""Security, JWT Tokens, Password Hashing, and OTP Verification.

Implements standard RFC 7519 JWT generation with HMAC-SHA256, secure PBKDF2 password
hashing, and TOTP/Hotp OTP verification.
Token rules:
- Access token: 15 minutes
- Refresh token: 7 days
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings

# ── TOKEN LIFETIME RULES ──────────────────────────────────────────────────────
# Both values are environment-configurable (.env.production):
#   JWT_ALGORITHM=HS256, ACCESS_TOKEN_EXPIRE_MINUTES=1440
_ACCESS_TOKEN_EXPIRE_MINUTES_CFG: int = max(1, settings.access_token_expire_minutes)
ACCESS_TOKEN_EXPIRE_MINUTES: int = _ACCESS_TOKEN_EXPIRE_MINUTES_CFG
REFRESH_TOKEN_EXPIRE_DAYS: int = 7
JWT_SECRET: str = settings.jwt_secret

# Algorithm lock-down: the signing implementation below is strictly
# HMAC-SHA256. Any configured value other than "HS256" would create an
# algorithm-confusion vulnerability (header claims ≠ actual cipher), so we
# hard-clamp to HS256 and warn loudly on misconfiguration.
_CONFIGURED_JWT_ALGORITHM: str = settings.jwt_algorithm.strip().upper()
JWT_ALGORITHM: str = "HS256"
if _CONFIGURED_JWT_ALGORITHM != JWT_ALGORITHM:
    import logging

    logging.getLogger(__name__).warning(
        "JWT_ALGORITHM=%s requested but only HS256 is supported — enforcing HS256.",
        _CONFIGURED_JWT_ALGORITHM or "(empty)",
    )



# ── PASSWORD HASHING (PBKDF2-HMAC-SHA256) ────────────────────────────────────
def hash_password(password: str) -> str:
    """Hash password using PBKDF2 with 100,000 iterations and random 16-byte salt."""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000
    )
    return f"pbkdf2:sha256:100000${salt}${key.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against stored hash."""
    try:
        parts = hashed_password.split("$")
        if len(parts) != 3:
            return False
        _, salt, stored_key = parts
        key = hashlib.pbkdf2_hmac(
            "sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), 100_000
        )
        return hmac.compare_digest(key.hex(), stored_key)
    except Exception:
        return False


# ── RFC 7519 JWT ENCODING & DECODING ─────────────────────────────────────────
def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64_decode(data: str) -> bytes:
    padding = 4 - (len(data) % 4)
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data.encode("ascii"))


def create_access_token(
    data: dict[str, Any], expires_delta: timedelta | None = None
) -> str:
    """Create a short-lived access token (default 15 minutes)."""
    to_encode = data.copy()
    now = int(time.time())
    if expires_delta:
        exp = now + int(expires_delta.total_seconds())
    else:
        exp = now + (ACCESS_TOKEN_EXPIRE_MINUTES * 60)

    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    payload = {**to_encode, "exp": exp, "iat": now, "type": "access"}

    header_b64 = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(
        JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256
    ).digest()
    signature_b64 = _b64_encode(signature)

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode and verify JWT signature and expiration time."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, signature_b64 = parts

        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected_sig = hmac.new(
            JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256
        ).digest()

        actual_sig = _b64_decode(signature_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload = json.loads(_b64_decode(payload_b64).decode("utf-8"))
        now = int(time.time())
        if "exp" in payload and payload["exp"] < now:
            return None  # Token expired

        return payload
    except Exception:
        return None


def hash_token(token: str) -> str:
    """Compute SHA-256 hash of a token string for secure revocation indexing."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_refresh_token(data: dict[str, Any], remember_me: bool = False) -> str:
    """Create a long-lived refresh token (7 days default, 30 days if remember_me)."""
    to_encode = data.copy()
    now = int(time.time())
    days = 30 if remember_me else REFRESH_TOKEN_EXPIRE_DAYS
    exp = now + (days * 86400)

    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    payload = {**to_encode, "exp": exp, "iat": now, "type": "refresh", "jti": secrets.token_hex(16)}

    header_b64 = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(
        JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256
    ).digest()
    signature_b64 = _b64_encode(signature)

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def create_password_reset_token(user_id: str, email: str) -> str:
    """Create a short-lived signed password reset token (15 minutes)."""
    now = int(time.time())
    exp = now + (15 * 60)  # 15 min expiry
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    payload = {"sub": user_id, "email": email, "type": "reset", "exp": exp, "iat": now}

    header_b64 = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64_encode(signature)}"


def verify_password_reset_token(token: str) -> dict[str, Any] | None:
    """Verify password reset token validity."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "reset":
        return None
    return payload


# ── 2FA TOTP (RFC 6238 TIME-BASED ONE-TIME PASSWORD) ─────────────────────────
def generate_totp_secret() -> str:
    """Generate a random Base32 encoded 160-bit TOTP secret key."""
    raw_bytes = secrets.token_bytes(20)
    return base64.b32encode(raw_bytes).decode("ascii").replace("=", "")


def generate_totp_uri(secret: str, email: str, issuer: str = "TradeThrone") -> str:
    """Generate standard otpauth:// URI for authenticator apps."""
    return f"otpauth://totp/{issuer}:{email}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"


def verify_totp_code(secret: str, code: str, window: int = 1) -> bool:
    """Verify 6-digit TOTP code across a time window (+/- 30s)."""
    if not secret or not code:
        return False

    try:
        clean_secret = secret.strip().upper()
        # Add padding if needed
        padding = 8 - (len(clean_secret) % 8)
        if padding != 8:
            clean_secret += "=" * padding
        key = base64.b32decode(clean_secret, casefold=True)

        current_time_step = int(time.time() // 30)
        for offset in range(-window, window + 1):
            time_step = current_time_step + offset
            time_bytes = time_step.to_bytes(8, byteorder="big")
            hmac_hash = hmac.new(key, time_bytes, hashlib.sha1).digest()
            offset_idx = hmac_hash[-1] & 0x0F
            truncated_hash = (
                (hmac_hash[offset_idx] & 0x7F) << 24
                | (hmac_hash[offset_idx + 1] & 0xFF) << 16
                | (hmac_hash[offset_idx + 2] & 0xFF) << 8
                | (hmac_hash[offset_idx + 3] & 0xFF)
            )
            totp_int = truncated_hash % 1_000_000
            expected_code = f"{totp_int:06d}"
            if hmac.compare_digest(expected_code, code.strip()):
                return True
        return False
    except Exception:
        return False


# ── IN-MEMORY SLIDING WINDOW RATE LIMITER ─────────────────────────────────────
class SlidingWindowRateLimiter:
    """Sliding-window in-memory rate limiter to stop brute force and spam."""

    def __init__(self) -> None:
        self._history: dict[str, list[float]] = {}

    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.time()
        cutoff = now - window_seconds
        records = self._history.setdefault(key, [])
        # Prune old records
        self._history[key] = [t for t in records if t > cutoff]

        if len(self._history[key]) >= max_requests:
            return False

        self._history[key].append(now)
        return True


rate_limiter = SlidingWindowRateLimiter()


def check_rate_limit(key: str, max_requests: int = 5, window_seconds: int = 60) -> bool:
    """Returns True if request is within rate limit, False if exceeded."""
    return rate_limiter.is_allowed(key, max_requests, window_seconds)


# ── OTP GENERATION & VERIFICATION (6-DIGIT TOTP / SHORT CODE) ────────────────
_IN_MEMORY_OTP_STORE: dict[str, dict[str, Any]] = {}


def generate_otp_for_identifier(identifier: str) -> str:
    """Generate a 6-digit numeric OTP valid for 15 minutes."""
    otp_code = f"{secrets.randbelow(900000) + 100000}"
    _IN_MEMORY_OTP_STORE[identifier.lower()] = {
        "code": otp_code,
        "expires_at": time.time() + 900,  # 15 min validity
    }
    return otp_code


def verify_otp_for_identifier(identifier: str, otp_code: str) -> bool:
    """Verify the 6-digit OTP code against the stored value."""
    key = identifier.lower()
    record = _IN_MEMORY_OTP_STORE.get(key)
    if not record:
        return False

    if time.time() > record["expires_at"]:
        _IN_MEMORY_OTP_STORE.pop(key, None)
        return False

    if record["code"] == otp_code:
        _IN_MEMORY_OTP_STORE.pop(key, None)
        return True

    return False

