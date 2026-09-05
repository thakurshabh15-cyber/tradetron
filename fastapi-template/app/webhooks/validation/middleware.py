"""Webhook validation middleware - ties together signature verification and schema validation."""

from __future__ import annotations

from fastapi import Request, HTTPException
import time
import uuid
import json

from app.webhooks.validation.signatures import get_verifier, VerificationResult
from app.webhooks.validation.schemas import validate_webhook_payload, WebhookEnvelope
from app.webhooks.resiliency.idempotency import idempotency_store
from app.core.logging import get_logger
from app.config import settings

logger = get_logger("webhook.validation.middleware")


async def validate_webhook_request(
    request: Request,
    provider: str,
) -> WebhookEnvelope:
    """
    Comprehensive webhook validation:
    1. Rate limiting (per provider + IP) - handled at ingress layer
    2. Signature verification
    3. Schema validation
    4. Idempotency key extraction/generation
    """
    start_time = time.perf_counter()
    client_ip = request.client.host if request.client else "unknown"

    # 1. Read raw body (needed for signature verification)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    # 2. Parse JSON
    try:
        json_body = json.loads(body)
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON from %s: %s", client_ip, e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 3. Extract standard fields
    event_id = json_body.get("event_id") or json_body.get("id") or str(uuid.uuid4())
    event_type = json_body.get("event") or json_body.get("event_type") or "unknown"
    timestamp_str = json_body.get("timestamp") or json_body.get("created_at")

    try:
        from datetime import datetime
        timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")) if timestamp_str else datetime.utcnow()
    except Exception:
        timestamp = datetime.utcnow()

    # 4. Signature verification (skipped in local testing mode)
    logger.debug("webhook_local_mode=%s, provider=%s", settings.webhook_local_mode, provider)
    if not settings.webhook_local_mode:
        verifier = get_verifier(provider)
        if verifier:
            try:
                headers = {k.lower(): v for k, v in request.headers.items()}
                result: VerificationResult = verifier.verify(body, headers)
                if not result.valid:
                    logger.warning(
                        "Signature verification failed for %s from %s: %s",
                        provider, client_ip, result.error
                    )
                    raise HTTPException(status_code=401, detail=result.error or "Invalid signature")
            except HTTPException:
                raise
            except Exception as exc:
                logger.error(
                    "Signature verification error for %s from %s: %s",
                    provider, client_ip, exc
                )
                raise HTTPException(status_code=401, detail="Invalid or missing signature")
        else:
            # No verifier registered for this provider in non-local mode.
            #
            # SECURITY HARDENING (Phase 3-E): we must REJECT, not pass through.
            # The `("*", "*")` fallback route resolves to the `custom_normal`
            # worker pool, whose handler is `handle_tradethrone_signal` — which
            # places REAL orders. Allowing an unregistered provider slug through
            # with no signature check would let an unauthenticated attacker POST
            # a validly-shaped TradeThrone signal and trigger a genuine trade.
            #
            # Every legitimately-integrated provider registers a verifier via
            # `init_verifiers` (razorpay, zerodha, upstox, angel_one, binance,
            # tradethrone). A provider with no verifier cannot have its
            # authenticity established and must therefore be refused rather than
            # forwarded to a trade-executing handler.
            logger.warning(
                "Rejecting webhook for provider %s in non-local mode: "
                "no signature verifier configured (unauthenticated provider would "
                "route to a trade-executing fallback handler)",
                provider
            )
            raise HTTPException(
                status_code=401,
                detail=(
                    f"No signature verifier configured for provider "
                    f"'{provider}'; unauthenticated webhooks are rejected."
                ),
            )
    else:
        logger.debug("Skipping signature verification for %s (local mode)", provider)

    # 4b. Timestamp freshness / replay-window enforcement (skipped in local mode).
    #
    # SECURITY HARDENING (Phase 3-E): The idempotency store only deduplicates an
    # event for its retention window (default 7 days). Once that key expires, a
    # captured valid webhook can be replayed with the same event_id and it will
    # be treated as NEW again, re-running its financial/trading side effect
    # (duplicate TradeThrone order, re-applied Razorpay payment, ...).
    #
    # The signature verification above authenticates the raw body, which
    # includes the timestamp, so a verified timestamp cannot be forged by an
    # attacker. We therefore reject any event whose authenticated timestamp
    # precedes the idempotency retention window: such an event can never be
    # deduplicated and must be a replay or a pathological delivery. This binds
    # the freshness boundary to the ALREADY-defined idempotency TTL constant
    # (no new product policy is chosen).
    #
    # Providers that do not send a timestamp are unaffected: the extraction
    # fallback above uses 'now', which is always fresh.
    if not settings.webhook_local_mode:
        try:
            from datetime import timezone as _tz, timedelta as _td
            # Normalize the authenticated timestamp to timezone-aware UTC so
            # naive timestamps (e.g. datetime.utcnow() fallback, or providers
            # that omit the timezone offset) compare consistently with 'now'.
            _ts = timestamp
            if _ts.tzinfo is None:
                _ts = _ts.replace(tzinfo=_tz.utc)
            replay_horizon = idempotency_store.ttl_seconds
            max_age = _td(seconds=replay_horizon)
            if (datetime.now(_tz.utc) - _ts) > max_age:
                logger.warning(
                    "Rejecting stale webhook for provider=%s event_id=%s: "
                    "timestamp %s older than idempotency/replay window (%ds); "
                    "likely a replay after key expiry",
                    provider, event_id, timestamp, replay_horizon,
                )
                raise HTTPException(
                    status_code=401,
                    detail=(
                        f"Webhook timestamp is older than the replay-protection "
                        f"window ({replay_horizon}s) and cannot be deduplicated; "
                        f"replayed or stale webhooks are rejected."
                    ),
                )
        except HTTPException:
            raise
        except Exception as exc:  # Fail safe: do not loosen the replay guard on error
            logger.error(
                "Freshness check error for %s: %s", provider, exc
            )
            raise HTTPException(
                status_code=401,
                detail="Stale-webhook rejection check failed; refusing request.",
            )

    # 5. Schema validation
    payload = json_body.get("payload", json_body)
    valid, error = await validate_webhook_payload(provider, event_type, payload)
    if not valid:
        logger.warning(
            "Schema validation failed for %s/%s: %s",
            provider, event_type, error
        )
        raise HTTPException(status_code=422, detail=error)

    # 6. Idempotency key
    idempotency_key = request.headers.get("X-Idempotency-Key") or json_body.get("idempotency_key")
    if not idempotency_key:
        # Generate deterministic key from event_id + provider
        idempotency_key = f"{provider}:{event_id}"

    # 7. Build envelope
    envelope = WebhookEnvelope(
        event_id=event_id,
        event_type=event_type,
        timestamp=timestamp,
        provider=provider.lower(),
        payload=payload,
        idempotency_key=idempotency_key,
    )

    # 8. Record metrics
    duration_ms = (time.perf_counter() - start_time) * 1000
    logger.debug(
        "Webhook validated: provider=%s event=%s duration_ms=%.2f",
        provider, event_type, duration_ms
    )

    # Store envelope in request state for downstream handlers
    request.state.webhook_envelope = envelope

    return envelope