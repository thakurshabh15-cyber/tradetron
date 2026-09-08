"""TradeThrone Webhook Platform - Main entry point."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure root directory is in Python path
ROOT_DIR = Path(__file__).parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.webhooks.ingress import router as ingress_router
from app.webhooks.ingress.audit_router import router as audit_router
from app.webhooks.validation.signatures import init_verifiers
from app.webhooks.resiliency.circuit_breaker import init_circuit_breakers
from app.webhooks.resiliency.bulkhead import init_bulkheads
from app.webhooks.observability import setup_webhook_logging  # setup_tracing TEMPORARILY removed
from app.core.cors import build_cors_config
from app.core.logging import setup_logging, get_logger
from app.config import settings
from app.db.session import init_db

# Import handlers to register them
from app.webhooks.handlers import broker_postback, billing, tradethrone_signal  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: logging/tracing, database, resiliency, and the Redis-backed
    # webhook pipeline (queue + rate limiter + idempotency + worker pool).
    setup_logging()
    setup_webhook_logging()
    # TEMPORARY: OpenTelemetry tracing disabled - FastAPIInstrumentor middleware
    # was causing infinite-loading/hanging requests on all endpoints.
    # setup_tracing(app)

    logger = get_logger("webhook.main")

    # Initialize database tables
    await init_db()

    # Initialize resiliency components (synchronous, no await)
    init_verifiers(settings)
    init_circuit_breakers()
    init_bulkheads()

    # Initialize the Redis-backed webhook pipeline.  In webhook_local_mode the
    # ingress path bypasses Redis entirely (signature verification and the
    # queue are skipped), so there is nothing to connect here.
    if not settings.webhook_local_mode:
        from app.webhooks.queue.redis_streams import webhook_queue
        from app.webhooks.resiliency.rate_limiter import rate_limiter
        from app.webhooks.resiliency.idempotency import idempotency_store
        from app.webhooks.workers.pool import worker_pool

        await webhook_queue.initialize()
        await rate_limiter.initialize()
        await idempotency_store.initialize()
        await worker_pool.start()
        logger.info("Webhook pipeline initialized (queue + rate limiter + idempotency + workers)")
    else:
        logger.info("Webhook local mode active — pipeline not started (Redis bypassed)")

    logger.info("TradeThrone Webhook platform started (lifespan yield only)")

    yield

    # Shutdown - minimal, no await on external services
    logger.info("Shutting down TradeThrone webhook platform...")
    if not settings.webhook_local_mode:
        from app.webhooks.workers.pool import worker_pool
        from app.webhooks.queue.redis_streams import webhook_queue

        try:
            await worker_pool.stop()
        except Exception:
            pass
        try:
            await webhook_queue.shutdown()
        except Exception:
            pass



app = FastAPI(
    title="TradeThrone Webhook Platform",
    version="1.0.0",
    # Interactive docs are a development aid; hide the full endpoint/schema
    # inventory when running under ENVIRONMENT=production.
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url="/redoc" if settings.environment != "production" else None,
    openapi_url="/openapi.json" if settings.environment != "production" else None,
)

# CORS follows the same hardened posture as the main API: production uses exact
# origins only (never a generic `*.vercel.app` regex, never a wildcard combined
# with credentials).  The previous `settings.allowed_origins.split(",")` plus
# `allow_credentials=True` silently became `["*"]` if ALLOWED_ORIGINS was ever
# set to `*` in production.  Like the main API, the webhook platform is also
# fronted by a proxy that must keep the exact-origin lock consistent.
_cors = build_cors_config(
    environment=settings.environment,
    allowed_origins=settings.allowed_origins,
    frontend_url=settings.frontend_url,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors.origins,
    allow_origin_regex=_cors.origin_regex,
    allow_credentials=True,
    allow_methods=["POST", "GET", "HEAD"],
    allow_headers=["*"],
)

# Mount webhook routers
app.include_router(ingress_router)
app.include_router(audit_router)


# Health check (no auth, no rate limit) - returns instantly without external calls
@app.get("/healthz")
async def health_check():
    return {"status": "healthy", "service": "webhook-platform"}


# Readiness check - verifies external dependencies (Redis, etc.)
@app.get("/readyz")
async def readiness_check():
    from app.webhooks.queue.redis_streams import webhook_queue
    queue_healthy = await webhook_queue.health_check()
    if queue_healthy:
        return {"status": "ready"}
    else:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Queue unhealthy")


# Metrics endpoint
@app.get("/metrics")
async def metrics():
    from prometheus_client import generate_latest
    from app.webhooks.observability.metrics import WEBHOOK_REGISTRY
    return Response(content=generate_latest(WEBHOOK_REGISTRY), media_type="text/plain")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.webhooks.main:app",
        host="127.0.0.1",
        port=settings.webhook_http_port,
        reload=settings.environment == "development",
        log_level=settings.log_level.lower(),
    )