"""Regression tests: migrate-before-serve startup gate (Render Free).

Verifies ``app.db.migrations.run_migrations`` — the production startup
migration runner that executes ``alembic upgrade head`` before any request
is served.

Coverage:
  A. Legacy schema (no idempotency columns, no alembic_version) gets upgraded.
  B. Already-current schema is harmless / idempotent (second run is a no-op).
  C. Migration failure causes startup failure (fail-closed, init_db never reached).
  D. Production startup never falls back to create_all (no side-effect tables).
  E. Migration precedes any request-serving path (source order + behavioral).

SAFETY: Every test uses a throwaway SQLite database.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app.db.migrations import MigrationError, run_migrations

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_alembic_cmd(db_url: str, *args: str):
    """Run an alembic CLI command against *db_url* via subprocess."""
    import subprocess
    import sys

    env = {**os.environ}
    env["DATABASE_URL"] = db_url
    env["BROKER_MODE"] = "simulated"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _create_legacy_orders_db(db_path: Path) -> None:
    """Minimal 'orders' table: NO client_order_id, NO position_id."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE orders ("
            "  id VARCHAR(36) PRIMARY KEY,"
            "  user_id VARCHAR(36), strategy_id VARCHAR(36),"
            "  broker_account_id VARCHAR(36), broker_order_id VARCHAR(64),"
            "  symbol VARCHAR(20), side VARCHAR(10), quantity INTEGER,"
            "  price FLOAT, status VARCHAR(20),"
            "  mode VARCHAR(20) DEFAULT 'PAPER', created_at TIMESTAMP"
            ")"
        )
        conn.execute("CREATE INDEX ix_orders_user_id ON orders (user_id)")
        conn.commit()
    finally:
        conn.close()


def _col_names(db_path: Path, table: str) -> set[str]:
    c = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info('{table}')")}
    finally:
        c.close()


def _tables(db_path: Path) -> set[str]:
    c = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )}
    finally:
        c.close()


def _version(db_path: Path) -> str | None:
    c = sqlite3.connect(str(db_path))
    try:
        r = c.execute("SELECT version_num FROM alembic_version").fetchone()
        return r[0] if r else None
    finally:
        c.close()


def _indexes(db_path: Path, table: str) -> set[str]:
    c = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in c.execute(f"PRAGMA index_list('{table}')")}
    finally:
        c.close()


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _set_db_env(db_path: Path) -> dict[str, str]:
    """Return env dict with DATABASE_URL pointing at *db_path*."""
    env = os.environ.copy()
    env["DATABASE_URL"] = _db_url(db_path)
    env["BROKER_MODE"] = "simulated"
    return env


def _patch_env(env: dict[str, str]):
    """Context-like helper: patch os.environ then restore."""
    original = dict(os.environ)
    os.environ.update(env)
    return original


def _restore_env(original: dict[str, str]):
    os.environ.clear()
    os.environ.update(original)


# ---------------------------------------------------------------------------
# A. Legacy schema gets upgraded
# ---------------------------------------------------------------------------

def test_legacy_schema_upgraded_by_run_migrations(tmp_path):
    """RED→GREEN: orders WITHOUT client_order_id / position_id, no
    alembic_version → run_migrations() repairs the schema.
    """
    db = tmp_path / "legacy.db"
    _create_legacy_orders_db(db)

    # Stamp at 0002 so upgrade head runs ONLY migration 0003
    stamp = _run_alembic_cmd(_db_url(db), "stamp", "0002_drop_mobile_otps")
    assert stamp.returncode == 0, stamp.stderr

    # Pre-state: columns absent, version = 0002
    assert "client_order_id" not in _col_names(db, "orders")
    assert "position_id" not in _col_names(db, "orders")
    assert _version(db) == "0002_drop_mobile_otps"

    # Invoke the production startup runner
    original = _patch_env(_set_db_env(db))
    try:
        run_migrations()
    finally:
        _restore_env(original)

    # Post-state: columns present, indexes present, version = 0006
    assert "client_order_id" in _col_names(db, "orders")
    assert "position_id" in _col_names(db, "orders")
    assert "signal_key" in _col_names(db, "orders")
    assert "ux_orders_user_client_order_id" in _indexes(db, "orders")
    assert "ux_orders_signal_key" in _indexes(db, "orders")
    assert _version(db) == "0006_user_setup_tasks"

    # Behavioral D: create_all did NOT run — only migration-defined tables
    # (0006 adds user_setup_tasks; 0003-0005 only alter existing tables).
    tables = _tables(db)
    assert tables == {"orders", "user_setup_tasks", "alembic_version"}, (
        f"Unexpected tables: {tables - {'orders', 'user_setup_tasks', 'alembic_version'}}"
    )


# ---------------------------------------------------------------------------
# B. Already-current schema is idempotent
# ---------------------------------------------------------------------------

def test_second_upgrade_head_is_idempotent(tmp_path):
    """Running run_migrations() twice succeeds both times."""
    db = tmp_path / "idem.db"
    _create_legacy_orders_db(db)
    stamp = _run_alembic_cmd(_db_url(db), "stamp", "0002_drop_mobile_otps")
    assert stamp.returncode == 0

    original = _patch_env(_set_db_env(db))
    try:
        run_migrations()
        assert _version(db) == "0006_user_setup_tasks"
        run_migrations()  # second run: no-op
    finally:
        _restore_env(original)

    assert _version(db) == "0006_user_setup_tasks"
    assert "client_order_id" in _col_names(db, "orders")
    assert "signal_key" in _col_names(db, "orders")
    assert "ux_orders_signal_key" in _indexes(db, "orders")


# ---------------------------------------------------------------------------
# C. Migration failure raises MigrationError
# ---------------------------------------------------------------------------

def test_migration_failure_raises_migration_error():
    """Unreachable DB → MigrationError (startup fails closed)."""
    original = _patch_env({
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://user:pass@127.0.0.1:5432/x?connect_timeout=2",
        "BROKER_MODE": "simulated",
    })
    try:
        with pytest.raises(MigrationError, match="alembic upgrade head failed"):
            run_migrations()
    finally:
        _restore_env(original)


def test_lifespan_fails_closed_before_init_db(monkeypatch):
    """When migration fails, lifespan raises BEFORE init_db is called."""
    calls: list[str] = []

    async def _spy():
        calls.append("init_db")

    import asyncio

    monkeypatch.setattr("app.db.session.init_db", _spy)
    monkeypatch.setenv("DATABASE_URL",
        "postgresql+asyncpg://user:pass@127.0.0.1:5432/x?connect_timeout=1")
    monkeypatch.setenv("BROKER_MODE", "simulated")

    from app.main import lifespan
    from fastapi import FastAPI

    # asynccontextmanager wraps the raw generator in __wrapped__; call the raw
    # generator directly so we can drive it one step at a time.
    raw_gen = lifespan.__wrapped__(FastAPI())
    with pytest.raises(MigrationError):
        asyncio.run(raw_gen.asend(None))
    assert "init_db" not in calls

# ---------------------------------------------------------------------------
# D. No fallback to create_all
# ---------------------------------------------------------------------------

def test_migrations_module_never_uses_create_all():
    """migrations.py must NOT call create_all or ad-hoc ALTER TABLE."""
    src = (ROOT / "app" / "db" / "migrations.py").read_text(encoding="utf-8")
    assert "create_all" not in src
    assert "ALTER TABLE" not in src


def test_main_lifespan_never_falls_back_to_create_all():
    """main.py must NOT call create_all from the lifespan."""
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "create_all" not in src


# ---------------------------------------------------------------------------
# E. Migration precedes any request-serving path
# ---------------------------------------------------------------------------

def test_migration_precedes_init_db_and_yield_in_source():
    """run_migrations() appears BEFORE init_db() and BEFORE yield."""
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    m = src.index("run_migrations()")
    i = src.index("await init_db()")
    y = src.index("yield  #")
    assert m < i < y


def test_main_source_wires_migrations():
    """main.py imports and calls run_migrations + MigrationError."""
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "from app.db.migrations import" in src
    assert "run_migrations()" in src
    assert "MigrationError" in src


def test_migrations_module_uses_subprocess_not_alembic_internals():
    """Runner delegates to python -m alembic via subprocess (not internals)."""
    src = (ROOT / "app" / "db" / "migrations.py").read_text(encoding="utf-8")
    assert "subprocess" in src
    assert "alembic" in src
    assert "-m" in src
    # No module-level `import alembic` / `from alembic import ...` statements —
    # the runner must NOT pull Alembic internals into the running process.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import alembic") or stripped.startswith("from alembic"):
            raise AssertionError(f"migrations.py must not import Alembic internals: {stripped!r}")


def test_fail_closed_error_message_has_context():
    """MigrationError message contains the command and rc for diagnosis."""
    original = _patch_env({
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://user:pass@127.0.0.1:5432/x?connect_timeout=1",
        "BROKER_MODE": "simulated",
    })
    try:
        with pytest.raises(MigrationError) as exc_info:
            run_migrations()
        msg = str(exc_info.value)
        assert "alembic upgrade head failed" in msg
        assert "rc=" in msg
    finally:
        _restore_env(original)
