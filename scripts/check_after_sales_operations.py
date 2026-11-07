#!/usr/bin/env python3
"""Read-only production operations checks for the after-sales stack.

The script verifies, in one bounded run, the P7 deployment gates:

* database connectivity over an explicit PostgreSQL URL;
* ``alembic_version`` is exactly ``0005``;
* every requested Worker market has a fresh
  ``after_sales_sync_cursor.last_success_at``. The Worker advances this row
  after each successful market poll, including a poll that returned an empty
  page, so a fresh row is the per-market polling heartbeat;
* the API ``/ready`` endpoint returns HTTP 200 with JSON ``ready=true``;
* the newest ``after_sales_*.dump`` in an explicit directory is a non-empty
  regular 0600 file no older than ``--max-backup-age-hours``;
* the explicit disk path has at least ``--min-free-disk-gb`` free.

The target, API URL, directories and thresholds are always explicit. No
``.env``, ambient ``DATABASE_URL``/``PG*`` variable, settings object or cloud
API is consulted. Nothing is written and no resource is created.

Output contains only stable ``PASS``/``FAIL`` labels, a summary and sanitized
details. Database URLs, passwords, child stderr and HTTP bodies are never
printed. The database password is passed only through the sanitized child
``PGPASSWORD`` environment variable, never through argv.

Usage::

    uv run python scripts/check_after_sales_operations.py \\
        --database-url-file /run/secrets/database-url \\
        --api-ready-url http://127.0.0.1:8000/api/v1/ready \\
        --backup-dir /var/backups/dtc \\
        --max-backup-age-hours 26 \\
        --disk-path /var/backups/dtc \\
        --min-free-disk-gb 10 \\
        --worker-markets au,sg,us \\
        --max-worker-age-seconds 300

Exit codes:
    0  every check passed
    1  one or more checks failed
    2  input, permission or argument validation refused the run
"""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import json
import math
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas.after_sales import AFTER_SALES_MARKETS  # noqa: E402
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

EXPECTED_ALEMBIC_VERSION = "0005"
DEFAULT_TIMEOUT = 30.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 86400.0
DEFAULT_API_TIMEOUT = 10.0
MAX_API_TIMEOUT = 300.0
MAX_API_BODY_BYTES = 64 * 1024
BACKUP_NAME_PATTERN = "after_sales_*.dump"
_BACKUP_MODE = 0o600
_BYTES_PER_GIB = 1024**3
_MAX_LABEL_LENGTH = 128

_CONNECTIVITY_QUERY = "SELECT 1"
_ALEMBIC_VERSION_QUERY = "SELECT version_num FROM alembic_version"
_SYNC_CURSOR_QUERY = (
    "SELECT market, COALESCE(EXTRACT(EPOCH FROM last_success_at)::text, '') "
    "FROM after_sales_sync_cursor ORDER BY market"
)


class OperationsInputError(ValueError):
    """An explicit input, permission or argument is unsafe or invalid."""


class CheckError(RuntimeError):
    """A sanitized runtime failure while executing one check."""


@dataclass(frozen=True)
class OperationsConfig:
    target: DatabaseTarget = field(repr=False)
    api_ready_url: str = field(repr=False)
    backup_dir: Path
    max_backup_age_hours: float
    disk_path: Path
    min_free_disk_gb: float
    worker_markets: tuple[str, ...]
    max_worker_age_seconds: float
    timeout: float
    api_timeout: float


@dataclass(frozen=True)
class CheckResult:
    label: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ApiReadyResponse:
    status: int
    body: bytes


@dataclass(frozen=True)
class DiskUsage:
    total: int
    used: int
    free: int


def _has_control_characters(value: str) -> bool:
    return any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)


def _safe_label(value: str) -> str:
    """Keep database-derived text printable and bounded before reporting it."""
    cleaned = "".join(char if char.isprintable() else "?" for char in value)
    return cleaned[:_MAX_LABEL_LENGTH]


def _is_loopback_host(host: str) -> bool:
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _validate_api_ready_url(raw: str) -> str:
    """Accept an explicit https URL, or http only for a loopback host."""
    text = raw.strip()
    if (
        not text
        or any(char.isspace() for char in text)
        or "\x00" in text
        or _has_control_characters(text)
    ):
        raise OperationsInputError("--api-ready-url must be a single URL without whitespace")
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - validates a malformed or out-of-range port
    except ValueError as exc:
        raise OperationsInputError("--api-ready-url is invalid") from exc

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise OperationsInputError("--api-ready-url must use http or https")
    if parts.username is not None or parts.password is not None:
        raise OperationsInputError("--api-ready-url must not include credentials")
    if parts.query:
        raise OperationsInputError("--api-ready-url must not include a query")
    if parts.fragment:
        raise OperationsInputError("--api-ready-url must not include a fragment")

    host = parts.hostname
    if not host:
        raise OperationsInputError("--api-ready-url must include a host")
    if scheme == "http" and not _is_loopback_host(host):
        raise OperationsInputError("--api-ready-url must use https unless the host is loopback")
    if not parts.path:
        raise OperationsInputError("--api-ready-url must include a path")
    return text


def _parse_worker_markets(raw: str) -> tuple[str, ...]:
    values = sorted({value.strip().lower() for value in raw.split(",") if value.strip()})
    if not values:
        raise OperationsInputError("--worker-markets must contain at least one market")
    invalid = sorted(set(values) - set(AFTER_SALES_MARKETS))
    if invalid:
        raise OperationsInputError(f"invalid worker market(s): {', '.join(invalid)}")
    return tuple(values)


def _require_directory(path: Path, flag: str) -> None:
    if not path.exists():
        raise OperationsInputError(f"{flag} does not exist")
    if not path.is_dir():
        raise OperationsInputError(f"{flag} is not a directory")


def _require_positive_finite(value: float, flag: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise OperationsInputError(f"{flag} must be a finite value greater than 0")
    return value


def _require_nonnegative_finite(value: float, flag: str) -> float:
    if not math.isfinite(value) or value < 0:
        raise OperationsInputError(f"{flag} must be a finite value of at least 0")
    return value


def _require_timeout(value: float, flag: str, maximum: float) -> float:
    if not math.isfinite(value) or not MIN_TIMEOUT <= value <= maximum:
        raise OperationsInputError(
            f"{flag} must be between {MIN_TIMEOUT:g} and {maximum:g} seconds"
        )
    return value


def _build_config(args: argparse.Namespace) -> OperationsConfig:
    try:
        if args.database_url_file is not None:
            raw_url = load_database_url_file(args.database_url_file)
        else:
            raw_url = args.database_url or ""
        target = parse_database_url(raw_url)
    except BackupInputError as exc:
        raise OperationsInputError(str(exc)) from exc

    api_ready_url = _validate_api_ready_url(args.api_ready_url)
    _require_directory(args.backup_dir, "--backup-dir")
    if not args.disk_path.exists():
        raise OperationsInputError("--disk-path does not exist")

    return OperationsConfig(
        target=target,
        api_ready_url=api_ready_url,
        backup_dir=args.backup_dir,
        max_backup_age_hours=_require_positive_finite(
            args.max_backup_age_hours, "--max-backup-age-hours"
        ),
        disk_path=args.disk_path,
        min_free_disk_gb=_require_nonnegative_finite(args.min_free_disk_gb, "--min-free-disk-gb"),
        worker_markets=_parse_worker_markets(args.worker_markets),
        max_worker_age_seconds=_require_positive_finite(
            args.max_worker_age_seconds, "--max-worker-age-seconds"
        ),
        timeout=_require_timeout(args.timeout, "--timeout", MAX_TIMEOUT),
        api_timeout=_require_timeout(args.api_timeout, "--api-timeout", MAX_API_TIMEOUT),
    )


def now_utc() -> datetime:
    """Clock seam; tests freeze it instead of depending on wall time."""
    return datetime.now(UTC)


def disk_usage(path: Path) -> DiskUsage:
    """Filesystem seam around :func:`shutil.disk_usage`."""
    usage = shutil.disk_usage(path)
    return DiskUsage(total=usage.total, used=usage.used, free=usage.free)


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


class _NoRedirectHandler(HTTPRedirectHandler):
    """Fail closed instead of following a redirect to another host."""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _build_api_opener() -> OpenerDirector:
    """Build an opener that never follows redirects or uses proxy settings.

    ``ProxyHandler({})`` is deliberately explicit: with no handler argument,
    urllib would read ambient ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY``
    variables and could route the readiness request through a proxy. The
    readiness URL is explicit, so environment proxies must never apply.
    """
    return build_opener(_NoRedirectHandler(), ProxyHandler({}))


_OPENER = _build_api_opener()


def fetch_api_ready(url: str, *, timeout: float) -> ApiReadyResponse:
    """HTTP seam; unit tests replace this and never contact the API."""
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    with _OPENER.open(request, timeout=timeout) as response:
        body = response.read(MAX_API_BODY_BYTES + 1)
        return ApiReadyResponse(status=int(response.status), body=body)


def _psql_command(target: DatabaseTarget, sql: str) -> list[str]:
    return [
        "psql",
        "--no-password",
        "--no-psqlrc",
        "--set=ON_ERROR_STOP=1",
        "--tuples-only",
        "--no-align",
        "--field-separator=|",
        f"--dbname={target.libpq_url}",
        f"--command={sql}",
    ]


def _decode_lines(raw: bytes, *, stage: str) -> list[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckError(f"{stage} returned invalid UTF-8") from exc
    lines: list[str] = []
    for line in text.splitlines():
        value = line.strip()
        if value:
            lines.append(_safe_label(value))
    return lines


def _run_query(config: OperationsConfig, sql: str, *, stage: str) -> list[str]:
    try:
        result = run_psql(
            _psql_command(config.target, sql),
            env=_sanitized_child_env(config.target.password),
            timeout=config.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"{stage} timed out") from exc
    except FileNotFoundError as exc:
        raise CheckError(f"{stage} failed: psql was not found") from exc
    except OSError as exc:
        raise CheckError(f"{stage} failed: could not run psql") from exc
    if result.returncode != 0:
        raise CheckError(f"{stage} failed (psql status {result.returncode})")
    return _decode_lines(result.stdout, stage=stage)


def _check_alembic_version(config: OperationsConfig) -> CheckResult:
    try:
        versions = _run_query(
            config,
            _ALEMBIC_VERSION_QUERY,
            stage="alembic_version check",
        )
    except CheckError as exc:
        return CheckResult("ALEMBIC_VERSION", False, str(exc))

    if versions == [EXPECTED_ALEMBIC_VERSION]:
        return CheckResult("ALEMBIC_VERSION", True, f"version={EXPECTED_ALEMBIC_VERSION}")
    observed = ",".join(versions) if versions else "missing"
    return CheckResult(
        "ALEMBIC_VERSION",
        False,
        f"expected={EXPECTED_ALEMBIC_VERSION} observed={observed}",
    )


def _check_worker_cursor(config: OperationsConfig, now: datetime) -> CheckResult:
    try:
        rows = _run_query(
            config,
            _SYNC_CURSOR_QUERY,
            stage="worker cursor check",
        )
    except CheckError as exc:
        return CheckResult("WORKER_CURSOR", False, str(exc))

    observed: dict[str, float | None] = {}
    unparseable = False
    for row in rows:
        market, separator, value = row.partition("|")
        if not separator or not market.strip():
            unparseable = True
            continue
        market = market.strip()
        value = value.strip()
        if not value:
            observed[market] = None
            continue
        try:
            epoch = float(value)
        except ValueError:
            unparseable = True
            continue
        if not math.isfinite(epoch):
            unparseable = True
            continue
        observed[market] = epoch

    now_epoch = now.timestamp()
    missing = [market for market in config.worker_markets if market not in observed]
    stale: list[str] = []
    for market in config.worker_markets:
        if market not in observed:
            continue
        last_success = observed[market]
        if last_success is None or now_epoch - last_success > config.max_worker_age_seconds:
            stale.append(market)

    details: list[str] = []
    if missing:
        details.append(f"missing={','.join(missing)}")
    if stale:
        details.append(f"stale={','.join(stale)}")
    if unparseable:
        details.append("unparseable rows")
    if details:
        return CheckResult("WORKER_CURSOR", False, " ".join(details))
    return CheckResult(
        "WORKER_CURSOR",
        True,
        f"markets={','.join(config.worker_markets)}",
    )


def _check_database(config: OperationsConfig, now: datetime) -> list[CheckResult]:
    try:
        connectivity = _run_query(
            config,
            _CONNECTIVITY_QUERY,
            stage="database connection check",
        )
    except CheckError as exc:
        reason = str(exc)
        return [
            CheckResult("DATABASE_CONNECTION", False, reason),
            CheckResult("ALEMBIC_VERSION", False, "not checked: database unavailable"),
            CheckResult("WORKER_CURSOR", False, "not checked: database unavailable"),
        ]

    if connectivity != ["1"]:
        return [
            CheckResult("DATABASE_CONNECTION", False, "unexpected response"),
            CheckResult("ALEMBIC_VERSION", False, "not checked: database unavailable"),
            CheckResult("WORKER_CURSOR", False, "not checked: database unavailable"),
        ]

    return [
        CheckResult("DATABASE_CONNECTION", True),
        _check_alembic_version(config),
        _check_worker_cursor(config, now),
    ]


def _latest_backup(backup_dir: Path) -> tuple[Path, os.stat_result] | None:
    candidates: list[tuple[Path, os.stat_result]] = []
    for entry in backup_dir.iterdir():
        if not fnmatch.fnmatchcase(entry.name, BACKUP_NAME_PATTERN):
            continue
        try:
            info = entry.lstat()
        except OSError:
            continue
        candidates.append((entry, info))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1].st_mtime)


def _check_backup(config: OperationsConfig, now: datetime) -> CheckResult:
    try:
        latest = _latest_backup(config.backup_dir)
    except OSError:
        return CheckResult("BACKUP", False, "backup directory is unreadable")
    if latest is None:
        return CheckResult("BACKUP", False, f"no {BACKUP_NAME_PATTERN} file found")

    _path, info = latest
    if not stat.S_ISREG(info.st_mode):
        return CheckResult("BACKUP", False, "newest backup is not a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode != _BACKUP_MODE:
        return CheckResult(
            "BACKUP",
            False,
            f"newest backup permissions must be 0600, got {mode:04o}",
        )
    if info.st_size <= 0:
        return CheckResult("BACKUP", False, "newest backup is empty")

    age_seconds = now.timestamp() - info.st_mtime
    max_age_seconds = config.max_backup_age_hours * 3600
    if age_seconds > max_age_seconds:
        return CheckResult(
            "BACKUP",
            False,
            f"newest backup is older than {config.max_backup_age_hours:g}h",
        )
    return CheckResult(
        "BACKUP",
        True,
        f"age_seconds={max(0, int(age_seconds))}",
    )


def _check_disk(config: OperationsConfig) -> CheckResult:
    try:
        usage = disk_usage(config.disk_path)
    except OSError as exc:
        return CheckResult("DISK_SPACE", False, f"disk usage unavailable: {type(exc).__name__}")

    required = config.min_free_disk_gb * _BYTES_PER_GIB
    free_gb = usage.free / _BYTES_PER_GIB
    if usage.free < required:
        return CheckResult(
            "DISK_SPACE",
            False,
            f"free_gb={free_gb:.2f} required_gb={config.min_free_disk_gb:g}",
        )
    return CheckResult(
        "DISK_SPACE",
        True,
        f"free_gb={free_gb:.2f} required_gb={config.min_free_disk_gb:g}",
    )


def _check_api(config: OperationsConfig) -> CheckResult:
    try:
        response = fetch_api_ready(config.api_ready_url, timeout=config.api_timeout)
    except HTTPError as exc:
        return CheckResult("API_READY", False, f"HTTP status {exc.code}")
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return CheckResult("API_READY", False, f"request failed: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - exception text could contain the URL
        return CheckResult("API_READY", False, f"request failed: {type(exc).__name__}")

    if response.status != 200:
        return CheckResult("API_READY", False, f"HTTP status {response.status}")
    if len(response.body) > MAX_API_BODY_BYTES:
        return CheckResult("API_READY", False, "response body is too large")
    try:
        payload = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return CheckResult("API_READY", False, "response is not JSON")
    if not isinstance(payload, dict):
        return CheckResult("API_READY", False, "response is not a JSON object")
    if payload.get("ready") is not True:
        return CheckResult("API_READY", False, "JSON ready is not true")
    return CheckResult("API_READY", True)


def run_checks(config: OperationsConfig) -> list[CheckResult]:
    """Run every check once against one frozen clock reading."""
    now = now_utc()
    results = _check_database(config, now)
    results.append(_check_api(config))
    results.append(_check_backup(config, now))
    results.append(_check_disk(config))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only after-sales operations checks: database, migration, "
            "Worker cursor, API readiness, backup age and free disk."
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
        "--api-ready-url",
        required=True,
        help="explicit /ready URL: https, or http only for a loopback host",
    )
    parser.add_argument(
        "--backup-dir",
        required=True,
        type=Path,
        help="existing directory containing after_sales_*.dump backups",
    )
    parser.add_argument(
        "--max-backup-age-hours",
        required=True,
        type=float,
        help="maximum acceptable age of the newest backup",
    )
    parser.add_argument(
        "--disk-path",
        required=True,
        type=Path,
        help="existing path whose filesystem free space is checked",
    )
    parser.add_argument(
        "--min-free-disk-gb",
        required=True,
        type=float,
        help="minimum free space in GiB",
    )
    parser.add_argument(
        "--worker-markets",
        required=True,
        help="comma-separated Worker markets that must have a fresh sync cursor",
    )
    parser.add_argument(
        "--max-worker-age-seconds",
        required=True,
        type=float,
        help="maximum acceptable cursor age per requested market",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-psql timeout in seconds ({MIN_TIMEOUT:g}-{MAX_TIMEOUT:g})",
    )
    parser.add_argument(
        "--api-timeout",
        type=float,
        default=DEFAULT_API_TIMEOUT,
        help=f"API request timeout in seconds ({MIN_TIMEOUT:g}-{MAX_API_TIMEOUT:g})",
    )
    args = parser.parse_args(argv)

    try:
        config = _build_config(args)
    except OperationsInputError as exc:
        print(f"operations check failed: {exc}", file=sys.stderr)
        return EXIT_INPUT

    try:
        results = run_checks(config)
    except Exception as exc:  # noqa: BLE001 - unexpected details are never printed
        print(f"operations check failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILURE

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        line = f"{status} {result.label}"
        if result.detail:
            line = f"{line}: {result.detail}"
        print(line)

    failed = sum(1 for result in results if not result.passed)
    print(f"SUMMARY: passed={len(results) - failed} failed={failed}")
    return EXIT_OK if failed == 0 else EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
