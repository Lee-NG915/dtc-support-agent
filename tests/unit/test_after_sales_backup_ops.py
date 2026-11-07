"""Focused tests for the backup, restore and operations-check scripts.

The unit suite never starts pg_dump, psql or pg_restore, never contacts
PostgreSQL or the API, and never reads the real clock or filesystem usage.
Module fixtures block the real subprocess/HTTP/clock seams by default; each
path installs fakes that record the command, child environment, timeout,
request and output.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from urllib.request import ProxyHandler

import pytest

from scripts import (
    backup_after_sales_pg as backup,
    check_after_sales_operations as operations,
    restore_after_sales_pg as restore,
)

_PASSWORD = "pg-password-sentinel"
_ASYNC_URL = (
    f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:5432/"
    "dtc_production?sslmode=require"
)
_SANITIZED_URL = "postgresql://dtc@db.example.invalid:5432/dtc_production?sslmode=require"


@dataclass(frozen=True)
class _Call:
    command: list[str]
    env: dict[str, str]
    timeout: float


class _FakePgDump:
    def __init__(
        self,
        *,
        payload: bytes = b"PGDMP-fake-custom-backup",
        returncode: int = 0,
        timed_out: bool = False,
        start_error: BaseException | None = None,
    ) -> None:
        self.payload = payload
        self.returncode = returncode
        self.timed_out = timed_out
        self.start_error = start_error
        self.calls: list[_Call] = []

    def __call__(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        stdout: BinaryIO,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(_Call(command=list(command), env=dict(env), timeout=timeout))
        if self.start_error is not None:
            raise self.start_error
        if self.payload:
            stdout.write(self.payload)
        if self.timed_out:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, self.returncode)


@pytest.fixture(autouse=True)
def _block_real_pg_dump(monkeypatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit tests must never execute a real pg_dump")

    monkeypatch.setattr(backup, "run_pg_dump", fail)


@pytest.fixture(autouse=True)
def _block_real_restore_subprocesses(monkeypatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit tests must never execute real psql, pg_restore, or API/Worker")

    monkeypatch.setattr(restore, "run_psql", fail)
    monkeypatch.setattr(restore, "run_pg_restore", fail)
    # No code path may spawn any other child process (for example API/Worker).
    monkeypatch.setattr(subprocess, "run", fail)


def _install_fake(monkeypatch, fake: _FakePgDump) -> _FakePgDump:
    monkeypatch.setattr(backup, "run_pg_dump", fake)
    return fake


def _write_url_file(
    directory: Path,
    url: str = _ASYNC_URL,
    *,
    mode: int = 0o600,
    name: str = "database-url",
    text: str | None = None,
) -> Path:
    path = directory / name
    path.write_text(text if text is not None else f"{url}\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _main_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--database-url",
        _ASYNC_URL,
        "--output-dir",
        str(tmp_path),
        "--output-file",
        "after-sales.dump",
        "--timeout",
        "30",
        *extra,
    ]


def _assert_no_secret(text: str) -> None:
    assert _PASSWORD not in text
    assert _ASYNC_URL not in text


def test_backup_writes_new_0600_custom_dump_without_secret_leaks(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    monkeypatch.setenv("PGPASSWORD", "ambient-password")
    monkeypatch.setenv("PGHOST", "ambient.invalid")
    monkeypatch.setenv("DATABASE_URL", _ASYNC_URL)

    assert backup.main(_main_args(tmp_path)) == 0

    output = tmp_path / "after-sales.dump"
    assert output.read_bytes() == b"PGDMP-fake-custom-backup"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert len(fake.calls) == 1

    call = fake.calls[0]
    assert call.command[:3] == ["pg_dump", "--format=custom", "--no-password"]
    assert call.command[3] == f"--dbname={_SANITIZED_URL}"
    assert call.timeout == 30.0
    assert call.env["PGPASSWORD"] == _PASSWORD
    assert call.env["PGPASSFILE"] == os.devnull
    assert "PGHOST" not in call.env
    assert "DATABASE_URL" not in call.env

    joined_command = " ".join(call.command)
    _assert_no_secret(joined_command)
    captured = capsys.readouterr()
    _assert_no_secret(captured.out)
    _assert_no_secret(captured.err)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_backup_uses_protected_url_file_and_decodes_password(
    tmp_path: Path, monkeypatch, capsys, mode: int
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    encoded_url = (
        "postgresql+asyncpg://dtc:p%40ss%3Aword%2Fsentinel@db.example.invalid:5432/dtc_production"
    )
    url_file = _write_url_file(tmp_path, encoded_url, mode=mode)

    code = backup.main(
        [
            "--database-url-file",
            str(url_file),
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "url-file.dump",
            "--timeout",
            "30",
        ]
    )

    assert code == 0
    call = fake.calls[0]
    assert call.env["PGPASSWORD"] == "p@ss:word/sentinel"
    joined_command = " ".join(call.command)
    assert "p%40ss%3Aword" not in joined_command
    assert "p@ss:word/sentinel" not in joined_command
    assert "postgresql+asyncpg" not in joined_command
    captured = capsys.readouterr()
    assert "p@ss:word/sentinel" not in captured.out + captured.err
    assert "p%40ss%3Aword" not in captured.out + captured.err


def test_url_file_rejects_unsafe_mode_without_running_pg_dump(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    url_file = _write_url_file(tmp_path, mode=0o644)

    code = backup.main(
        [
            "--database-url-file",
            str(url_file),
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "unsafe.dump",
        ]
    )

    assert code == backup.EXIT_INPUT
    assert fake.calls == []
    assert not (tmp_path / "unsafe.dump").exists()
    captured = capsys.readouterr()
    assert "permissions must be 0400 or 0600, got 0644" in captured.err
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{_ASYNC_URL}\n{_ASYNC_URL}\n", "exactly one line"),
        ("\n", "is empty"),
        ("   \n", "is empty"),
    ],
)
def test_url_file_rejects_multiline_and_empty_content(
    tmp_path: Path, monkeypatch, capsys, text: str, expected: str
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    url_file = _write_url_file(tmp_path, mode=0o600, text=text)

    code = backup.main(
        [
            "--database-url-file",
            str(url_file),
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "bad-file.dump",
        ]
    )

    assert code == backup.EXIT_INPUT
    assert fake.calls == []
    assert not (tmp_path / "bad-file.dump").exists()
    captured = capsys.readouterr()
    assert expected in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_url_file_must_exist_and_be_a_regular_file(tmp_path: Path, monkeypatch, capsys) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    missing = tmp_path / "missing-url"
    directory = tmp_path / "url-dir"
    directory.mkdir()

    for url_file in (missing, directory):
        assert (
            backup.main(
                [
                    "--database-url-file",
                    str(url_file),
                    "--output-dir",
                    str(tmp_path),
                    "--output-file",
                    "missing.dump",
                ]
            )
            == backup.EXIT_INPUT
        )
    assert fake.calls == []
    assert not (tmp_path / "missing.dump").exists()
    captured = capsys.readouterr()
    assert "does not exist" in captured.err
    assert "must be a regular file" in captured.err


def test_requires_exactly_one_explicit_url_source(tmp_path: Path, monkeypatch, capsys) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    monkeypatch.setenv("DATABASE_URL", _ASYNC_URL)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"DATABASE_URL={_ASYNC_URL}\n", encoding="utf-8")
    url_file = _write_url_file(tmp_path)

    with pytest.raises(SystemExit) as exc:
        backup.main(["--output-dir", str(tmp_path)])
    assert exc.value.code == backup.EXIT_INPUT

    with pytest.raises(SystemExit) as exc:
        backup.main(
            [
                "--database-url",
                _ASYNC_URL,
                "--database-url-file",
                str(url_file),
                "--output-dir",
                str(tmp_path),
            ]
        )
    assert exc.value.code == backup.EXIT_INPUT

    assert fake.calls == []
    captured = capsys.readouterr()
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///./data/local/app.db",
        f"postgresql://dtc:{_PASSWORD}@db.example.invalid:5432/dtc_production",
        f"mysql+asyncmy://dtc:{_PASSWORD}@db.example.invalid:3306/dtc_production",
        f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:5432",
        f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:5432/db#fragment",
        f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:notaport/db",
        f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:5432/db?password={_PASSWORD}",
        f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:5432/db?passfile=/tmp/x",
    ],
)
def test_rejects_non_asyncpg_or_unsafe_database_urls(
    tmp_path: Path, monkeypatch, capsys, url: str
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())

    code = backup.main(
        [
            "--database-url",
            url,
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "rejected.dump",
        ]
    )

    assert code == backup.EXIT_INPUT
    assert fake.calls == []
    assert not (tmp_path / "rejected.dump").exists()
    captured = capsys.readouterr()
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    "param",
    [
        "host=attacker.invalid",
        "hostaddr=192.0.2.1",
        "port=6543",
        "dbname=other_database",
        "user=other_user",
        f"password={_PASSWORD}",
        "passfile=%2Ftmp%2Fpgpass",
        "service=other_service",
        "servicefile=%2Ftmp%2Fservice",
        "options=-c%20search_path%3Devil",
    ],
)
def test_rejects_query_parameters_that_can_override_the_target(
    tmp_path: Path, monkeypatch, capsys, param: str
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    url = f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:5432/dtc_production?{param}"

    code = backup.main(
        [
            "--database-url",
            url,
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "rejected.dump",
        ]
    )

    assert code == backup.EXIT_INPUT
    assert fake.calls == []
    assert not (tmp_path / "rejected.dump").exists()
    captured = capsys.readouterr()
    assert "not allowed" in captured.err
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    "query",
    [
        "sslmode=require&sslmode=disable",
        "sslmode=require&sslmode=require",
        "application_name=first&application_name=second",
        "connect_timeout=5&connect_timeout=10",
    ],
)
def test_rejects_duplicate_query_parameters(
    tmp_path: Path, monkeypatch, capsys, query: str
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    url = f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:5432/dtc_production?{query}"

    code = backup.main(
        [
            "--database-url",
            url,
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "duplicate.dump",
        ]
    )

    assert code == backup.EXIT_INPUT
    assert fake.calls == []
    assert not (tmp_path / "duplicate.dump").exists()
    captured = capsys.readouterr()
    assert "must not repeat query parameters" in captured.err
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("=value", "must not have empty names"),
        ("%00=value", "control characters"),
        ("%09sslmode=require", "control characters"),
        ("sslmode=%00", "control characters"),
        ("sslmode=require%0A", "control characters"),
    ],
)
def test_rejects_empty_or_control_character_query_parameters(
    tmp_path: Path, monkeypatch, capsys, query: str, expected: str
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    url = f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:5432/dtc_production?{query}"

    code = backup.main(
        [
            "--database-url",
            url,
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "bad-query.dump",
        ]
    )

    assert code == backup.EXIT_INPUT
    assert fake.calls == []
    assert not (tmp_path / "bad-query.dump").exists()
    captured = capsys.readouterr()
    assert expected in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_accepts_allowlisted_query_parameters_and_preserves_values(
    tmp_path: Path, monkeypatch
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    query = (
        "sslmode=verify-full"
        "&sslrootcert=%2Fetc%2Fssl%2Fca.pem"
        "&sslcert=%2Fetc%2Fssl%2Fclient.pem"
        "&sslkey=%2Fetc%2Fssl%2Fclient.key"
        "&sslcrl=%2Fetc%2Fssl%2Fcrl.pem"
        "&connect_timeout=10"
        "&application_name=dtc%20backup%26probe"
        "&channel_binding=require"
    )
    url = f"postgresql+asyncpg://dtc:{_PASSWORD}@db.example.invalid:5432/dtc_production?{query}"

    code = backup.main(
        [
            "--database-url",
            url,
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "allowlisted.dump",
        ]
    )

    assert code == 0
    assert fake.calls[0].command[3] == (
        f"--dbname=postgresql://dtc@db.example.invalid:5432/dtc_production?{query}"
    )
    assert fake.calls[0].env["PGPASSWORD"] == _PASSWORD


def test_refuses_to_overwrite_existing_file_without_touching_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    output = tmp_path / "after-sales.dump"
    output.write_bytes(b"existing backup")
    output.chmod(0o600)

    assert backup.main(_main_args(tmp_path)) == backup.EXIT_INPUT

    assert output.read_bytes() == b"existing backup"
    assert fake.calls == []
    captured = capsys.readouterr()
    assert "refusing to overwrite" in captured.err
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    "output_file",
    ["../escape.dump", "/tmp/escape.dump", "sub/dir.dump", "sub\\dir.dump", ""],
)
def test_rejects_output_names_outside_the_output_directory(
    tmp_path: Path, monkeypatch, capsys, output_file: str
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())

    code = backup.main(
        [
            "--database-url",
            _ASYNC_URL,
            "--output-dir",
            str(tmp_path),
            "--output-file",
            output_file,
        ]
    )

    assert code == backup.EXIT_INPUT
    assert fake.calls == []
    assert not (tmp_path.parent / "escape.dump").exists()
    captured = capsys.readouterr()
    assert "single file name" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_rejects_missing_or_non_directory_output_directory(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    not_a_directory = tmp_path / "file-not-dir"
    not_a_directory.write_text("x", encoding="utf-8")

    for output_dir in (tmp_path / "missing-dir", not_a_directory):
        assert (
            backup.main(
                [
                    "--database-url",
                    _ASYNC_URL,
                    "--output-dir",
                    str(output_dir),
                    "--output-file",
                    "backup.dump",
                ]
            )
            == backup.EXIT_INPUT
        )
    assert fake.calls == []
    captured = capsys.readouterr()
    assert "does not exist" in captured.err
    assert "is not a directory" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_pg_dump_failure_removes_partial_output_and_sanitizes_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _install_fake(
        monkeypatch,
        _FakePgDump(payload=f"partial-{_PASSWORD}".encode(), returncode=2),
    )

    assert backup.main(_main_args(tmp_path)) == backup.EXIT_FAILURE

    assert not (tmp_path / "after-sales.dump").exists()
    captured = capsys.readouterr()
    assert "pg_dump exited with status 2" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_pg_dump_timeout_removes_partial_output_and_sanitizes_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _install_fake(
        monkeypatch,
        _FakePgDump(payload=f"partial-{_PASSWORD}".encode(), timed_out=True),
    )

    assert backup.main(_main_args(tmp_path)) == backup.EXIT_FAILURE

    assert not (tmp_path / "after-sales.dump").exists()
    captured = capsys.readouterr()
    assert "pg_dump timed out" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_missing_pg_dump_removes_output_and_sanitizes_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _install_fake(
        monkeypatch,
        _FakePgDump(start_error=FileNotFoundError(f"missing {_PASSWORD}")),
    )

    assert backup.main(_main_args(tmp_path)) == backup.EXIT_FAILURE

    assert not (tmp_path / "after-sales.dump").exists()
    captured = capsys.readouterr()
    assert "pg_dump executable was not found" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_empty_pg_dump_output_is_rejected_and_removed(tmp_path: Path, monkeypatch, capsys) -> None:
    _install_fake(monkeypatch, _FakePgDump(payload=b""))

    assert backup.main(_main_args(tmp_path)) == backup.EXIT_FAILURE

    assert not (tmp_path / "after-sales.dump").exists()
    captured = capsys.readouterr()
    assert "produced an empty file" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_output_mode_is_0600_even_with_a_restrictive_umask(tmp_path: Path, monkeypatch) -> None:
    _install_fake(monkeypatch, _FakePgDump())
    previous_umask = os.umask(0o777)
    try:
        assert backup.main(_main_args(tmp_path)) == 0
    finally:
        os.umask(previous_umask)

    output = tmp_path / "after-sales.dump"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.read_bytes() == b"PGDMP-fake-custom-backup"


def test_default_output_file_is_timestamped_and_0600(tmp_path: Path, monkeypatch) -> None:
    _install_fake(monkeypatch, _FakePgDump())

    code = backup.main(
        [
            "--database-url",
            _ASYNC_URL,
            "--output-dir",
            str(tmp_path),
            "--timeout",
            "30",
        ]
    )

    assert code == 0
    outputs = list(tmp_path.glob("after_sales_*.dump"))
    assert len(outputs) == 1
    assert stat.S_IMODE(outputs[0].stat().st_mode) == 0o600


def test_url_without_password_does_not_reuse_ambient_pgpassword(
    tmp_path: Path, monkeypatch
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())
    monkeypatch.setenv("PGPASSWORD", "ambient-password")

    code = backup.main(
        [
            "--database-url",
            "postgresql+asyncpg://dtc@db.example.invalid:5432/dtc_production",
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "no-password.dump",
        ]
    )

    assert code == 0
    assert "PGPASSWORD" not in fake.calls[0].env
    assert fake.calls[0].env["PGPASSFILE"] == os.devnull


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "86401"])
def test_timeout_must_be_finite_and_bounded(
    tmp_path: Path, monkeypatch, capsys, timeout: str
) -> None:
    fake = _install_fake(monkeypatch, _FakePgDump())

    code = backup.main(
        [
            "--database-url",
            _ASYNC_URL,
            "--output-dir",
            str(tmp_path),
            "--output-file",
            "timeout.dump",
            "--timeout",
            timeout,
        ]
    )

    assert code == backup.EXIT_INPUT
    assert fake.calls == []
    assert not (tmp_path / "timeout.dump").exists()
    captured = capsys.readouterr()
    assert "--timeout must be between 1 and 86400 seconds" in captured.err
    _assert_no_secret(captured.out + captured.err)


# ---------------------------------------------------------------------------
# Restore behavior
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RestoreCall:
    command: list[str]
    env: dict[str, str]
    timeout: float


@dataclass(frozen=True)
class _PsqlResponse:
    stdout: bytes = b""
    returncode: int = 0
    stderr: bytes = b""


class _FakePsql:
    """Serve queued responses in call order and record every invocation."""

    def __init__(self, responses: list[_PsqlResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[_RestoreCall] = []

    def __call__(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(_RestoreCall(command=list(command), env=dict(env), timeout=timeout))
        if not self.responses:
            raise AssertionError("unexpected psql call")
        response = self.responses.pop(0)
        return subprocess.CompletedProcess(
            command, response.returncode, response.stdout, response.stderr
        )


class _FakePgRestore:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stderr: bytes = b"",
        timed_out: bool = False,
        start_error: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out
        self.start_error = start_error
        self.calls: list[_RestoreCall] = []

    def __call__(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(_RestoreCall(command=list(command), env=dict(env), timeout=timeout))
        if self.start_error is not None:
            raise self.start_error
        if self.timed_out:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, self.returncode, b"", self.stderr)


def _install_psql(monkeypatch, fake: _FakePsql) -> _FakePsql:
    monkeypatch.setattr(restore, "run_psql", fake)
    return fake


def _install_pg_restore(monkeypatch, fake: _FakePgRestore) -> _FakePgRestore:
    monkeypatch.setattr(restore, "run_pg_restore", fake)
    return fake


def _write_backup_file(
    directory: Path,
    *,
    payload: bytes = b"PGDMP-fake-custom-backup",
    mode: int = 0o600,
    name: str = "after-sales.dump",
) -> Path:
    path = directory / name
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def _restore_args(backup_file: Path, *extra: str) -> list[str]:
    return [
        "--database-url",
        _ASYNC_URL,
        "--backup-file",
        str(backup_file),
        "--confirm-worker-stopped",
        "--timeout",
        "30",
        *extra,
    ]


def _restored_table_names(*, omit: frozenset[str] = frozenset()) -> list[str]:
    names = (set(restore.REQUIRED_AFTER_SALES_TABLES) | {"alembic_version", "orders"}) - omit
    return sorted(names)


def _tables_stdout(names: list[str]) -> bytes:
    return ("\n".join(names) + "\n").encode()


def test_required_after_sales_tables_match_the_0003_to_0005_models() -> None:
    assert restore.REQUIRED_AFTER_SALES_TABLES == frozenset(
        {
            "after_sales_staff",
            "after_sales_case",
            "after_sales_sync_cursor",
            "after_sales_material",
            "after_sales_draft",
            "after_sales_approval",
            "after_sales_operation",
            "after_sales_event",
        }
    )


def test_restore_accepts_empty_target_and_uses_required_flags(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    restored = _restored_table_names()
    psql = _install_psql(
        monkeypatch,
        _FakePsql(
            [
                _PsqlResponse(stdout=b""),
                _PsqlResponse(stdout=b"0005\n"),
                _PsqlResponse(stdout=_tables_stdout(restored)),
            ]
        ),
    )
    pg_restore = _install_pg_restore(monkeypatch, _FakePgRestore())
    monkeypatch.setenv("PGPASSWORD", "ambient-password")
    monkeypatch.setenv("PGHOST", "ambient.invalid")
    monkeypatch.setenv("DATABASE_URL", _ASYNC_URL)
    backup_file = _write_backup_file(tmp_path)

    assert restore.main(_restore_args(backup_file)) == restore.EXIT_OK

    assert len(psql.calls) == 3
    assert len(pg_restore.calls) == 1
    call = pg_restore.calls[0]
    assert call.command[0] == "pg_restore"
    for flag in (
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--single-transaction",
    ):
        assert flag in call.command
    assert f"--dbname={_SANITIZED_URL}" in call.command
    assert call.command[-1] == str(backup_file)
    assert call.timeout == 30.0
    assert call.env["PGPASSWORD"] == _PASSWORD
    assert call.env["PGPASSFILE"] == os.devnull
    assert "PGHOST" not in call.env
    assert "DATABASE_URL" not in call.env
    _assert_no_secret(" ".join(call.command))

    for psql_call in psql.calls:
        assert f"--dbname={_SANITIZED_URL}" in psql_call.command
        assert psql_call.env["PGPASSWORD"] == _PASSWORD
        assert psql_call.env["PGPASSFILE"] == os.devnull
        _assert_no_secret(" ".join(psql_call.command))
    preflight_sql = psql.calls[0].command[-1]
    assert "pg_catalog" in preflight_sql
    assert "information_schema" in preflight_sql
    assert "alembic_version" in psql.calls[1].command[-1]

    captured = capsys.readouterr()
    _assert_no_secret(captured.out + captured.err)
    assert "alembic_version=0005" in captured.out
    assert f"tables={len(restored)}" in captured.out
    for table in restored:
        assert table in captured.out


def test_restore_rejects_non_empty_target_before_pg_restore(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    psql = _install_psql(
        monkeypatch,
        _FakePsql([_PsqlResponse(stdout=b"orders\nalembic_version\n")]),
    )
    pg_restore = _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_file = _write_backup_file(tmp_path)

    assert restore.main(_restore_args(backup_file)) == restore.EXIT_INPUT

    assert len(psql.calls) == 1
    assert pg_restore.calls == []
    captured = capsys.readouterr()
    assert "target database is not empty" in captured.err
    assert "2 non-system table(s)" in captured.err
    assert "orders" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_restore_requires_worker_stopped_confirmation(tmp_path: Path, monkeypatch, capsys) -> None:
    psql = _install_psql(monkeypatch, _FakePsql([]))
    pg_restore = _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_file = _write_backup_file(tmp_path)

    code = restore.main(["--database-url", _ASYNC_URL, "--backup-file", str(backup_file)])

    assert code == restore.EXIT_INPUT
    assert psql.calls == []
    assert pg_restore.calls == []
    captured = capsys.readouterr()
    assert "--confirm-worker-stopped is required" in captured.err
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644, 0o700])
def test_restore_rejects_backup_file_modes_other_than_0600(
    tmp_path: Path, monkeypatch, capsys, mode: int
) -> None:
    psql = _install_psql(monkeypatch, _FakePsql([]))
    pg_restore = _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_file = _write_backup_file(tmp_path, mode=mode)

    assert restore.main(_restore_args(backup_file)) == restore.EXIT_INPUT

    assert psql.calls == []
    assert pg_restore.calls == []
    captured = capsys.readouterr()
    assert f"backup file permissions must be 0600, got {mode:04o}" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_restore_rejects_non_regular_backup_file(tmp_path: Path, monkeypatch, capsys) -> None:
    psql = _install_psql(monkeypatch, _FakePsql([]))
    pg_restore = _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_dir = tmp_path / "backup-dir"
    backup_dir.mkdir()
    backup_dir.chmod(0o600)

    assert restore.main(_restore_args(backup_dir)) == restore.EXIT_INPUT

    assert psql.calls == []
    assert pg_restore.calls == []
    captured = capsys.readouterr()
    assert "backup file must be a regular file" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_restore_rejects_backup_without_custom_dump_magic(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    psql = _install_psql(monkeypatch, _FakePsql([]))
    pg_restore = _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_file = _write_backup_file(tmp_path, payload=b"plain-sql-dump")

    assert restore.main(_restore_args(backup_file)) == restore.EXIT_INPUT

    assert psql.calls == []
    assert pg_restore.calls == []
    captured = capsys.readouterr()
    assert "PGDMP magic missing" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_restore_rejects_unsafe_database_url_file_mode(tmp_path: Path, monkeypatch, capsys) -> None:
    psql = _install_psql(monkeypatch, _FakePsql([]))
    pg_restore = _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_file = _write_backup_file(tmp_path)
    url_file = _write_url_file(tmp_path, mode=0o644)

    code = restore.main(
        [
            "--database-url-file",
            str(url_file),
            "--backup-file",
            str(backup_file),
            "--confirm-worker-stopped",
        ]
    )

    assert code == restore.EXIT_INPUT
    assert psql.calls == []
    assert pg_restore.calls == []
    captured = capsys.readouterr()
    assert "permissions must be 0400 or 0600, got 0644" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_restore_uses_url_file_password_only_in_child_environment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    encoded_password = "p%40ss%3Aword%2Fsentinel"
    decoded_password = "p@ss:word/sentinel"
    url = f"postgresql+asyncpg://dtc:{encoded_password}@db.example.invalid:5432/dtc_production"
    url_file = _write_url_file(tmp_path, url, mode=0o600)
    backup_file = _write_backup_file(tmp_path)
    psql = _install_psql(
        monkeypatch,
        _FakePsql(
            [
                _PsqlResponse(
                    returncode=2,
                    stderr=f"{decoded_password} {encoded_password} {url}".encode(),
                )
            ]
        ),
    )
    pg_restore = _install_pg_restore(monkeypatch, _FakePgRestore())

    code = restore.main(
        [
            "--database-url-file",
            str(url_file),
            "--backup-file",
            str(backup_file),
            "--confirm-worker-stopped",
        ]
    )

    assert code == restore.EXIT_FAILURE
    assert pg_restore.calls == []
    assert len(psql.calls) == 1
    assert psql.calls[0].env["PGPASSWORD"] == decoded_password
    joined_command = " ".join(psql.calls[0].command)
    assert decoded_password not in joined_command
    assert encoded_password not in joined_command
    assert "postgresql+asyncpg" not in joined_command

    captured = capsys.readouterr()
    assert decoded_password not in captured.out + captured.err
    assert encoded_password not in captured.out + captured.err
    assert url not in captured.out + captured.err
    assert "pre-restore target check failed (psql exited with status 2)" in captured.err


def test_pg_restore_failure_is_sanitized_and_skips_verification(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    psql = _install_psql(monkeypatch, _FakePsql([_PsqlResponse(stdout=b"")]))
    _install_pg_restore(
        monkeypatch,
        _FakePgRestore(
            returncode=3,
            stderr=f"pg_restore: {_PASSWORD} {_ASYNC_URL}".encode(),
        ),
    )
    backup_file = _write_backup_file(tmp_path)

    assert restore.main(_restore_args(backup_file)) == restore.EXIT_FAILURE

    # Only the pre-restore target query ran; no post-restore verification.
    assert len(psql.calls) == 1
    captured = capsys.readouterr()
    assert "pg_restore exited with status 3" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_restore_rejects_a_wrong_alembic_version(tmp_path: Path, monkeypatch, capsys) -> None:
    psql = _install_psql(
        monkeypatch,
        _FakePsql(
            [
                _PsqlResponse(stdout=b""),
                _PsqlResponse(stdout=b"0004\n"),
            ]
        ),
    )
    _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_file = _write_backup_file(tmp_path)

    assert restore.main(_restore_args(backup_file)) == restore.EXIT_FAILURE

    assert len(psql.calls) == 2
    captured = capsys.readouterr()
    assert "alembic_version is 0004; expected 0005" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_restore_rejects_missing_required_after_sales_tables(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    restored = _restored_table_names(omit=frozenset({"after_sales_event"}))
    psql = _install_psql(
        monkeypatch,
        _FakePsql(
            [
                _PsqlResponse(stdout=b""),
                _PsqlResponse(stdout=b"0005\n"),
                _PsqlResponse(stdout=_tables_stdout(restored)),
            ]
        ),
    )
    _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_file = _write_backup_file(tmp_path)

    assert restore.main(_restore_args(backup_file)) == restore.EXIT_FAILURE

    assert len(psql.calls) == 3
    captured = capsys.readouterr()
    assert "missing required after-sales table(s): after_sales_event" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_expected_version_is_configurable(tmp_path: Path, monkeypatch, capsys) -> None:
    restored = _restored_table_names()
    _install_psql(
        monkeypatch,
        _FakePsql(
            [
                _PsqlResponse(stdout=b""),
                _PsqlResponse(stdout=b"0004\n"),
                _PsqlResponse(stdout=_tables_stdout(restored)),
            ]
        ),
    )
    _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_file = _write_backup_file(tmp_path)

    code = restore.main(_restore_args(backup_file, "--expected-version", "0004"))

    assert code == restore.EXIT_OK
    captured = capsys.readouterr()
    assert "alembic_version=0004" in captured.out
    _assert_no_secret(captured.out + captured.err)


def test_restore_timeout_is_sanitized(tmp_path: Path, monkeypatch, capsys) -> None:
    psql = _install_psql(monkeypatch, _FakePsql([_PsqlResponse(stdout=b"")]))
    _install_pg_restore(
        monkeypatch,
        _FakePgRestore(timed_out=True),
    )
    backup_file = _write_backup_file(tmp_path)

    assert restore.main(_restore_args(backup_file)) == restore.EXIT_FAILURE

    assert len(psql.calls) == 1
    captured = capsys.readouterr()
    assert "pg_restore timed out" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_missing_pg_restore_executable_is_sanitized(tmp_path: Path, monkeypatch, capsys) -> None:
    _install_psql(monkeypatch, _FakePsql([_PsqlResponse(stdout=b"")]))
    _install_pg_restore(
        monkeypatch,
        _FakePgRestore(start_error=FileNotFoundError(f"missing {_PASSWORD}")),
    )
    backup_file = _write_backup_file(tmp_path)

    assert restore.main(_restore_args(backup_file)) == restore.EXIT_FAILURE

    captured = capsys.readouterr()
    assert "pg_restore executable was not found" in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_restore_requires_exactly_one_explicit_url_source(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    psql = _install_psql(monkeypatch, _FakePsql([]))
    pg_restore = _install_pg_restore(monkeypatch, _FakePgRestore())
    backup_file = _write_backup_file(tmp_path)
    url_file = _write_url_file(tmp_path)

    with pytest.raises(SystemExit) as exc:
        restore.main(
            [
                "--database-url",
                _ASYNC_URL,
                "--database-url-file",
                str(url_file),
                "--backup-file",
                str(backup_file),
                "--confirm-worker-stopped",
            ]
        )
    assert exc.value.code == restore.EXIT_INPUT

    assert psql.calls == []
    assert pg_restore.calls == []
    captured = capsys.readouterr()
    _assert_no_secret(captured.out + captured.err)


# ---------------------------------------------------------------------------
# Read-only operations checks
# ---------------------------------------------------------------------------

_OPS_NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
_OPS_API_URL = "http://127.0.0.1:8000/api/v1/ready"


@dataclass(frozen=True)
class _OpsApiCall:
    url: str
    timeout: float


class _OpsFakeApi:
    def __init__(
        self,
        *,
        response: operations.ApiReadyResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response or operations.ApiReadyResponse(status=200, body=b'{"ready": true}')
        self.error = error
        self.calls: list[_OpsApiCall] = []

    def __call__(self, url: str, *, timeout: float) -> operations.ApiReadyResponse:
        self.calls.append(_OpsApiCall(url=url, timeout=timeout))
        if self.error is not None:
            raise self.error
        return self.response


class _OpsFakeDisk:
    def __init__(
        self,
        *,
        free_gb: float = 10.0,
        error: BaseException | None = None,
    ) -> None:
        self.free_gb = free_gb
        self.error = error
        self.calls: list[Path] = []

    def __call__(self, path: Path) -> operations.DiskUsage:
        self.calls.append(path)
        if self.error is not None:
            raise self.error
        free = int(self.free_gb * 1024**3)
        return operations.DiskUsage(total=free * 4, used=free * 3, free=free)


@pytest.fixture(autouse=True)
def _block_real_operations_services(monkeypatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit tests must never run real psql or contact the API")

    monkeypatch.setattr(operations, "run_psql", fail)
    monkeypatch.setattr(operations, "fetch_api_ready", fail)


@pytest.fixture(autouse=True)
def _freeze_operations_clock(monkeypatch) -> None:
    monkeypatch.setattr(operations, "now_utc", lambda: _OPS_NOW)


def _install_ops_psql(monkeypatch, fake: _FakePsql) -> _FakePsql:
    monkeypatch.setattr(operations, "run_psql", fake)
    return fake


def _install_ops_api(monkeypatch, fake: _OpsFakeApi) -> _OpsFakeApi:
    monkeypatch.setattr(operations, "fetch_api_ready", fake)
    return fake


def _install_ops_disk(monkeypatch, fake: _OpsFakeDisk) -> _OpsFakeDisk:
    monkeypatch.setattr(operations, "disk_usage", fake)
    return fake


def _ops_cursor_rows(
    *,
    markets: tuple[str, ...] = ("au", "sg", "us"),
    age_seconds: float = 60,
    null_markets: tuple[str, ...] = (),
    overrides: dict[str, str] | None = None,
) -> bytes:
    values = overrides or {}
    lines: list[str] = []
    for market in markets:
        if market in null_markets:
            value = ""
        else:
            value = values.get(market, str(_OPS_NOW.timestamp() - age_seconds))
        lines.append(f"{market}|{value}")
    return ("\n".join(lines) + "\n").encode()


def _ops_db_pass(cursor: bytes | None = None) -> list[_PsqlResponse]:
    return [
        _PsqlResponse(stdout=b"1\n"),
        _PsqlResponse(stdout=b"0005\n"),
        _PsqlResponse(stdout=cursor if cursor is not None else _ops_cursor_rows()),
    ]


def _write_ops_backup(
    directory: Path,
    *,
    age_seconds: float = 60,
    mode: int = 0o600,
    payload: bytes = b"PGDMP-check",
    name: str = "after_sales_20261107T000000Z.dump",
) -> Path:
    path = directory / name
    path.write_bytes(payload)
    path.chmod(mode)
    timestamp = _OPS_NOW.timestamp() - age_seconds
    os.utime(path, (timestamp, timestamp))
    return path


def _ops_args(
    tmp_path: Path,
    *,
    source: list[str] | None = None,
    backup_dir: Path | None = None,
    disk_path: Path | None = None,
    api_url: str = _OPS_API_URL,
) -> list[str]:
    return [
        *(source if source is not None else ["--database-url", _ASYNC_URL]),
        "--api-ready-url",
        api_url,
        "--backup-dir",
        str(backup_dir if backup_dir is not None else tmp_path),
        "--max-backup-age-hours",
        "24",
        "--disk-path",
        str(disk_path if disk_path is not None else tmp_path),
        "--min-free-disk-gb",
        "1",
        "--worker-markets",
        "au,sg,us",
        "--max-worker-age-seconds",
        "300",
        "--timeout",
        "30",
        "--api-timeout",
        "5",
    ]


def _ops_replace(args: list[str], flag: str, value: str) -> list[str]:
    index = args.index(flag)
    return [*args[: index + 1], value, *args[index + 2 :]]


def _install_healthy_ops(monkeypatch, tmp_path: Path, cursor: bytes | None = None) -> None:
    _install_ops_psql(monkeypatch, _FakePsql(_ops_db_pass(cursor)))
    _install_ops_api(monkeypatch, _OpsFakeApi())
    _install_ops_disk(monkeypatch, _OpsFakeDisk())
    _write_ops_backup(tmp_path)


def test_operations_check_passes_every_gate_without_secret_leaks(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _install_healthy_ops(monkeypatch, tmp_path)
    monkeypatch.setenv("PGPASSWORD", "ambient-password")
    monkeypatch.setenv("PGHOST", "ambient.invalid")
    monkeypatch.setenv("DATABASE_URL", _ASYNC_URL)

    assert operations.main(_ops_args(tmp_path)) == operations.EXIT_OK

    psql = operations.run_psql
    assert isinstance(psql, _FakePsql)
    assert len(psql.calls) == 3
    for call in psql.calls:
        assert call.command[0] == "psql"
        assert f"--dbname={_SANITIZED_URL}" in call.command
        assert call.env["PGPASSWORD"] == _PASSWORD
        assert call.env["PGPASSFILE"] == os.devnull
        assert "PGHOST" not in call.env
        assert "DATABASE_URL" not in call.env
        assert call.timeout == 30.0
        _assert_no_secret(" ".join(call.command))
    assert "--command=SELECT 1" in psql.calls[0].command
    assert any("alembic_version" in command for command in psql.calls[1].command)
    assert any("after_sales_sync_cursor" in command for command in psql.calls[2].command)

    api = operations.fetch_api_ready
    assert isinstance(api, _OpsFakeApi)
    assert api.calls == [_OpsApiCall(url=_OPS_API_URL, timeout=5.0)]
    disk = operations.disk_usage
    assert isinstance(disk, _OpsFakeDisk)
    assert disk.calls == [tmp_path]

    captured = capsys.readouterr()
    for label in (
        "DATABASE_CONNECTION",
        "ALEMBIC_VERSION",
        "WORKER_CURSOR",
        "API_READY",
        "BACKUP",
        "DISK_SPACE",
    ):
        assert f"PASS {label}" in captured.out
    assert "SUMMARY: passed=6 failed=0" in captured.out
    _assert_no_secret(captured.out + captured.err)
    assert _OPS_API_URL not in captured.out + captured.err


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_operations_uses_protected_url_file_password_only_in_child_env(
    tmp_path: Path, monkeypatch, capsys, mode: int
) -> None:
    encoded_password = "p%40ss%3Aword%2Fsentinel"
    decoded_password = "p@ss:word/sentinel"
    url = f"postgresql+asyncpg://dtc:{encoded_password}@db.example.invalid:5432/dtc_production"
    url_file = _write_url_file(tmp_path, url, mode=mode)
    _install_healthy_ops(monkeypatch, tmp_path)

    code = operations.main(_ops_args(tmp_path, source=["--database-url-file", str(url_file)]))

    assert code == operations.EXIT_OK
    psql = operations.run_psql
    assert isinstance(psql, _FakePsql)
    call = psql.calls[0]
    assert call.env["PGPASSWORD"] == decoded_password
    joined = " ".join(call.command)
    assert decoded_password not in joined
    assert encoded_password not in joined
    assert "postgresql+asyncpg" not in joined

    captured = capsys.readouterr()
    assert decoded_password not in captured.out + captured.err
    assert encoded_password not in captured.out + captured.err
    assert url not in captured.out + captured.err


def test_operations_requires_exactly_one_explicit_url_source(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    psql = _install_ops_psql(monkeypatch, _FakePsql([]))
    api = _install_ops_api(monkeypatch, _OpsFakeApi())
    monkeypatch.setenv("DATABASE_URL", _ASYNC_URL)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"DATABASE_URL={_ASYNC_URL}\n", encoding="utf-8")
    url_file = _write_url_file(tmp_path)

    with pytest.raises(SystemExit) as exc:
        operations.main(_ops_args(tmp_path, source=[]))
    assert exc.value.code == operations.EXIT_INPUT

    with pytest.raises(SystemExit) as exc:
        operations.main(
            _ops_args(
                tmp_path,
                source=[
                    "--database-url",
                    _ASYNC_URL,
                    "--database-url-file",
                    str(url_file),
                ],
            )
        )
    assert exc.value.code == operations.EXIT_INPUT

    assert psql.calls == []
    assert api.calls == []
    captured = capsys.readouterr()
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    ("mode", "text", "expected"),
    [
        (0o644, None, "permissions must be 0400 or 0600, got 0644"),
        (0o600, f"{_ASYNC_URL}\n{_ASYNC_URL}\n", "exactly one line"),
        (0o600, "\n", "is empty"),
    ],
)
def test_operations_rejects_unsafe_database_url_file(
    tmp_path: Path, monkeypatch, capsys, mode: int, text: str | None, expected: str
) -> None:
    url_file = _write_url_file(tmp_path, mode=mode, text=text)
    psql = _install_ops_psql(monkeypatch, _FakePsql([]))
    api = _install_ops_api(monkeypatch, _OpsFakeApi())

    code = operations.main(_ops_args(tmp_path, source=["--database-url-file", str(url_file)]))

    assert code == operations.EXIT_INPUT
    assert psql.calls == []
    assert api.calls == []
    captured = capsys.readouterr()
    assert expected in captured.err
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://api.example.invalid/api/v1/ready", "must use https unless"),
        ("http://user:pass@127.0.0.1/ready", "must not include credentials"),
        ("https://user:pass@api.example.invalid/ready", "must not include credentials"),
        ("http://127.0.0.1/ready?token=secret", "must not include a query"),
        ("http://127.0.0.1/ready#secret", "must not include a fragment"),
        ("ftp://127.0.0.1/ready", "must use http or https"),
        ("https://api.example.invalid", "must include a path"),
        ("http://127.0.0.1:notaport/ready", "is invalid"),
        ("http:///ready", "must include a host"),
        ("http://127.0.0.1/re\nady", "without whitespace"),
    ],
)
def test_operations_rejects_unsafe_api_ready_urls(
    tmp_path: Path, monkeypatch, capsys, url: str, expected: str
) -> None:
    psql = _install_ops_psql(monkeypatch, _FakePsql([]))
    api = _install_ops_api(monkeypatch, _OpsFakeApi())

    code = operations.main(_ops_args(tmp_path, api_url=url))

    assert code == operations.EXIT_INPUT
    assert psql.calls == []
    assert api.calls == []
    captured = capsys.readouterr()
    assert expected in captured.err
    assert url not in captured.out + captured.err


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/api/v1/ready",
        "http://localhost/ready",
        "http://127.0.0.1/ready",
        "http://127.0.0.2:8000/api/v1/ready",
        "http://[::1]:8000/ready",
        "https://api.example.invalid/api/v1/ready",
        "https://localhost/ready",
    ],
)
def test_operations_accepts_safe_api_ready_urls(url: str) -> None:
    assert operations._validate_api_ready_url(url) == url


def test_operations_api_opener_ignores_proxy_environment(monkeypatch) -> None:
    """The explicit readiness URL must never be routed through ambient proxies."""
    proxy_url = "http://proxy.sentinel.invalid:3128"
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(variable, proxy_url)

    # Build under the proxy environment to prove the explicit empty
    # ProxyHandler wins over urllib's environment-derived default. An empty
    # ProxyHandler installs no protocol hook, so build_opener leaves it out
    # entirely and no environment proxy handler is registered.
    for candidate in (operations._build_api_opener(), operations._OPENER):
        assert [
            handler for handler in candidate.handlers if isinstance(handler, ProxyHandler)
        ] == []
        assert proxy_url not in repr(candidate.handlers)


def test_operations_rejects_missing_backup_directory_or_disk_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    psql = _install_ops_psql(monkeypatch, _FakePsql([]))
    api = _install_ops_api(monkeypatch, _OpsFakeApi())
    not_a_directory = tmp_path / "file-not-dir"
    not_a_directory.write_text("x", encoding="utf-8")
    missing_backup = tmp_path / "missing-backup"
    missing_disk = tmp_path / "missing-disk"

    assert operations.main(_ops_args(tmp_path, backup_dir=missing_backup)) == operations.EXIT_INPUT
    assert operations.main(_ops_args(tmp_path, backup_dir=not_a_directory)) == operations.EXIT_INPUT
    assert operations.main(_ops_args(tmp_path, disk_path=missing_disk)) == operations.EXIT_INPUT

    assert psql.calls == []
    assert api.calls == []
    captured = capsys.readouterr()
    assert "--backup-dir does not exist" in captured.err
    assert "--backup-dir is not a directory" in captured.err
    assert "--disk-path does not exist" in captured.err
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    ("markets", "expected"),
    [
        ("", "at least one market"),
        ("au,xx", "invalid worker market(s): xx"),
        ("zz,au", "invalid worker market(s): zz"),
    ],
)
def test_operations_rejects_invalid_worker_markets(
    tmp_path: Path, monkeypatch, capsys, markets: str, expected: str
) -> None:
    psql = _install_ops_psql(monkeypatch, _FakePsql([]))
    api = _install_ops_api(monkeypatch, _OpsFakeApi())
    args = _ops_replace(_ops_args(tmp_path), "--worker-markets", markets)

    assert operations.main(args) == operations.EXIT_INPUT
    assert psql.calls == []
    assert api.calls == []
    captured = capsys.readouterr()
    assert expected in captured.err
    _assert_no_secret(captured.out + captured.err)


def test_operations_worker_markets_are_normalized_and_deduplicated() -> None:
    assert operations._parse_worker_markets(" AU, sg,au ") == ("au", "sg")


@pytest.mark.parametrize(
    ("flag", "value", "expected"),
    [
        ("--max-backup-age-hours", "0", "greater than 0"),
        ("--max-backup-age-hours", "-1", "greater than 0"),
        ("--max-backup-age-hours", "nan", "greater than 0"),
        ("--max-backup-age-hours", "inf", "greater than 0"),
        ("--max-worker-age-seconds", "0", "greater than 0"),
        ("--max-worker-age-seconds", "nan", "greater than 0"),
        ("--min-free-disk-gb", "-1", "at least 0"),
        ("--min-free-disk-gb", "nan", "at least 0"),
        ("--timeout", "0", "between 1 and 86400 seconds"),
        ("--timeout", "inf", "between 1 and 86400 seconds"),
        ("--api-timeout", "0", "between 1 and 300 seconds"),
        ("--api-timeout", "301", "between 1 and 300 seconds"),
    ],
)
def test_operations_rejects_invalid_numeric_thresholds(
    tmp_path: Path, monkeypatch, capsys, flag: str, value: str, expected: str
) -> None:
    psql = _install_ops_psql(monkeypatch, _FakePsql([]))
    api = _install_ops_api(monkeypatch, _OpsFakeApi())

    assert operations.main(_ops_replace(_ops_args(tmp_path), flag, value)) == operations.EXIT_INPUT
    assert psql.calls == []
    assert api.calls == []
    captured = capsys.readouterr()
    assert expected in captured.err
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("missing", "no after_sales_*.dump file found"),
        ("mode", "permissions must be 0600, got 0644"),
        ("empty", "newest backup is empty"),
        ("stale", "older than 1h"),
        ("directory", "not a regular file"),
    ],
)
def test_operations_backup_gate_fails_closed(
    tmp_path: Path, monkeypatch, capsys, scenario: str, expected: str
) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    if scenario == "mode":
        _write_ops_backup(backup_dir, mode=0o644)
    elif scenario == "empty":
        _write_ops_backup(backup_dir, payload=b"")
    elif scenario == "stale":
        _write_ops_backup(backup_dir, age_seconds=7200)
    elif scenario == "directory":
        (backup_dir / "after_sales_20261107T120000Z.dump").mkdir()

    _install_ops_psql(monkeypatch, _FakePsql(_ops_db_pass()))
    _install_ops_api(monkeypatch, _OpsFakeApi())
    _install_ops_disk(monkeypatch, _OpsFakeDisk())
    args = _ops_replace(
        _ops_args(tmp_path, backup_dir=backup_dir),
        "--max-backup-age-hours",
        "1",
    )

    assert operations.main(args) == operations.EXIT_FAILURE

    captured = capsys.readouterr()
    assert "FAIL BACKUP" in captured.out
    assert expected in captured.out
    assert "PASS DATABASE_CONNECTION" in captured.out
    assert "SUMMARY: passed=5 failed=1" in captured.out
    _assert_no_secret(captured.out + captured.err)


def test_operations_backup_gate_uses_only_the_newest_file(tmp_path: Path, monkeypatch) -> None:
    passing_dir = tmp_path / "passing"
    passing_dir.mkdir()
    _write_ops_backup(
        passing_dir,
        name="after_sales_old.dump",
        mode=0o644,
        age_seconds=7200,
    )
    _write_ops_backup(
        passing_dir,
        name="after_sales_new.dump",
        mode=0o600,
        age_seconds=60,
    )
    _install_ops_psql(monkeypatch, _FakePsql(_ops_db_pass()))
    _install_ops_api(monkeypatch, _OpsFakeApi())
    _install_ops_disk(monkeypatch, _OpsFakeDisk())
    assert operations.main(_ops_args(tmp_path, backup_dir=passing_dir)) == operations.EXIT_OK

    failing_dir = tmp_path / "failing"
    failing_dir.mkdir()
    _write_ops_backup(
        failing_dir,
        name="after_sales_old.dump",
        mode=0o600,
        age_seconds=7200,
    )
    _write_ops_backup(
        failing_dir,
        name="after_sales_new.dump",
        mode=0o644,
        age_seconds=60,
    )
    _install_ops_psql(monkeypatch, _FakePsql(_ops_db_pass()))
    assert operations.main(_ops_args(tmp_path, backup_dir=failing_dir)) == operations.EXIT_FAILURE


@pytest.mark.parametrize(
    ("versions", "expected"),
    [
        (b"0004\n", "expected=0005 observed=0004"),
        (b"", "expected=0005 observed=missing"),
        (b"0004\n0005\n", "expected=0005 observed=0004,0005"),
    ],
)
def test_operations_alembic_gate_requires_exact_0005(
    tmp_path: Path, monkeypatch, capsys, versions: bytes, expected: str
) -> None:
    _install_ops_psql(
        monkeypatch,
        _FakePsql(
            [
                _PsqlResponse(stdout=b"1\n"),
                _PsqlResponse(stdout=versions),
                _PsqlResponse(stdout=_ops_cursor_rows()),
            ]
        ),
    )
    _install_ops_api(monkeypatch, _OpsFakeApi())
    _install_ops_disk(monkeypatch, _OpsFakeDisk())
    _write_ops_backup(tmp_path)

    assert operations.main(_ops_args(tmp_path)) == operations.EXIT_FAILURE

    captured = capsys.readouterr()
    assert f"FAIL ALEMBIC_VERSION: {expected}" in captured.out
    assert "PASS WORKER_CURSOR" in captured.out
    assert "SUMMARY: passed=5 failed=1" in captured.out
    _assert_no_secret(captured.out + captured.err)


def test_operations_alembic_query_failure_is_sanitized(tmp_path: Path, monkeypatch, capsys) -> None:
    _install_ops_psql(
        monkeypatch,
        _FakePsql(
            [
                _PsqlResponse(stdout=b"1\n"),
                _PsqlResponse(
                    returncode=2,
                    stderr=f"{_PASSWORD} {_ASYNC_URL}".encode(),
                ),
                _PsqlResponse(stdout=_ops_cursor_rows()),
            ]
        ),
    )
    _install_ops_api(monkeypatch, _OpsFakeApi())
    _install_ops_disk(monkeypatch, _OpsFakeDisk())
    _write_ops_backup(tmp_path)

    assert operations.main(_ops_args(tmp_path)) == operations.EXIT_FAILURE

    captured = capsys.readouterr()
    assert "FAIL ALEMBIC_VERSION: alembic_version check failed (psql status 2)" in captured.out
    assert "PASS WORKER_CURSOR" in captured.out
    _assert_no_secret(captured.out + captured.err)


@pytest.mark.parametrize(
    ("cursor", "expected"),
    [
        (_ops_cursor_rows(markets=("au", "sg")), "missing=us"),
        (_ops_cursor_rows(age_seconds=400), "stale=au,sg,us"),
        (_ops_cursor_rows(null_markets=("sg",)), "stale=sg"),
        (_ops_cursor_rows(overrides={"sg": "not-a-number"}), "unparseable rows"),
    ],
)
def test_operations_worker_cursor_gate_fails_closed(
    tmp_path: Path, monkeypatch, capsys, cursor: bytes, expected: str
) -> None:
    _install_healthy_ops(monkeypatch, tmp_path, cursor)

    assert operations.main(_ops_args(tmp_path)) == operations.EXIT_FAILURE

    captured = capsys.readouterr()
    assert "FAIL WORKER_CURSOR" in captured.out
    assert expected in captured.out
    assert "PASS DATABASE_CONNECTION" in captured.out
    assert "SUMMARY: passed=5 failed=1" in captured.out
    _assert_no_secret(captured.out + captured.err)


def test_operations_worker_cursor_only_requires_requested_markets(
    tmp_path: Path, monkeypatch
) -> None:
    cursor = _ops_cursor_rows(overrides={"sg": str(_OPS_NOW.timestamp() - 7200)})
    _install_healthy_ops(monkeypatch, tmp_path, cursor)
    args = _ops_replace(_ops_args(tmp_path), "--worker-markets", "au")

    assert operations.main(args) == operations.EXIT_OK


@pytest.mark.parametrize(
    ("response", "error", "expected"),
    [
        (
            operations.ApiReadyResponse(status=503, body=b'{"ready": true}'),
            None,
            "HTTP status 503",
        ),
        (
            operations.ApiReadyResponse(status=200, body=b'{"ready": false}'),
            None,
            "JSON ready is not true",
        ),
        (
            operations.ApiReadyResponse(status=200, body=b'{"ready": 1}'),
            None,
            "JSON ready is not true",
        ),
        (
            operations.ApiReadyResponse(status=200, body=b"not-json"),
            None,
            "response is not JSON",
        ),
        (
            operations.ApiReadyResponse(status=200, body=b"[]"),
            None,
            "response is not a JSON object",
        ),
        (
            operations.ApiReadyResponse(
                status=200,
                body=b"x" * (operations.MAX_API_BODY_BYTES + 1),
            ),
            None,
            "response body is too large",
        ),
        (None, TimeoutError(f"timeout {_PASSWORD}"), "request failed: TimeoutError"),
    ],
)
def test_operations_api_gate_fails_closed(
    tmp_path: Path,
    monkeypatch,
    capsys,
    response: operations.ApiReadyResponse | None,
    error: BaseException | None,
    expected: str,
) -> None:
    _install_ops_psql(monkeypatch, _FakePsql(_ops_db_pass()))
    _install_ops_api(monkeypatch, _OpsFakeApi(response=response, error=error))
    _install_ops_disk(monkeypatch, _OpsFakeDisk())
    _write_ops_backup(tmp_path)

    assert operations.main(_ops_args(tmp_path)) == operations.EXIT_FAILURE

    captured = capsys.readouterr()
    assert f"FAIL API_READY: {expected}" in captured.out
    assert "PASS DATABASE_CONNECTION" in captured.out
    assert "SUMMARY: passed=5 failed=1" in captured.out
    _assert_no_secret(captured.out + captured.err)
    assert _OPS_API_URL not in captured.out + captured.err


@pytest.mark.parametrize(
    ("free_gb", "error", "expected"),
    [
        (0.5, None, "free_gb=0.50 required_gb=1"),
        (10.0, OSError(f"disk {_PASSWORD}"), "disk usage unavailable: OSError"),
    ],
)
def test_operations_disk_gate_fails_closed(
    tmp_path: Path,
    monkeypatch,
    capsys,
    free_gb: float,
    error: BaseException | None,
    expected: str,
) -> None:
    _install_ops_psql(monkeypatch, _FakePsql(_ops_db_pass()))
    _install_ops_api(monkeypatch, _OpsFakeApi())
    _install_ops_disk(monkeypatch, _OpsFakeDisk(free_gb=free_gb, error=error))
    _write_ops_backup(tmp_path)

    assert operations.main(_ops_args(tmp_path)) == operations.EXIT_FAILURE

    captured = capsys.readouterr()
    assert f"FAIL DISK_SPACE: {expected}" in captured.out
    assert "PASS BACKUP" in captured.out
    assert "SUMMARY: passed=5 failed=1" in captured.out
    _assert_no_secret(captured.out + captured.err)


def test_operations_failures_never_print_urls_passwords_stderr_or_body(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    body_marker = "response-body-marker"
    _install_ops_psql(
        monkeypatch,
        _FakePsql(
            [
                _PsqlResponse(
                    returncode=2,
                    stderr=f"{_PASSWORD} {_ASYNC_URL} {body_marker}".encode(),
                )
            ]
        ),
    )
    _install_ops_api(
        monkeypatch,
        _OpsFakeApi(
            response=operations.ApiReadyResponse(
                status=503,
                body=f"{body_marker} {_PASSWORD} {_ASYNC_URL}".encode(),
            )
        ),
    )
    _install_ops_disk(monkeypatch, _OpsFakeDisk())
    _write_ops_backup(tmp_path)

    assert operations.main(_ops_args(tmp_path)) == operations.EXIT_FAILURE

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert (
        "FAIL DATABASE_CONNECTION: database connection check failed (psql status 2)" in captured.out
    )
    assert "FAIL API_READY: HTTP status 503" in captured.out
    assert "FAIL ALEMBIC_VERSION: not checked: database unavailable" in captured.out
    assert "FAIL WORKER_CURSOR: not checked: database unavailable" in captured.out
    assert "SUMMARY: passed=2 failed=4" in captured.out
    _assert_no_secret(combined)
    assert _OPS_API_URL not in combined
    assert body_marker not in combined


def test_operations_url_without_password_does_not_reuse_ambient_pgpassword(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PGPASSWORD", "ambient-password")
    psql = _install_ops_psql(monkeypatch, _FakePsql(_ops_db_pass()))
    _install_ops_api(monkeypatch, _OpsFakeApi())
    _install_ops_disk(monkeypatch, _OpsFakeDisk())
    _write_ops_backup(tmp_path)

    code = operations.main(
        _ops_args(
            tmp_path,
            source=[
                "--database-url",
                "postgresql+asyncpg://dtc@db.example.invalid:5432/dtc_production",
            ],
        )
    )

    assert code == operations.EXIT_OK
    call = psql.calls[0]
    assert "PGPASSWORD" not in call.env
    assert call.env["PGPASSFILE"] == os.devnull
