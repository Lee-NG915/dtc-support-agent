"""Safety and lifecycle tests for the production staff provisioning CLI.

These tests never connect to PostgreSQL. The CLI's explicit ``postgresql+asyncpg``
gate is exercised directly, while create/rotate/disable run against a temporary
SQLite database through a monkeypatched engine factory. Test setup owns schema
creation, which also proves the CLI itself never calls ``create_all``.
"""

from __future__ import annotations

import asyncio
import json
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import hash_staff_token
from app.core.time import utcnow
from app.repositories.db import init_schema
from app.repositories.models import AfterSalesStaffRow
from app.repositories.staff_repository import StaffRepository
from scripts import manage_after_sales_staff as manage

POSTGRES_URL = "postgresql+asyncpg://staff:not-a-real-password@db.example.invalid/dtc"


@pytest.fixture
def sqlite_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database_path = tmp_path / "staff.db"

    async def setup() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            await init_schema(engine)
        finally:
            await engine.dispose()

    asyncio.run(setup())

    def fake_engine(_database_url: str):
        return create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    monkeypatch.setattr(manage, "_create_engine", fake_engine)
    return database_path


def _future_expiry() -> str:
    return (utcnow() + timedelta(days=30)).isoformat()


def _target_args(database_url: str | None, database_url_file: Path | None) -> list[str]:
    if database_url_file is not None:
        return ["--database-url-file", str(database_url_file)]
    if database_url is not None:
        return ["--database-url", database_url]
    return []


def _create_args(
    output: Path,
    *,
    database_url: str | None = POSTGRES_URL,
    database_url_file: Path | None = None,
    staff_id: str = "staff_support_au",
    roles: str = "support,approver",
    markets: str = "au,sg",
    expires_at: str | None = None,
) -> list[str]:
    return [
        "create",
        *_target_args(database_url, database_url_file),
        "--staff-id",
        staff_id,
        "--display-name",
        "Synthetic Support AU",
        "--roles",
        roles,
        "--markets",
        markets,
        "--expires-at",
        expires_at or _future_expiry(),
        "--output",
        str(output),
    ]


def _rotate_args(
    output: Path,
    *,
    database_url: str | None = POSTGRES_URL,
    database_url_file: Path | None = None,
    staff_id: str = "staff_support_au",
) -> list[str]:
    return [
        "rotate",
        *_target_args(database_url, database_url_file),
        "--staff-id",
        staff_id,
        "--output",
        str(output),
    ]


def _disable_args(
    *,
    database_url: str | None = POSTGRES_URL,
    database_url_file: Path | None = None,
    staff_id: str = "staff_support_au",
) -> list[str]:
    return [
        "disable",
        *_target_args(database_url, database_url_file),
        "--staff-id",
        staff_id,
    ]


def _write_database_url_file(
    tmp_path: Path,
    *,
    contents: str | None = None,
    mode: int = 0o600,
) -> Path:
    path = tmp_path / "database-url"
    path.write_text(f"{POSTGRES_URL}\n" if contents is None else contents, encoding="utf-8")
    path.chmod(mode)
    return path


def _rows(database_path: Path) -> list[AfterSalesStaffRow]:
    async def read() -> list[AfterSalesStaffRow]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with factory() as session:
                result = await session.execute(select(AfterSalesStaffRow))
                return list(result.scalars().all())
        finally:
            await engine.dispose()

    return asyncio.run(read())


def _authenticate(database_path: Path, token: str):
    async def authenticate():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            async with factory() as session:
                return await StaffRepository(session).authenticate(token)
        finally:
            await engine.dispose()

    return asyncio.run(authenticate())


def _read_payload(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_cli_requires_explicit_database_url_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An ambient DATABASE_URL must never satisfy the required argument.
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    output = tmp_path / "credentials.json"
    args = _create_args(output)
    # Remove only the pair, preserving the rest.
    without_database_url: list[str] = []
    skip = False
    for value in args:
        if skip:
            skip = False
            continue
        if value == "--database-url":
            skip = True
            continue
        without_database_url.append(value)

    with pytest.raises(SystemExit) as missing_url:
        manage.main(without_database_url)
    assert missing_url.value.code == 2

    without_output = _create_args(output)
    output_index = without_output.index("--output")
    del without_output[output_index : output_index + 2]
    with pytest.raises(SystemExit) as missing_output:
        manage.main(without_output)
    assert missing_output.value.code == 2

    with pytest.raises(SystemExit):
        manage.main([])


@pytest.mark.parametrize("command", ["create", "rotate", "disable"])
def test_every_subcommand_requires_exactly_one_database_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    # An ambient DATABASE_URL must never satisfy the explicit target requirement.
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    output = tmp_path / "credentials.json"
    args = {
        "create": _create_args(output, database_url=None),
        "rotate": _rotate_args(output, database_url=None),
        "disable": _disable_args(database_url=None),
    }[command]

    with pytest.raises(SystemExit) as missing_target:
        manage.main(args)
    assert missing_target.value.code == 2

    captured = capsys.readouterr()
    assert POSTGRES_URL not in captured.out
    assert POSTGRES_URL not in captured.err
    assert not output.exists()


@pytest.mark.parametrize("command", ["create", "rotate", "disable"])
def test_every_subcommand_rejects_both_database_targets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    url_file = _write_database_url_file(tmp_path)
    output = tmp_path / "credentials.json"
    args = {
        "create": _create_args(output),
        "rotate": _rotate_args(output),
        "disable": _disable_args(),
    }[command]
    args[1:1] = ["--database-url-file", str(url_file)]

    with pytest.raises(SystemExit) as both_targets:
        manage.main(args)
    assert both_targets.value.code == 2

    captured = capsys.readouterr()
    assert POSTGRES_URL not in captured.out
    assert POSTGRES_URL not in captured.err
    assert not output.exists()


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_create_accepts_a_restricted_database_url_file(
    sqlite_database: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mode: int,
) -> None:
    url_file = _write_database_url_file(tmp_path, mode=mode)
    output = tmp_path / "credentials.json"

    assert manage.main(_create_args(output, database_url=None, database_url_file=url_file)) == 0

    captured = capsys.readouterr()
    payload = _read_payload(output)
    token = payload["token"]
    assert isinstance(token, str)
    assert payload["staff_id"] == "staff_support_au"
    assert POSTGRES_URL not in captured.out
    assert POSTGRES_URL not in captured.err
    assert token not in captured.out
    assert token not in captured.err
    assert stat.S_IMODE(url_file.stat().st_mode) == mode

    rows = _rows(sqlite_database)
    assert len(rows) == 1
    assert rows[0].token_sha256 == hash_staff_token(token)


def test_database_url_file_trims_surrounding_whitespace(
    sqlite_database: Path, tmp_path: Path
) -> None:
    url_file = _write_database_url_file(tmp_path, contents=f"\n   {POSTGRES_URL}   \n\n")
    output = tmp_path / "credentials.json"

    assert manage.main(_create_args(output, database_url=None, database_url_file=url_file)) == 0
    assert len(_rows(sqlite_database)) == 1


def test_rotate_and_disable_accept_a_database_url_file(
    sqlite_database: Path, tmp_path: Path
) -> None:
    url_file = _write_database_url_file(tmp_path)
    create_output = tmp_path / "create.json"
    assert (
        manage.main(_create_args(create_output, database_url=None, database_url_file=url_file)) == 0
    )
    old_token = _read_payload(create_output)["token"]
    assert isinstance(old_token, str)

    rotate_output = tmp_path / "rotate.json"
    assert (
        manage.main(_rotate_args(rotate_output, database_url=None, database_url_file=url_file)) == 0
    )
    new_token = _read_payload(rotate_output)["token"]
    assert isinstance(new_token, str)
    assert new_token != old_token
    assert _authenticate(sqlite_database, old_token) is None
    assert _authenticate(sqlite_database, new_token) is not None

    assert manage.main(_disable_args(database_url=None, database_url_file=url_file)) == 0
    assert _rows(sqlite_database)[0].enabled is False


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o700])
def test_rejects_database_url_file_with_unsafe_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: int
) -> None:
    url_file = _write_database_url_file(tmp_path, mode=mode)
    output = tmp_path / "credentials.json"

    assert manage.main(_create_args(output, database_url=None, database_url_file=url_file)) == 2

    captured = capsys.readouterr()
    assert POSTGRES_URL not in captured.out
    assert POSTGRES_URL not in captured.err
    assert not output.exists()


def test_rejects_non_regular_database_url_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "database-url"
    directory.mkdir(mode=0o600)
    output = tmp_path / "credentials.json"

    assert manage.main(_create_args(output, database_url=None, database_url_file=directory)) == 2

    captured = capsys.readouterr()
    assert not output.exists()
    assert POSTGRES_URL not in captured.out
    assert POSTGRES_URL not in captured.err


def test_rejects_missing_database_url_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "credentials.json"
    missing = tmp_path / "missing-url"

    assert manage.main(_create_args(output, database_url=None, database_url_file=missing)) == 2

    captured = capsys.readouterr()
    assert not output.exists()
    assert POSTGRES_URL not in captured.out
    assert POSTGRES_URL not in captured.err


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "\n   \n",
        f"{POSTGRES_URL}\npostgresql+asyncpg://staff2:other@db2.example.invalid/dtc\n",
        "sqlite+aiosqlite:///./local.db\n",
        "postgresql://staff:pass@db.example.invalid/dtc\n",
        "postgresql+asyncpg://staff:SUPER_SECRET@db.example.invalid\n",
    ],
)
def test_rejects_malformed_or_multiline_database_url_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], contents: str
) -> None:
    url_file = _write_database_url_file(tmp_path, contents=contents)
    output = tmp_path / "credentials.json"

    assert manage.main(_create_args(output, database_url=None, database_url_file=url_file)) == 2

    captured = capsys.readouterr()
    assert not output.exists()
    assert POSTGRES_URL not in captured.out
    assert POSTGRES_URL not in captured.err
    assert "SUPER_SECRET" not in captured.out
    assert "SUPER_SECRET" not in captured.err


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+aiosqlite:///./local.db",
        "postgresql://staff:pass@db.example.invalid/dtc",
        "postgresql+psycopg://staff:pass@db.example.invalid/dtc",
        "mysql+aiomysql://staff:pass@db.example.invalid/dtc",
    ],
)
def test_cli_rejects_non_asyncpg_database_urls(tmp_path: Path, database_url: str) -> None:
    output = tmp_path / "credentials.json"
    assert manage.main(_create_args(output, database_url=database_url)) == 2
    assert not output.exists()


def test_create_refuses_existing_output_and_leaves_database_untouched(
    sqlite_database: Path, tmp_path: Path
) -> None:
    output = tmp_path / "credentials.json"
    output.write_text("existing", encoding="utf-8")

    assert manage.main(_create_args(output)) == 2

    assert output.read_text(encoding="utf-8") == "existing"
    assert _rows(sqlite_database) == []


def test_create_writes_new_0600_file_and_stores_only_the_existing_hash(
    sqlite_database: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "credentials.json"

    assert manage.main(_create_args(output)) == 0

    captured = capsys.readouterr()
    payload = _read_payload(output)
    token = payload["token"]
    assert isinstance(token, str)
    assert token.startswith("dtc_staff_")
    assert payload["schema_version"] == "1"
    assert payload["action"] == "create"
    assert payload["staff_id"] == "staff_support_au"
    assert POSTGRES_URL not in output.read_text(encoding="utf-8")
    assert token not in captured.out
    assert token not in captured.err
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    rows = _rows(sqlite_database)
    assert len(rows) == 1
    row = rows[0]
    assert row.staff_id == "staff_support_au"
    assert row.display_name == "Synthetic Support AU"
    assert row.token_sha256 == hash_staff_token(token)
    assert row.token_sha256 != token
    assert row.enabled is True
    assert row.roles == "approver,support"
    assert row.allowed_markets == "au,sg"

    actor = _authenticate(sqlite_database, token)
    assert actor is not None
    assert actor.staff_id == "staff_support_au"
    assert actor.roles == frozenset({"approver", "support"})
    assert actor.allowed_markets == frozenset({"au", "sg"})


def test_rotate_replaces_hash_and_old_token_fails_on_the_next_request(
    sqlite_database: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_output = tmp_path / "create.json"
    assert manage.main(_create_args(create_output)) == 0
    old_token = _read_payload(create_output)["token"]
    assert isinstance(old_token, str)
    assert _authenticate(sqlite_database, old_token) is not None
    capsys.readouterr()

    rotate_output = tmp_path / "rotate.json"
    assert (
        manage.main(
            [
                "rotate",
                "--database-url",
                POSTGRES_URL,
                "--staff-id",
                "staff_support_au",
                "--output",
                str(rotate_output),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    payload = _read_payload(rotate_output)
    new_token = payload["token"]
    assert isinstance(new_token, str)
    assert new_token != old_token
    assert payload["action"] == "rotate"
    assert new_token not in captured.out
    assert new_token not in captured.err
    assert stat.S_IMODE(rotate_output.stat().st_mode) == 0o600

    rows = _rows(sqlite_database)
    assert len(rows) == 1
    assert rows[0].token_sha256 == hash_staff_token(new_token)
    assert rows[0].roles == "approver,support"
    assert rows[0].allowed_markets == "au,sg"

    assert _authenticate(sqlite_database, old_token) is None
    actor = _authenticate(sqlite_database, new_token)
    assert actor is not None
    assert actor.staff_id == "staff_support_au"


def test_rotate_refuses_existing_output_without_rotating(
    sqlite_database: Path, tmp_path: Path
) -> None:
    create_output = tmp_path / "create.json"
    assert manage.main(_create_args(create_output)) == 0
    token = _read_payload(create_output)["token"]
    assert isinstance(token, str)

    rotate_output = tmp_path / "rotate.json"
    rotate_output.write_text("existing", encoding="utf-8")
    assert (
        manage.main(
            [
                "rotate",
                "--database-url",
                POSTGRES_URL,
                "--staff-id",
                "staff_support_au",
                "--output",
                str(rotate_output),
            ]
        )
        == 2
    )

    assert rotate_output.read_text(encoding="utf-8") == "existing"
    assert _authenticate(sqlite_database, token) is not None


def test_disable_sets_enabled_false_and_the_token_stops_working(
    sqlite_database: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_output = tmp_path / "create.json"
    assert manage.main(_create_args(create_output)) == 0
    token = _read_payload(create_output)["token"]
    assert isinstance(token, str)
    assert _authenticate(sqlite_database, token) is not None
    capsys.readouterr()

    assert (
        manage.main(
            [
                "disable",
                "--database-url",
                POSTGRES_URL,
                "--staff-id",
                "staff_support_au",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    rows = _rows(sqlite_database)
    assert len(rows) == 1
    assert rows[0].enabled is False
    assert rows[0].token_sha256 == hash_staff_token(token)
    assert _authenticate(sqlite_database, token) is None


def test_rotate_and_disable_require_an_existing_staff_identity(
    sqlite_database: Path, tmp_path: Path
) -> None:
    rotate_output = tmp_path / "rotate.json"
    assert (
        manage.main(
            [
                "rotate",
                "--database-url",
                POSTGRES_URL,
                "--staff-id",
                "staff_missing",
                "--output",
                str(rotate_output),
            ]
        )
        == 2
    )
    assert not rotate_output.exists()
    assert (
        manage.main(
            [
                "disable",
                "--database-url",
                POSTGRES_URL,
                "--staff-id",
                "staff_missing",
            ]
        )
        == 2
    )
    assert _rows(sqlite_database) == []


@pytest.mark.parametrize(
    ("roles", "markets"),
    [
        ("admin", "au"),
        ("support", "eu"),
        ("", "au"),
        ("support", ""),
    ],
)
def test_create_rejects_roles_or_markets_outside_the_existing_contract(
    tmp_path: Path, roles: str, markets: str
) -> None:
    output = tmp_path / "credentials.json"
    assert manage.main(_create_args(output, roles=roles, markets=markets)) == 2
    assert not output.exists()


@pytest.mark.parametrize("expires_at", ["2999-01-01T00:00:00", "2000-01-01T00:00:00+00:00"])
def test_create_requires_a_timezone_aware_future_expiry(tmp_path: Path, expires_at: str) -> None:
    output = tmp_path / "credentials.json"
    assert manage.main(_create_args(output, expires_at=expires_at)) == 2
    assert not output.exists()
