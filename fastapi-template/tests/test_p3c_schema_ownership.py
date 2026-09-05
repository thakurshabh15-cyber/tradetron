"""Phase 3-C regression tests: production schema ownership (Alembic gate).

Verifies ``app/db/session.py`` → ``ensure_tables_local_dev()`` which backs the
webhook-ingress schema bootstrap:

- in production, ORM ``create_all`` is NEVER triggered from the request path —
  the schema is owned exclusively by Alembic and a webhook POST must not be a
  parallel, version-less DDL channel that masks migration drift
- outside production, the idempotent dev bootstrap still works

Also verifies the webhook ingress routers no longer reference the raw
``engine``/``Base.metadata.create_all`` block directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.db import session as db_session


def test_ensure_tables_local_dev_gated_in_production(monkeypatch):
    """create_all must be skipped when environment == production."""
    calls: list[bool] = []

    class _FakeMetadata:
        @staticmethod
        def create_all(bind, *args, **kwargs):
            calls.append(True)

    class _FakeBase:
        metadata = _FakeMetadata()

    monkeypatch.setattr(db_session, "Base", _FakeBase)
    monkeypatch.setattr(db_session, "settings", type("S", (), {"environment": "production"})())

    asyncio.run(db_session.ensure_tables_local_dev())
    assert not calls, "create_all must NOT run under production (Alembic owns schema)"

    monkeypatch.setattr(db_session, "settings", type("S", (), {"environment": "testing"})())
    asyncio.run(db_session.ensure_tables_local_dev())
    assert calls, "dev/test bootstrap must still run create_all"


def test_webhook_ingress_uses_gated_helper():
    """The webhook request paths must call the gated helper, not raw create_all."""
    root = Path(__file__).resolve().parent.parent
    for rel in (
        "app/webhooks/ingress/router.py",
        "app/webhooks/ingress/audit_router.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert "ensure_tables_local_dev" in text, f"{rel} must use the gated helper"
        assert "Base.metadata.create_all" not in text, (
            f"{rel} must not call raw Base.metadata.create_all from the request path"
        )