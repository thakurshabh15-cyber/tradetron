"""Structured JSON logging for webhook platform."""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from typing import Any
from pythonjsonlogger.jsonlogger import JsonFormatter  # pyright: ignore[reportPrivateImportUsage]  # pythonjsonlogger re-exports the class at runtime

from app.config import settings


class WebhookJsonFormatter(JsonFormatter):
    """JSON formatter with webhook-specific fields"""
    
    def add_fields(self, log_data: dict, record: logging.LogRecord, message_dict: dict) -> None:
        super().add_fields(log_data, record, message_dict)
        log_data["timestamp"] = datetime.now(timezone.utc).isoformat()
        log_data["service"] = "tradetron-webhooks"
        log_data["environment"] = settings.environment
        
        # Add webhook context if available (custom attrs attached via extra=).
        webhook_event_id = getattr(record, "webhook_event_id", None)
        if webhook_event_id is not None:
            log_data["webhook_event_id"] = webhook_event_id
        webhook_provider = getattr(record, "webhook_provider", None)
        if webhook_provider is not None:
            log_data["webhook_provider"] = webhook_provider
        webhook_event_type = getattr(record, "webhook_event_type", None)
        if webhook_event_type is not None:
            log_data["webhook_event_type"] = webhook_event_type


def setup_webhook_logging() -> None:
    """Configure structured JSON logging for webhook platform"""
    handler = logging.StreamHandler()
    handler.setFormatter(WebhookJsonFormatter(
        "%(timestamp)s %(levelname)s %(name)s %(message)s"
    ))
    
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]
    
    # Reduce noise from libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("redis").setLevel(logging.WARNING)