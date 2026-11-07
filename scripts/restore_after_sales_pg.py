#!/usr/bin/env python3
"""Restore one ``pg_dump --format=custom`` backup into an explicit empty target.

The script is intentionally narrow and fail-closed:

* the target comes only from ``--database-url`` or a protected (0400/0600)
  single-line ``--database-url-file``. The target database must already exist;
  this script never creates a database and never reads ``.env`` or the ambient
  environment for a URL;
* ``--backup-file`` must be a regular 0600 file whose content starts with the
  PostgreSQL custom-format magic. ``--confirm-worker-stopped`` is mandatory;
  without it the run is refused before any subprocess starts;
* before restoring, the target is queried through ``psql`` and any non-system
  table is treated as a non-empty target. ``pg_restore`` is not started in that
  case;
* ``pg_restore`` runs with ``--no-owner --no-privileges --exit-on-error
  --single-transaction`` into the pre-created empty database;
* after restoring, ``alembic_version`` must contain exactly the expected
  revision (``0005`` by default) and every required after-sales table must be
  present;
* success and failure output contains only table names/counts, revision values
  and exit codes. The database URL, password and child stderr are never
  printed. The password is passed only through ``PGPASSWORD`` in a sanitized
  child environment, never through argv;
* API and Worker are never started by this script. After a restore they must
  stay stopped until an operator completes reconciliation.

Usage::

    uv run python scripts/restore_after_sales_pg.py \\
        --database-url-file /run/secrets/database-url \\
        --backup-file /var/backups/dtc/after_sales-2026-11-07.dump \\
        --confirm-worker-stopped

Exit codes:
    0  restore completed and the restored schema passed verification
    1  psql/pg_restore failed, timed out, or schema verification failed
    2  input, permission, confirmation or empty-target validation refused the run
"""

from __future__ import annotations

import argparse
import math
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backup_after_sales_pg import (  # noqa: E402
    BackupInputError,
    DatabaseTarget,
    _pg_dump_env as _sanitized_child_env,
    load_database_url_file,
    parse_database_url,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INPUT = 2

DEFAULT_TIMEOUT = 3600.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 86400.0
DEFAULT_EXPECTED_VERSION = "0005"
_BACKUP_FILE_MODE = 0o600
_CUSTOM_DUMP_MAGIC = b"PGDMP"
_MAX_LABEL_LENGTH = 128

# Every table created by migrations 0003-0005. Core 0001/0002 tables are
# checked indirectly through the same query only when listing the schema; the
# after-sales workflow cannot be verified without these tables.
REQUIRED_AFTER_SALES_TABLES = frozenset(
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

_USER_TABLE_QUERY = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
  AND n.nspname NOT LIKE 'pg_temp%'
ORDER BY c.relname
""".strip()
_ALEMBIC_VERSION_QUERY = "SELECT version_num FROM alembic_version"


class RestoreInputError(ValueError):
    """An explicit input, permission or argument is unsafe or invalid."""


class RestoreError(RuntimeError):
    """A sanitized runtime failure while querying or restoring the target."""


@dataclass(frozen=True)
class RestoreConfig:
    target: DatabaseTarget = field(repr=False)
    backup_path: Path
    expected_version: str
    timeout: float


def _safe_label(value: str) -> str:
    """Keep database-derived text printable and bounded before reporting it."""
    cleaned = "".join(char if char.isprintable() else "?" for char in value)
    return cleaned[:_MAX_LABEL_LENGTH]


def _validate_expected_version(value: str) -> str:
    text = value.strip()
    if not text:
        raise RestoreInputError("--expected-version must not be empty")
    if not all(char.isalnum() or char in "_-" for char in text):
        raise RestoreInputError("--expected-version must contain only letters, digits, '_' or '-'")
    return text


def _validate_backup_file(path: Path) -> Path:
    """Require a regular 0600 file that starts with the custom-format magic."""
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RestoreInputError("backup file does not exist") from exc
    except OSError as exc:
        raise RestoreInputError(
            f"cannot stat backup file: {exc.strerror or type(exc).__name__}"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise RestoreInputError("backup file must be a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode != _BACKUP_FILE_MODE:
        raise RestoreInputError(f"backup file permissions must be 0600, got {mode:04o}")
    try:
        with path.open("rb") as handle:
            magic = handle.read(len(_CUSTOM_DUMP_MAGIC))
    except OSError as exc:
        raise RestoreInputError(
            f"cannot read backup file: {exc.strerror or type(exc).__name__}"
        ) from exc
    if magic != _CUSTOM_DUMP_MAGIC:
        raise RestoreInputError(
            "backup file is not a PostgreSQL custom-format dump (PGDMP magic missing)"
        )
    return path


def _build_config(args: argparse.Namespace) -> RestoreConfig:
    if not args.confirm_worker_stopped:
        raise RestoreInputError("--confirm-worker-stopped is required; stop API and Worker first")

    try:
        if args.database_url_file is not None:
            raw_url = load_database_url_file(args.database_url_file)
        else:
            raw_url = args.database_url or ""
        target = parse_database_url(raw_url)
    except BackupInputError as exc:
        raise RestoreInputError(str(exc)) from exc

    if not math.isfinite(args.timeout) or not MIN_TIMEOUT <= args.timeout <= MAX_TIMEOUT:
        raise RestoreInputError(
            f"--timeout must be between {MIN_TIMEOUT:g} and {MAX_TIMEOUT:g} seconds"
        )

    expected_version = _validate_expected_version(args.expected_version)
    backup_path = _validate_backup_file(args.backup_file)
    return RestoreConfig(
        target=target,
        backup_path=backup_path,
        expected_version=expected_version,
        timeout=args.timeout,
    )


def _psql_command(target: DatabaseTarget, sql: str) -> list[str]:
    return [
        "psql",
        "--no-password",
        "--no-psqlrc",
        "--set=ON_ERROR_STOP=1",
        "--tuples-only",
        "--no-align",
        f"--dbname={target.libpq_url}",
        f"--command={sql}",
    ]


def run_psql(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Subprocess seam; unit tests replace this and never start psql."""
    return subprocess.run(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def run_pg_restore(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Subprocess seam; unit tests replace this and never start pg_restore."""
    return subprocess.run(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _decode_lines(raw: bytes) -> list[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RestoreError("psql returned non-UTF-8 output") from exc
    lines: list[str] = []
    for line in text.splitlines():
        value = line.strip()
        if value:
            lines.append(_safe_label(value))
    return lines


def _run_psql_query(config: RestoreConfig, sql: str, *, stage: str) -> list[str]:
    try:
        result = run_psql(
            _psql_command(config.target, sql),
            env=_sanitized_child_env(config.target.password),
            timeout=config.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RestoreError(f"{stage} timed out") from exc
    except FileNotFoundError as exc:
        raise RestoreError("psql executable was not found") from exc
    except OSError as exc:
        raise RestoreError("could not run psql") from exc
    if result.returncode != 0:
        raise RestoreError(f"{stage} failed (psql exited with status {result.returncode})")
    return _decode_lines(result.stdout)


def _assert_empty_target(config: RestoreConfig) -> None:
    tables = _run_psql_query(config, _USER_TABLE_QUERY, stage="pre-restore target check")
    if tables:
        names = ", ".join(tables)
        raise RestoreInputError(
            f"target database is not empty: {len(tables)} non-system table(s) present: {names}"
        )


def _run_restore(config: RestoreConfig) -> None:
    command = [
        "pg_restore",
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--single-transaction",
        "--no-password",
        f"--dbname={config.target.libpq_url}",
        str(config.backup_path),
    ]
    try:
        result = run_pg_restore(
            command,
            env=_sanitized_child_env(config.target.password),
            timeout=config.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RestoreError("pg_restore timed out") from exc
    except FileNotFoundError as exc:
        raise RestoreError("pg_restore executable was not found") from exc
    except OSError as exc:
        raise RestoreError("could not run pg_restore") from exc
    if result.returncode != 0:
        raise RestoreError(f"pg_restore exited with status {result.returncode}")


def _verify_restored_schema(config: RestoreConfig) -> tuple[str, list[str]]:
    versions = _run_psql_query(
        config,
        _ALEMBIC_VERSION_QUERY,
        stage="post-restore alembic_version check",
    )
    if versions != [config.expected_version]:
        observed = ", ".join(versions) if versions else "missing"
        raise RestoreError(f"alembic_version is {observed}; expected {config.expected_version}")

    tables = _run_psql_query(
        config,
        _USER_TABLE_QUERY,
        stage="post-restore table check",
    )
    missing = sorted(REQUIRED_AFTER_SALES_TABLES - set(tables))
    if missing:
        raise RestoreError(
            f"restored schema is missing required after-sales table(s): {', '.join(missing)}"
        )
    return config.expected_version, sorted(tables)


def _execute_restore(config: RestoreConfig) -> tuple[str, list[str]]:
    _assert_empty_target(config)
    _run_restore(config)
    return _verify_restored_schema(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Restore a 0600 pg_dump custom backup into an existing empty "
            "PostgreSQL target; never starts API or Worker."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database-url", help="explicit postgresql+asyncpg database URL")
    source.add_argument(
        "--database-url-file",
        type=Path,
        help="explicit single-line URL file with mode 0400 or 0600",
    )
    parser.add_argument(
        "--backup-file",
        required=True,
        type=Path,
        help="existing regular 0600 pg_dump --format=custom file",
    )
    parser.add_argument(
        "--confirm-worker-stopped",
        action="store_true",
        help="confirm API and Worker are stopped before restoring",
    )
    parser.add_argument(
        "--expected-version",
        default=DEFAULT_EXPECTED_VERSION,
        help=f"exact alembic_version required after restore (default: {DEFAULT_EXPECTED_VERSION})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-subprocess timeout in seconds ({MIN_TIMEOUT:g}-{MAX_TIMEOUT:g})",
    )
    args = parser.parse_args(argv)

    try:
        config = _build_config(args)
    except RestoreInputError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return EXIT_INPUT

    try:
        version, tables = _execute_restore(config)
    except RestoreInputError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except RestoreError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - unexpected details are never printed
        print(f"restore failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILURE

    print(f"Restore completed: alembic_version={version} tables={len(tables)}")
    if tables:
        print(f"Restored tables: {', '.join(tables)}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
