"""TradeThrone Alembic migration environment.

Supports both synchronous (PostgreSQL via psycopg2) and online async
(PostgreSQL via asyncpg / SQLite via aiosqlite) engines.  URL resolution
delegates to ``app.db.session.normalize_database_url`` so cloud connection
strings (Supabase/Neon/Railway poolers with ``?sslmode``) are transformed
exactly as the runtime application would.

Usage:
    alembic revision --autogenerate -m "describe change"
    alembic upgrade head              # apply pending migrations
    alembic downgrade -1              # roll back one step
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.db.session import Base, normalize_database_url

# Register every ORM model so autogenerate sees the full schema.
from app.models import (  # noqa: F401  (side-effect: populates Base.metadata)
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

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use the application's normalized database URL (respects DATABASE_URL env var).
config.set_main_option("sqlalchemy.url", normalize_database_url(settings.database_url))

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """Configure the context with the live connection and run migrations."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Create an async engine from config and run migrations over it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using the async engine."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
