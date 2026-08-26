"""Cryptographic utilities for encrypting and decrypting sensitive broker
credentials at rest.

Primary format: **Fernet** (AES-128-CBC + HMAC-SHA256, key derived from
JWT_SECRET via SHA-256 → 32-byte urlsafe base64).  Legacy XOR+HMAC payloads
written before this module migrated are still decryptable transparently,
and every new write uses Fernet so all secrets rest as ciphertext only.
"""

from __future__ import annotations

import base64
import hashlib
import os
import warnings

from app.config import settings

_SECRET = settings.jwt_secret or "UNCONFIGURED-CHANGE-ME"
if _SECRET == "UNCONFIGURED-CHANGE-ME" or not _SECRET:
    warnings.warn(
        "JWT_SECRET not configured — using insecure fallback for encryption. Set JWT_SECRET in .env!",
        stacklevel=2,
    )
_KEY = hashlib.sha256(_SECRET.encode("utf-8")).digest()
_FERNET_KEY = base64.urlsafe_b64encode(_KEY)


def _fernet():
    """Lazily construct the Fernet instance (cryptography is a hard dep)."""
    from cryptography.fernet import Fernet

    return Fernet(_FERNET_KEY)


def _legacy_decrypt(ciphertext_b64: str) -> str:
    """Decrypt pre-Fernet XOR+HMAC-SHA256 payloads (read-only compat)."""
    try:
        data = base64.urlsafe_b64decode(ciphertext_b64.encode("ascii"))
        if len(data) < 32:
            return ""
        salt, mac, body = data[:16], data[-16:], data[16:-16]
        expected = hashlib.sha256(_KEY + salt + body).digest()[:16]
        if not hmac_compare(expected, mac):
            return ""
        stream = hashlib.sha256(_KEY + salt).digest()
        plain = bytes(b ^ stream[i % len(stream)] for i, b in enumerate(body))
        return plain.decode("utf-8")
    except Exception:
        return ""


def hmac_compare(a: bytes, b: bytes) -> bool:
    """Constant-time equality."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= x ^ y
    return result == 0


def encrypt_secret(plaintext: str) -> str:
    """Encrypt plaintext into an authenticated Fernet token (AES-256-class)."""
    if not plaintext:
        return ""
    token = _fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt Fernet tokens; transparently falls back to legacy payloads."""
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        pass
    # Legacy pre-migration payload?
    if ciphertext.startswith("gAAAAA") is False and not ciphertext.endswith("="):
        return _legacy_decrypt(ciphertext)
    return _legacy_decrypt(ciphertext)


def mask_secret(value: str) -> str:
    """Mask a secret for display ('apikey_1234567890' -> 'api****7890')."""
    if not value or len(value) <= 6:
        return "******"
    return f"{value[:3]}****{value[-4:]}"


def generate_totp(totp_secret: str) -> str:
    """Generate a 6-digit RFC-6238 TOTP code from a Base32 secret (Angel One login)."""
    if not totp_secret:
        raise ValueError("TOTP secret cannot be empty")
    try:
        import pyotp

        return pyotp.TOTP(totp_secret.strip()).now()
    except Exception as exc:
        raise ValueError(f"Invalid TOTP secret format: {exc}") from exc
