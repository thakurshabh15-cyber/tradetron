"""Webhook ingress layer - HTTP endpoints."""

from app.webhooks.ingress.router import router

__all__ = ["router"]