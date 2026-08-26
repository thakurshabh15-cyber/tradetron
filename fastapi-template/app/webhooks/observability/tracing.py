"""OpenTelemetry distributed tracing setup."""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

from app.config import settings
from app.core.logging import get_logger

logger = get_logger("webhook.tracing")


def setup_tracing(app: FastAPI | None = None) -> None:
    """Initialize OpenTelemetry tracing"""
    resource = Resource.create({
        SERVICE_NAME: "tradetron-webhooks",
        "environment": settings.environment,
    })
    
    provider = TracerProvider(resource=resource)
    
    # OTLP exporter (Jaeger, Tempo, Datadog, etc.)
    if settings.otlp_endpoint:
        otlp_exporter = OTLPSpanExporter(
            endpoint=settings.otlp_endpoint,
            insecure=settings.otlp_insecure,
        )
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    
    trace.set_tracer_provider(provider)
    
    # Auto-instrumentation - instantiate instrumentors before calling instrument()
    RedisInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument()
    
    # FastAPI instrumentation requires the app instance
    if app is not None:
        FastAPIInstrumentor().instrument_app(app)
    
    logger.info("Distributed tracing initialized")


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)