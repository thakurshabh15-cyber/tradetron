"""Cryptographic utilities for encrypting and decrypting sensitive broker credentials at rest."""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

from app.config import settings

# Derive a 256-bit symmetric encryption key from server secret
_SECRET = settings.jwt_secret or "UNCONFIGURED-CHANGE-ME"
if _SECRET == "UNCONFIGURED-CHANGE-ME" or not _SECRET:
    import warnings
    warnings.warn("JWT_SECRET not configured — using insecure fallback for encryption. Set JWT_SECRET in .env!", stacklevel=2)
_KEY = hashlib.sha256(_SECRET.encode("utf-8")).digest()


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext secret using XOR + HMAC-SHA256 block stream cipher (Fernet-style).
    
    Produces a Base64-encoded encrypted token with a unique 16-byte random salt.
    """
    if not plaintext:
        return ""
    
    salt = os.urandom(16)
    # Derive unique stream key for this salt
    stream_key = hashlib.sha256(_KEY + salt).digest()
    
    raw_data = plaintext.encode("utf-8")
    cipher_bytes = bytearray(len(raw_data))
    for i in range(len(raw_data)):
        cipher_bytes[i] = raw_data[i] ^ stream_key[i % len(stream_key)]
    
    # Payload = salt (16 bytes) + ciphertext
    payload = salt + bytes(cipher_bytes)
    # Checksum
    mac = hashlib.sha256(_KEY + payload).digest()[:16]
    return base64.urlsafe_b64encode(payload + mac).decode("ascii")


def decrypt_secret(ciphertext_b64: str) -> str:
    """Decrypt a Base64-encoded ciphertext string back to plaintext."""
    if not ciphertext_b64:
        return ""
    
    try:
        data = base64.urlsafe_b64decode(ciphertext_b64.encode("ascii"))
        if len(data) < 32:  # 16 bytes salt + 16 bytes MAC
            return ""
        
        salt = data[:16]
        mac = data[-16:]
        cipher_bytes = data[16:-16]
        payload = salt + cipher_bytes
        
        # Verify MAC integrity
        expected_mac = hashlib.sha256(_KEY + payload).digest()[:16]
        if not hmac_compare(expected_mac, mac):
            return ""
        
        # Decrypt
        stream_key = hashlib.sha256(_KEY + salt).digest()
        plain_bytes = bytearray(len(cipher_bytes))
        for i in range(len(cipher_bytes)):
            plain_bytes[i] = cipher_bytes[i] ^ stream_key[i % len(stream_key)]
        
        return plain_bytes.decode("utf-8")
    except Exception:
        return ""


def hmac_compare(a: bytes, b: bytes) -> bool:
    """Constant-time comparison."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= x ^ y
    return result == 0


def mask_secret(value: str) -> str:
    """Mask a secret key for display (e.g. 'apikey_1234567890' -> 'apikey_****_7890')."""
    if not value or len(value) <= 6:
        return "******"
    return f"{value[:3]}****{value[-4:]}"
