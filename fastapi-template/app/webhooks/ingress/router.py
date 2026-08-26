"""Webhook ingress router - HTTP endpoints for receiving webhooks."""

from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, Request, Depends, HTTPException, status
from fastapi.responses import JSONResponse
import time
from sqlalchemy.ext.asyncio import AsyncSession

from app.webhooks.validation.middleware import validate_webhook_request
from app.webhooks.validation.schemas import TradeThronePayload
from app.webhooks.routing.router import enrich_envelope
from app.webhooks.queue.redis_streams import webhook_queue
from app.webhooks.resiliency.rate_limiter import rate_limiter
from app.webhooks.resiliency.idempotency import idempotency_store, IdempotencyConflictError
from app.webhooks.observability.metrics import (
    record_webhook_received,
    observe_queue_latency,
)
# Importing the handler registers the TradeThrone signal worker pool
from app.webhooks.handlers.tradethrone_signal import handle_tradethrone_signal  # noqa: F401
from app.db.session import get_db, engine, Base
from app.models.audit import TradeAuditRecord
from app.brokers.angelone import place_tradethrone_order as place_tradethrone_order_angelone
from app.core.logging import get_logger
from app.config import settings

logger = get_logger("webhook.ingress")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Canonical provider identifier for the TradeThrone signal engine
TRADETHRONE_PROVIDER = "tradethrone"
# Legacy wire alias retained for backward compatibility with older integrations
TRADETRON_PROVIDER = "tradetron"
TRADETHRONE_PROVIDERS = {TRADETHRONE_PROVIDER, TRADETRON_PROVIDER}


def _rate_limit_headers(rate_meta: dict) -> dict[str, str]:
    """Build standard rate limit headers."""
    return {
        "X-RateLimit-Limit": str(rate_meta["limit"]),
        "X-RateLimit-Remaining": str(rate_meta["remaining"]),
    }


async def _handle_local_mode(
    provider: str,
    envelope,
    db,
) -> JSONResponse:
    """Handle webhook in local testing mode (bypasses queue, rate limiting, idempotency).

    Validates TradeThrone signals, places the order via Angel One and writes
    the trade-audit record before responding synchronously.
    """
    logger.info("Local mode: accepting webhook for provider=%s", provider)

    validated_data = envelope.payload
    execution_result = None

    if provider.lower() in TRADETHRONE_PROVIDERS:
        try:
            tradethrone_payload = TradeThronePayload(**envelope.payload)
            validated_data = tradethrone_payload.model_dump()
        except Exception as e:
            logger.warning("TradeThrone payload validation failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Schema validation failed: {e}"
            )

        # Execute order placement for validated TradeThrone signals (Angel One)
        execution_result = place_tradethrone_order_angelone(validated_data)

        # Ensure tables exist on the current engine instance before audit logging
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Create TradeAuditRecord with explicit database session
        try:
            audit_record = TradeAuditRecord(
                timestamp=datetime.now(timezone.utc),
                provider=TRADETHRONE_PROVIDER,
                symbol=validated_data.get("symbol", ""),
                action=validated_data.get("action", ""),
                quantity=int(validated_data.get("quantity", 0)),
                status=execution_result.get("status", "UNKNOWN"),
                order_id=execution_result.get("order_id", ""),
                signal=validated_data.get("signal"),
                price=validated_data.get("price"),
            )
            db.add(audit_record)
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"Audit DB save error: {e}")
            # Continue execution gracefully - don't fail the webhook response

    response_content = {
        "status": "validated",
        "provider": provider,
        "data": validated_data,
    }

    if provider.lower() in TRADETHRONE_PROVIDERS and execution_result:
        response_content["execution"] = execution_result

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=response_content,
    )


async def _handle_duplicate(envelope, provider: str, rate_meta: dict) -> JSONResponse:
    """Handle duplicate webhook (already processed)."""
    logger.info(
        "Duplicate webhook detected, returning cached result: event_id=%s",
        envelope.event_id
    )
    record_webhook_received(provider, envelope.event_type, "duplicate")
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "event_id": envelope.event_id,
            "duplicate": True,
        },
        headers=_rate_limit_headers(rate_meta),
    )


async def _handle_processing_conflict(envelope, provider: str, rate_meta: dict) -> JSONResponse:
    """Handle webhook currently being processed by another worker."""
    logger.warning(
        "Webhook currently being processed: event_id=%s",
        envelope.event_id
    )
    record_webhook_received(provider, envelope.event_type, "processing")
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "event_id": envelope.event_id,
            "processing": True,
        },
        headers=_rate_limit_headers(rate_meta),
    )


async def _handle_rate_limited(provider: str, client_ip: str, rate_meta: dict) -> None:
    """Handle rate limit exceeded - logs and raises HTTPException."""
    logger.warning(
        "Rate limit exceeded for provider=%s ip=%s",
        provider, client_ip
    )
    record_webhook_received(provider, "unknown", "rate_limited")
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Rate limit exceeded",
        headers={
            "X-RateLimit-Limit": str(rate_meta["limit"]),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset-After": str(rate_meta["reset_after"]),
        },
    )


async def _handle_enqueue_failure(envelope, provider: str, idempotency_key: str, error: Exception) -> None:
    """Handle enqueue failure - marks idempotency as failed and raises HTTPException."""
    logger.error("Failed to enqueue webhook %s: %s", envelope.event_id, error)
    await idempotency_store.mark_failed(idempotency_key, str(error))
    record_webhook_received(provider, envelope.event_type, "enqueue_failed")
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Failed to queue webhook for processing",
    )


@router.post(
    "/{provider}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive webhook from provider",
    description="Accept webhook events from external providers (Zerodha, Upstox, Razorpay, etc.)",
    responses={
        202: {"description": "Accepted for processing"},
        400: {"description": "Invalid payload"},
        401: {"description": "Invalid signature"},
        422: {"description": "Schema validation failed"},
        429: {"description": "Rate limited"},
        503: {"description": "Service unavailable"},
    },
)
async def receive_webhook(
    provider: str,
    request: Request,
    envelope=Depends(validate_webhook_request),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Main webhook ingestion endpoint.
    
    Flow:
    1. Rate limiting (per provider + IP)
    2. Signature verification (handled by validate_webhook_request dependency)
    3. Schema validation (handled by validate_webhook_request dependency)
    4. Idempotency check
    5. Enrich with routing metadata
    6. Enqueue to Redis Streams
    7. Return 202 Accepted
    """
    # Local testing mode: fast path - accept without Redis/rate-limiting/idempotency
    if settings.webhook_local_mode:
        logger.info("Local mode: accepting webhook for provider=%s", provider)
        
        # Validate payload against the TradeThrone signal schema (the legacy
        # provider alias "tradetron" is still accepted on the wire).
        validated_data = envelope.payload
        if provider.lower() in TRADETHRONE_PROVIDERS:
            try:
                tradethrone_payload = TradeThronePayload(**envelope.payload)
                validated_data = tradethrone_payload.model_dump()
            except Exception as e:
                logger.warning("TradeThrone payload validation failed: %s", e)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Schema validation failed: {e}"
                )
            
            # Execute order placement for validated TradeThrone signals (Angel One)
            execution_result = place_tradethrone_order_angelone(validated_data)
            
            # Ensure tables exist on the current engine instance before audit logging
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            
            # Create TradeAuditRecord with explicit database session
            try:
                audit_record = TradeAuditRecord(
                    timestamp=datetime.now(timezone.utc),
                    provider="tradethrone",
                    symbol=validated_data.get("symbol", ""),
                    action=validated_data.get("action", ""),
                    quantity=int(validated_data.get("quantity", 0)),
                    status=execution_result.get("status", "UNKNOWN"),
                    order_id=execution_result.get("order_id", ""),
                    signal=validated_data.get("signal"),
                    price=validated_data.get("price"),
                )
                db.add(audit_record)
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"Audit DB save error: {e}")
                # Continue execution gracefully - don't fail the webhook response
        
        response_content = {
            "status": "validated",
            "provider": provider,
            "data": validated_data,
        }
        
        # Add execution result for TradeThrone provider
        if provider.lower() in TRADETHRONE_PROVIDERS:
            response_content["execution"] = execution_result
        
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=response_content,
        )

    start_time = time.perf_counter()
    client_ip = request.client.host if request.client else "unknown"
    
    # 1. Rate limiting
    rate_limit_key = f"provider:{provider}"
    allowed, rate_meta = await rate_limiter.check_limit(rate_limit_key)
    if not allowed:
        logger.warning(
            "Rate limit exceeded for provider=%s ip=%s",
            provider, client_ip
        )
        record_webhook_received(provider, "unknown", "rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={
                "X-RateLimit-Limit": str(rate_meta["limit"]),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset-After": str(rate_meta["reset_after"]),
            },
        )
    
    # 2. Idempotency check
    idempotency_key = envelope.idempotency_key
    try:
        is_new, existing_result = await idempotency_store.check_and_mark_processing(idempotency_key)
        if not is_new:
            # Already processed, return cached result
            logger.info(
                "Duplicate webhook detected, returning cached result: event_id=%s",
                envelope.event_id
            )
            record_webhook_received(provider, envelope.event_type, "duplicate")
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "accepted",
                    "event_id": envelope.event_id,
                    "duplicate": True,
                },
                headers={
                    "X-RateLimit-Limit": str(rate_meta["limit"]),
                    "X-RateLimit-Remaining": str(rate_meta["remaining"]),
                },
            )
    except IdempotencyConflictError:
        # Currently being processed by another worker
        logger.warning(
            "Webhook currently being processed: event_id=%s",
            envelope.event_id
        )
        record_webhook_received(provider, envelope.event_type, "processing")
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "accepted",
                "event_id": envelope.event_id,
                "processing": True,
            },
            headers={
                "X-RateLimit-Limit": str(rate_meta["limit"]),
                "X-RateLimit-Remaining": str(rate_meta["remaining"]),
            },
        )
    
    # 3. Enrich envelope with routing metadata
    enriched_envelope = await enrich_envelope(envelope)
    
    # 4. Enqueue to Redis Streams
    try:
        queue_start = time.perf_counter()
        entry_id = await webhook_queue.enqueue(enriched_envelope)
        queue_latency = time.perf_counter() - queue_start
        observe_queue_latency(enriched_envelope.payload["_routing"]["queue"], queue_latency)
    except Exception as e:
        logger.error("Failed to enqueue webhook %s: %s", envelope.event_id, e)
        # Mark idempotency as failed so it can be retried
        await idempotency_store.mark_failed(idempotency_key, str(e))
        record_webhook_received(provider, envelope.event_type, "enqueue_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to queue webhook for processing",
        )
    
    # 5. Record success metrics
    duration_ms = (time.perf_counter() - start_time) * 1000
    record_webhook_received(provider, envelope.event_type, "accepted")
    
    logger.info(
        "Webhook accepted: provider=%s event_type=%s event_id=%s duration_ms=%.2f queue_entry=%s",
        provider, envelope.event_type, envelope.event_id, duration_ms, entry_id
    )
    
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "event_id": envelope.event_id,
        },
        headers={
            "X-RateLimit-Limit": str(rate_meta["limit"]),
            "X-RateLimit-Remaining": str(rate_meta["remaining"]),
        },
    )


@router.get(
    "/{provider}/health",
    summary="Webhook endpoint health check",
    description="Check if webhook endpoint for a provider is healthy",
)
async def webhook_health(provider: str) -> dict:
    """Health check for specific provider webhook endpoint."""
    return {
        "status": "healthy",
        "provider": provider,
        "endpoint": f"/webhooks/{provider}",
    }
