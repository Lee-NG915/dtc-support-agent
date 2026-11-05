"""Alembic migrations with an explicit target database.

Never read the application .env. Select the target with either::

    alembic -x database_url=postgresql+asyncpg://... upgrade head
    ALEMBIC_DATABASE_URL=sqlite+aiosqlite:///... alembic upgrade head
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.repositories.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _explicit_database_url() -> str:
    from_x_argument = context.get_x_argument(as_dictionary=True).get("database_url")
    configured = config.get_main_option("sqlalchemy.url")
    value = (from_x_argument or os.getenv("ALEMBIC_DATABASE_URL") or configured or "").strip()
    if not value:
        raise RuntimeError(
            "explicit migration target required: pass -x database_url=... "
            "or set ALEMBIC_DATABASE_URL"
        )
    return value


def _async_driver_url(raw: str) -> str:
    url = make_url(raw)
    if url.drivername == "sqlite":
        url = url.set(drivername="sqlite+aiosqlite")
    elif url.drivername == "postgresql":
        url = url.set(drivername="postgresql+asyncpg")
    return url.render_as_string(hide_password=False)


config.set_main_option(
    "sqlalchemy.url",
    _async_driver_url(_explicit_database_url()).replace("%", "%%"),
)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
