from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import hash_staff_token
from app.repositories.models import AfterSalesStaffRow
from scripts.seed_after_sales_staff import main


def _rows(database_url: str) -> list[AfterSalesStaffRow]:
    async def read() -> list[AfterSalesStaffRow]:
        engine = create_async_engine(database_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with factory() as session:
                result = await session.execute(select(AfterSalesStaffRow))
                return list(result.scalars().all())
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_seed_creates_new_0600_file_and_stores_only_hashes(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    database_path = tmp_path / "staff.db"
    output = tmp_path / "staff-credentials.json"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    assert (
        main(
            [
                "--database-url",
                database_url,
                "--output",
                str(output),
                "--expires-in-days",
                "7",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1"
    assert len(payload["staff"]) == 5
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    tokens = {entry["staff_id"]: entry["token"] for entry in payload["staff"]}
    for entry in payload["staff"]:
        assert entry["token"] not in captured.out
        assert entry["token"] not in captured.err

    rows = _rows(database_url)
    assert len(rows) == 5
    stored = {row.staff_id: row.token_sha256 for row in rows}
    for staff_id, token in tokens.items():
        assert stored[staff_id] != token
        assert stored[staff_id] == hash_staff_token(token)
    assert all("token" not in row.token_sha256 for row in rows)
    assert {row.staff_id for row in rows} == {
        "staff_support_au",
        "staff_approver_au",
        "staff_auditor_all",
        "staff_operator_au",
        "staff_support_us",
    }


def test_seed_refuses_to_overwrite_existing_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    output = tmp_path / "staff-credentials.json"
    output.write_text("existing", encoding="utf-8")
    database_path = tmp_path / "staff.db"
    assert (
        main(
            [
                "--database-url",
                f"sqlite+aiosqlite:///{database_path}",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert output.read_text(encoding="utf-8") == "existing"
    assert not database_path.exists()


def test_seed_refuses_production_and_existing_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    production_db = tmp_path / "production.db"
    production_output = tmp_path / "production.json"
    assert (
        main(
            [
                "--database-url",
                f"sqlite+aiosqlite:///{production_db}",
                "--output",
                str(production_output),
            ]
        )
        == 2
    )
    assert not production_db.exists()
    assert not production_output.exists()

    monkeypatch.setenv("APP_ENV", "testing")
    existing_db = tmp_path / "existing.db"
    existing_db.write_bytes(b"")
    existing_output = tmp_path / "existing.json"
    assert (
        main(
            [
                "--database-url",
                f"sqlite+aiosqlite:///{existing_db}",
                "--output",
                str(existing_output),
            ]
        )
        == 2
    )
    assert not existing_output.exists()


def test_seed_rejects_memory_and_non_sqlite_targets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    output = tmp_path / "credentials.json"
    assert (
        main(
            [
                "--database-url",
                "sqlite+aiosqlite:///:memory:",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert (
        main(
            [
                "--database-url",
                "postgresql+asyncpg://user:pass@example.invalid/db",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert not output.exists()
