#!/usr/bin/env python3
"""Create a single ``pg_dump --format=custom`` backup of the after-sales DB.

The script is intentionally narrow and explicit:

* the PostgreSQL target comes only from ``--database-url`` or a protected
  (0400/0600) single-line ``--database-url-file``; ``.env`` is never read and
  no URL is taken from the environment;
* the input must be a ``postgresql+asyncpg`` URL. It is rewritten to a libpq
  URL without userinfo password for ``pg_dump``; the decoded password is passed
  through the child ``PGPASSWORD`` environment variable, never through argv;
* the output directory and file name are explicit. The file is created
  exclusively with mode 0600 and an existing path is never overwritten;
* ``pg_dump`` runs with a bounded timeout, and a partial file is removed on
  failure or timeout;
* output and errors never include the database URL, password, token or
  ``pg_dump`` stderr.

Usage::

    uv run python scripts/backup_after_sales_pg.py \\
        --database-url-file /run/secrets/database-url \\
        --output-dir /var/backups/dtc \\
        --output-file after_sales-2026-11-07.dump

Exit codes:
    0  backup created
    1  pg_dump failed, timed out, or produced no usable file
    2  input, permission, output or argument validation refused the run
"""

from __future__ import annotations

import argparse
import math
import os
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INPUT = 2

DEFAULT_TIMEOUT = 900.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 86400.0
_ALLOWED_URL_SCHEME = "postgresql+asyncpg"
_LIBPQ_SCHEME = "postgresql"
_ALLOWED_URL_FILE_MODES = frozenset({0o400, 0o600})
# Only libpq URL parameters that cannot redirect or retarget the connection.
_ALLOWED_QUERY_KEYS = frozenset(
    {
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "sslcrl",
        "connect_timeout",
        "application_name",
        "channel_binding",
    }
)
_SCRUBBED_ENV_EXACT = frozenset({"DATABASE_URL", "ALEMBIC_DATABASE_URL", "DTC_TEST_PG_URL"})
_OUTPUT_NAME_FORBIDDEN = frozenset({".", ".."})


class BackupInputError(ValueError):
    """An explicit input, output or argument is unsafe or invalid."""


class BackupError(RuntimeError):
    """A sanitized runtime failure while executing pg_dump."""


@dataclass(frozen=True)
class DatabaseTarget:
    """libpq-safe target plus the password kept out of argv and repr."""

    libpq_url: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class BackupConfig:
    target: DatabaseTarget
    output_path: Path
    timeout: float


def _has_control_characters(value: str) -> bool:
    """Return True for NUL, C0 controls and DEL/C1 controls."""
    return any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)


def parse_database_url(raw: str) -> DatabaseTarget:
    """Validate ``postgresql+asyncpg`` input and build a password-free libpq URL."""
    text = raw.strip()
    if not text:
        raise BackupInputError("database URL is empty")
    if any(char.isspace() for char in text) or "\x00" in text:
        raise BackupInputError("database URL must be a single line without whitespace")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise BackupInputError("database URL is invalid") from exc

    if parts.scheme.lower() != _ALLOWED_URL_SCHEME:
        raise BackupInputError("database URL must use the postgresql+asyncpg scheme")
    host = parts.hostname
    if not host:
        raise BackupInputError("database URL must include a host")
    if parts.fragment:
        raise BackupInputError("database URL must not include a fragment")

    database_path = parts.path.lstrip("/")
    if not database_path or "/" in database_path:
        raise BackupInputError("database URL must include exactly one database name")
    database = unquote(database_path)
    if not database or "/" in database or "\x00" in database:
        raise BackupInputError("database URL database name is invalid")

    username = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if "\x00" in username or "\x00" in password:
        raise BackupInputError("database URL credentials are invalid")

    query_pairs: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _has_control_characters(key) or _has_control_characters(value):
            raise BackupInputError(
                "database URL query parameters must not contain control characters"
            )
        if not key:
            raise BackupInputError("database URL query parameters must not have empty names")
        if key not in _ALLOWED_QUERY_KEYS:
            raise BackupInputError(
                "database URL query parameter is not allowed; only parameters that "
                "cannot override the connection target are accepted"
            )
        if key in seen_keys:
            raise BackupInputError("database URL must not repeat query parameters")
        seen_keys.add(key)
        query_pairs.append((key, value))

    query = "&".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in query_pairs)

    netloc_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    userinfo = f"{quote(username, safe='')}@" if username else ""
    netloc = f"{userinfo}{netloc_host}"
    if port is not None:
        netloc = f"{netloc}:{port}"

    libpq_url = urlunsplit(
        (
            _LIBPQ_SCHEME,
            netloc,
            "/" + quote(database, safe=""),
            query,
            "",
        )
    )
    return DatabaseTarget(libpq_url=libpq_url, password=password)


def load_database_url_file(path: Path) -> str:
    """Read one URL from an explicitly supplied 0400/0600 regular file."""
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise BackupInputError("database URL file does not exist") from exc
    except OSError as exc:
        raise BackupInputError(
            f"cannot stat database URL file: {exc.strerror or type(exc).__name__}"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise BackupInputError("database URL file must be a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode not in _ALLOWED_URL_FILE_MODES:
        raise BackupInputError(
            f"database URL file permissions must be 0400 or 0600, got {mode:04o}"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BackupInputError("cannot read database URL file as UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != 1:
        raise BackupInputError("database URL file must contain exactly one line")
    url = lines[0].strip()
    if not url:
        raise BackupInputError("database URL file is empty")
    return url


def _default_output_file(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return f"after_sales_{moment.strftime('%Y%m%dT%H%M%SZ')}.dump"


def _validate_output_name(name: str) -> str:
    if (
        not name
        or name in _OUTPUT_NAME_FORBIDDEN
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or Path(name).name != name
    ):
        raise BackupInputError("output file must be a single file name inside the output directory")
    return name


def _build_config(args: argparse.Namespace) -> BackupConfig:
    if args.database_url_file is not None:
        raw_url = load_database_url_file(args.database_url_file)
    else:
        raw_url = args.database_url or ""
    target = parse_database_url(raw_url)

    if not math.isfinite(args.timeout) or not MIN_TIMEOUT <= args.timeout <= MAX_TIMEOUT:
        raise BackupInputError(
            f"--timeout must be between {MIN_TIMEOUT:g} and {MAX_TIMEOUT:g} seconds"
        )

    output_dir = args.output_dir
    if not output_dir.exists():
        raise BackupInputError("output directory does not exist")
    if not output_dir.is_dir():
        raise BackupInputError("output directory is not a directory")

    output_name = (
        _default_output_file()
        if args.output_file is None
        else _validate_output_name(args.output_file)
    )
    output_path = output_dir / output_name
    if output_path.parent != output_dir:
        raise BackupInputError("output file must stay inside the output directory")

    return BackupConfig(target=target, output_path=output_path, timeout=args.timeout)


def _create_output_file(path: Path) -> int:
    """Create a brand-new 0600 regular file, refusing any existing path."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BackupInputError("output file already exists; refusing to overwrite") from exc
    except OSError as exc:
        raise BackupInputError(
            f"cannot create output file: {exc.strerror or type(exc).__name__}"
        ) from exc
    try:
        # os.open() applies the process umask; force the required mode.
        os.fchmod(descriptor, 0o600)
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise BackupInputError("cannot set output file permissions to 0600") from exc
    return descriptor


def _pg_dump_env(password: str) -> dict[str, str]:
    """Pass the password only through the child environment.

    Ambient ``PG*`` target/credential variables, including ``PGPASSWORD`` and
    ``PGPASSFILE``, are removed so the explicit URL remains the sole target.
    """
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
    for key in _SCRUBBED_ENV_EXACT:
        env.pop(key, None)
    env["PGPASSFILE"] = os.devnull
    if password:
        env["PGPASSWORD"] = password
    return env


def run_pg_dump(
    command: list[str],
    *,
    env: dict[str, str],
    stdout: BinaryIO,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Subprocess seam; unit tests replace this and never start pg_dump."""
    return subprocess.run(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _execute_backup(config: BackupConfig) -> int:
    command = [
        "pg_dump",
        "--format=custom",
        "--no-password",
        f"--dbname={config.target.libpq_url}",
    ]
    descriptor = _create_output_file(config.output_path)
    completed = False
    try:
        try:
            output = os.fdopen(descriptor, "wb")
        except OSError:
            os.close(descriptor)
            raise
        with output:
            result = run_pg_dump(
                command,
                env=_pg_dump_env(config.target.password),
                stdout=output,
                timeout=config.timeout,
            )
        if result.returncode != 0:
            raise BackupError(f"pg_dump exited with status {result.returncode}")
        size = config.output_path.stat().st_size
        if size == 0:
            raise BackupError("pg_dump produced an empty file")
        completed = True
        return size
    except subprocess.TimeoutExpired as exc:
        raise BackupError("pg_dump timed out") from exc
    except FileNotFoundError as exc:
        raise BackupError("pg_dump executable was not found") from exc
    except OSError as exc:
        # Never forward subprocess/path details that could contain the target.
        raise BackupError("could not run pg_dump") from exc
    finally:
        if not completed:
            try:
                config.output_path.unlink(missing_ok=True)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a new 0600 pg_dump --format=custom after-sales backup."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database-url", help="explicit postgresql+asyncpg database URL")
    source.add_argument(
        "--database-url-file",
        type=Path,
        help="explicit single-line URL file with mode 0400 or 0600",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="existing directory that receives the new backup",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        help="new file name inside --output-dir (default: timestamped .dump)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"pg_dump timeout in seconds ({MIN_TIMEOUT:g}-{MAX_TIMEOUT:g})",
    )
    args = parser.parse_args(argv)

    try:
        config = _build_config(args)
    except BackupInputError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return EXIT_INPUT

    try:
        size = _execute_backup(config)
    except BackupInputError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - unexpected details are never printed
        print(f"backup failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILURE

    print(f"Backup completed: {config.output_path} ({size} bytes).")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
