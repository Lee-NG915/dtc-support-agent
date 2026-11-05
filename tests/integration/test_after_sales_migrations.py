from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic.config import Config
from app.repositories.db import init_schema

ROOT = Path(__file__).resolve().parents[2]


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _columns(database_path: Path, table: str) -> dict[str, tuple[str, int]]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1]: (row[2].lower(), row[3]) for row in rows}


def _indexes(database_path: Path, table: str) -> set[tuple[str, int]]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f"PRAGMA index_list({table})").fetchall()
    return {(row[1], row[2]) for row in rows}


def test_sqlite_upgrade_from_empty_and_0002_is_repeatable(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = _config(f"sqlite+aiosqlite:///{database_path}")

    command.upgrade(config, "0002")
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "returns" in tables
    assert "after_sales_staff" not in tables

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    columns = _columns(database_path, "after_sales_staff")
    assert set(columns) == {
        "staff_id",
        "display_name",
        "token_sha256",
        "enabled",
        "expires_at",
        "roles",
        "allowed_markets",
        "created_at",
    }
    assert columns["staff_id"][1] == 1
    assert columns["display_name"][1] == 1
    assert columns["token_sha256"][1] == 1
    assert columns["enabled"][1] == 1
    assert columns["expires_at"][1] == 1
    assert ("ix_after_sales_staff_token_sha256", 1) in _indexes(database_path, "after_sales_staff")

    with sqlite3.connect(database_path) as connection:
        workflow_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "after_sales_case",
        "after_sales_sync_cursor",
        "after_sales_material",
        "after_sales_draft",
        "after_sales_approval",
        "after_sales_operation",
        "after_sales_event",
    } <= workflow_tables

    case_columns = _columns(database_path, "after_sales_case")
    assert case_columns["remote_sequence"][0].startswith("varchar")
    assert case_columns["current_draft_id"][0].startswith("varchar")
    assert any(unique == 1 for _, unique in _indexes(database_path, "after_sales_case"))
    assert ("ix_after_sales_case_market_state_updated", 0) in _indexes(
        database_path, "after_sales_case"
    )
    assert ("uq_after_sales_operation_active_case", 1) in _indexes(
        database_path, "after_sales_operation"
    )
    assert ("uq_after_sales_approval_pending_draft", 1) in _indexes(
        database_path, "after_sales_approval"
    )
    operation_columns = _columns(database_path, "after_sales_operation")
    assert {
        "lookup_count",
        "unknown_attempts",
        "reconciliation_state",
        "reconcile_started_at",
    } <= set(operation_columns)


def test_sqlite_downgrade_removes_the_staff_table(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = _config(f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "head")
    command.downgrade(config, "0002")
    with sqlite3.connect(database_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "after_sales_staff" not in names


def test_alembic_logging_does_not_disable_application_loggers(tmp_path: Path) -> None:
    logger = logging.getLogger("app.retrieval.cleaning")
    logger.disabled = False

    command.upgrade(_config(f"sqlite+aiosqlite:///{tmp_path / 'logging.db'}"), "head")

    assert logger.disabled is False
    assert logger.propagate is True


@pytest.mark.asyncio
async def test_postgres_startup_never_calls_create_all() -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeEngine:
        dialect = FakeDialect()

        def begin(self):
            raise AssertionError("create_all must not run on PostgreSQL")

    await init_schema(FakeEngine())  # type: ignore[arg-type]


@pytest.mark.skipif(
    not os.getenv("DTC_TEST_PG_URL"),
    reason="set DTC_TEST_PG_URL to a dedicated, disposable PostgreSQL database",
)
def test_dedicated_postgres_migration_target() -> None:
    database_url = os.environ["DTC_TEST_PG_URL"]
    config = _config(database_url)
    command.upgrade(config, "head")
    command.upgrade(config, "head")

    async def columns() -> list[str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: [
                        column["name"]
                        for column in inspect(sync_connection).get_columns("after_sales_staff")
                    ]
                )
        finally:
            await engine.dispose()

    assert asyncio.run(columns()) == [
        "staff_id",
        "display_name",
        "token_sha256",
        "enabled",
        "expires_at",
        "roles",
        "allowed_markets",
        "created_at",
    ]
