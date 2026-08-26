"""Webhook signature verification for various providers."""

from __future__ import annotations

import hmac
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

from app.core.logging import get_logger

logger = get_logger("webhook.validation.signatures")


@dataclass(frozen=True)
class VerificationResult:
    """Result of signature verification."""
    valid: bool
    error: str | None = None
    provider: str | None = None


class SignatureVerifier(Protocol):
    """Protocol for webhook signature verification."""
    
    def verify(self, payload: bytes, headers: dict[str, str]) -> VerificationResult:
        ...


class HMACVerifier:
    """Generic HMAC-SHA256 verifier (Razorpay, Stripe, etc.)"""
    
    def __init__(
        self, 
        secret: str, 
        header_name: str = "X-Signature", 
        algorithm: str = "sha256"
    ):
        self.secret = secret.encode()
        self.header_name = header_name.lower()
        self.algorithm = algorithm
    
    def verify(self, payload: bytes, headers: dict[str, str]) -> VerificationResult:
        # Case-insensitive header lookup
        signature = None
        for k, v in headers.items():
            if k.lower() == self.header_name:
                signature = v
                break
        
        if not signature:
            return VerificationResult(
                valid=False, 
                error=f"Missing {self.header_name} header"
            )
        
        # Handle different signature formats
        if signature.startswith("sha256="):
            signature = signature[7:]
        elif signature.startswith("v1,"):
            # Stripe format: v1=<signature>
            signature = signature[3:]
        
        expected = hmac.new(
            self.secret, 
            payload, 
            getattr(hashlib, self.algorithm)
        ).hexdigest()
        
        if not hmac.compare_digest(signature, expected):
            logger.warning("Signature verification failed for %s", self.header_name)
            return VerificationResult(valid=False, error="Invalid signature")
        
        return VerificationResult(valid=True, provider=self.header_name)


class ZerodhaPostbackVerifier:
    """Zerodha postback uses checksum in payload, not header"""
    
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
    
    def verify(self, payload: bytes, headers: dict[str, str]) -> VerificationResult:
        try:
            data = json.loads(payload)
            checksum = data.get("checksum")
            if not checksum:
                return VerificationResult(
                    valid=False, 
                    error="Missing checksum in payload"
                )
            
            # Reconstruct checksum: sha256(api_key + payload_without_checksum + api_secret)
            payload_without_checksum = {
                k: v for k, v in data.items() if k != "checksum"
            }
            reconstructed = hashlib.sha256(
                f"{self.api_key}{json.dumps(payload_without_checksum, separators=(',', ':'))}{self.api_secret}".encode()
            ).hexdigest()
            
            if not hmac.compare_digest(checksum, reconstructed):
                return VerificationResult(valid=False, error="Invalid Zerodha checksum")
            
            return VerificationResult(valid=True, provider="zerodha")
        except Exception as e:
            return VerificationResult(valid=False, error=f"Verification error: {e}")


class UpstoxWebhookVerifier:
    """Upstox uses HMAC-SHA256 with X-Upstox-Signature header"""
    
    def __init__(self, webhook_secret: str):
        self.secret = webhook_secret.encode()
    
    def verify(self, payload: bytes, headers: dict[str, str]) -> VerificationResult:
        # Case-insensitive header lookup
        signature = None
        for k, v in headers.items():
            if k.lower() == "x-upstox-signature":
                signature = v
                break
        
        if not signature:
            return VerificationResult(
                valid=False, 
                error="Missing X-Upstox-Signature"
            )
        
        expected = hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.lower(), expected):
            return VerificationResult(valid=False, error="Invalid Upstox signature")
        
        return VerificationResult(valid=True, provider="upstox")


class AngelOneWebhookVerifier:
    """Angel One uses HMAC-SHA256 with X-AngelOne-Signature header"""
    
    def __init__(self, webhook_secret: str):
        self.secret = webhook_secret.encode()
    
    def verify(self, payload: bytes, headers: dict[str, str]) -> VerificationResult:
        signature = headers.get("x-angelone-signature", "").lower()
        if not signature:
            return VerificationResult(
                valid=False, 
                error="Missing X-AngelOne-Signature"
            )
        
        expected = hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return VerificationResult(valid=False, error="Invalid Angel One signature")
        
        return VerificationResult(valid=True, provider="angel_one")


class BinanceWebhookVerifier:
    """Binance uses HMAC-SHA256 with X-Binance-Signature header"""
    
    def __init__(self, webhook_secret: str):
        self.secret = webhook_secret.encode()
    
    def verify(self, payload: bytes, headers: dict[str, str]) -> VerificationResult:
        signature = headers.get("x-binance-signature", "").lower()
        if not signature:
            return VerificationResult(
                valid=False, 
                error="Missing X-Binance-Signature"
            )
        
        expected = hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return VerificationResult(valid=False, error="Invalid Binance signature")
        
        return VerificationResult(valid=True, provider="binance")


class TradeThroneWebhookVerifier:
    """TradeThrone uses HMAC-SHA256 with X-TradeThrone-Signature header"""
    
    def __init__(self, webhook_secret: str):
        self.secret = webhook_secret.encode()
    
    def verify(self, payload: bytes, headers: dict[str, str]) -> VerificationResult:
        # Support multiple header names for flexibility
        signature = None
        for header_name in ["x-tradethrone-signature", "x-hub-signature", "x-signature"]:
            if header_name in headers:
                signature = headers[header_name].lower()
                break
        
        if not signature:
            return VerificationResult(
                valid=False, 
                error="Missing X-TradeThrone-Signature (or X-Hub-Signature/X-Signature) header"
            )
        
        # Handle "sha256=" prefix if present
        if signature.startswith("sha256="):
            signature = signature[7:]
        
        expected = hmac.new(self.secret, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("TradeThrone signature verification failed")
            return VerificationResult(valid=False, error="Invalid TradeThrone signature")
        
        return VerificationResult(valid=True, provider="tradethrone")


# Registry for dynamic verifier lookup
VERIFIER_REGISTRY: dict[str, SignatureVerifier] = {}


def register_verifier(provider: str, verifier: SignatureVerifier) -> None:
    """Register a signature verifier for a provider."""
    VERIFIER_REGISTRY[provider.lower()] = verifier


def get_verifier(provider: str) -> SignatureVerifier | None:
    """Get verifier for a provider."""
    return VERIFIER_REGISTRY.get(provider.lower())


def init_verifiers(settings) -> None:
    """Initialize default verifiers from settings."""
    if settings.razorpay_webhook_secret:
        register_verifier("razorpay", HMACVerifier(
            settings.razorpay_webhook_secret, 
            header_name="X-Razorpay-Signature"
        ))
    if settings.zerodha_api_key and settings.zerodha_api_secret:
        register_verifier("zerodha", ZerodhaPostbackVerifier(
            settings.zerodha_api_key, settings.zerodha_api_secret
        ))
    if settings.upstox_webhook_secret:
        register_verifier("upstox", UpstoxWebhookVerifier(settings.upstox_webhook_secret))
    if settings.angelone_webhook_secret:
        register_verifier("angel_one", AngelOneWebhookVerifier(settings.angelone_webhook_secret))
    if settings.binance_webhook_secret:
        register_verifier("binance", BinanceWebhookVerifier(settings.binance_webhook_secret))
    if settings.tradethrone_webhook_secret:
        register_verifier("tradethrone", TradeThroneWebhookVerifier(settings.tradethrone_webhook_secret))
