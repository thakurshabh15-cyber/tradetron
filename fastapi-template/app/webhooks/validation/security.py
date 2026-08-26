"""TradeThrone webhook signature verification utility."""

from __future__ import annotations

import hmac
import hashlib
from typing import Dict


def verify_tradethrone_signature(
    payload_bytes: bytes, 
    signature_header: str, 
    secret: str
) -> bool:
    """
    Verify TradeThrone webhook signature using HMAC-SHA256.
    
    Args:
        payload_bytes: Raw request body bytes
        signature_header: Signature value from header (X-TradeThrone-Signature, X-Hub-Signature, or X-Signature)
        secret: Webhook secret from settings
    
    Returns:
        True if signature is valid, False otherwise
    """
    if not secret:
        return False
    
    if not signature_header:
        return False
    
    # Handle "sha256=" prefix if present
    signature = signature_header.lower()
    if signature.startswith("sha256="):
        signature = signature[7:]
    
    expected = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, expected)


def extract_signature_header(headers: Dict[str, str]) -> str | None:
    """
    Extract signature from supported header names.
    
    Args:
        headers: Request headers dict
    
    Returns:
        Signature value or None if not found
    """
    for header_name in ["x-tradethrone-signature", "x-hub-signature", "x-signature"]:
        if header_name in headers:
            return headers[header_name]
    return None