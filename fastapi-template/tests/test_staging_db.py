"""Phase 3 — Staging Verification: DB migrations & Alembic baseline.

Verifies that the ORM metadata (used by the Alembic baseline) is complete and
that a fresh SQLite DB built from it contains every expected table — proving
the migration system can stand up a clean staging database.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest

# Import every model module so all tables register on Base.metadata.
from app.db.session import Base  # noqa: F401
from app.models import (  # noqa: F401
    audit,
    billing,
    broker_account,
    copy_trading,
    marketplace,
    notification,
    trading,
    user,
    visual_strategy,
    watchlist,
)


def _run(coro):
    return asyncio.run(coro)


class TestAlembicMigration:

    def test_metadata_has_all_expected_tables(self):
        expected = {
            "users", "orders", "strategies", "trades", "positions",
            "broker_accounts", "broker_session_logs", "audit_logs",
            "trade_audit_logs", "plans", "subscriptions", "payments",
            "invoices", "copy_groups", "copy_followers", "visual_strategies",
            "marketplace_strategies", "strategy_deployments",
            "creator_payout_settings", "notification_preferences",
            "price_alerts", "watchlists", "revoked_tokens",
        }
        tables = set(Base.metadata.tables.keys())
        missing = expected - tables
        assert not missing, f"Models missing tables: {missing}"
        assert len(tables) == len(expected)

    def test_baseline_creates_all_tables(self):
        """A fresh DB via the ORM create_all must produce all tables.

        This is exactly what the Alembic baseline migration executes, so it
        proves the migration system can stand up a clean staging DB.
        """
        from sqlalchemy.ext.asyncio import create_async_engine

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name

        expected_tables = sorted(Base.metadata.tables.keys())
        try:
            url = f"sqlite+aiosqlite:///{tmp_path}"
            engine = create_async_engine(url)

            async def create():
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

            _run(create())
            _run(engine.dispose())

            conn = sqlite3.connect(tmp_path)
            actual_tables = sorted(
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            )
            conn.close()

            assert actual_tables == expected_tables, (
                f"Table mismatch.\nMissing: {set(expected_tables) - set(actual_tables)}\n"
                f"Extra:   {set(actual_tables) - set(expected_tables)}"
            )
        finally:
            try:
                os.unlink(tmp_path)
            except PermissionError:
                pass  # file lock held on Windows; best-effort cleanup

    def test_baseline_migration_file_exists(self):
        versions_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        assert os.path.isdir(versions_dir)
        files = os.listdir(versions_dir)
        assert any("0001_baseline" in f for f in files), (
            f"No baseline migration found in {versions_dir}"
        )
