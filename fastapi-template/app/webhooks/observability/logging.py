"""Structured JSON logging for webhook platform."""

from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from typing import Any
from pythonjsonlogger import jsonlogger

from app.config import settings


class WebhookJsonFormatter(jsonlogger.JsonFormatter):
    """JSON formatter with webhook-specific fields"""
    
    def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = datetime.now(timezone.utc).isoformat()
        log_record["service"] = "tradetron-webhooks"
        log_record["environment"] = settings.environment
        
        # Add webhook context if available
        if hasattr(record, "webhook_event_id"):
            log_record["webhook_event_id"] = record.webhook_event_id
        if hasattr(record, "webhook_provider"):
            log_record["webhook_provider"] = record.webhook_provider
        if hasattr(record, "webhook_event_type"):
            log_record["webhook_event_type"] = record.webhook_event_type


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