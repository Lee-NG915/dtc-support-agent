#!/usr/bin/env python3
"""Run the isolated after-sales browser acceptance suite.

This entry point is deliberately narrow:

* both source contexts come from explicit full commit SHAs via ``git archive``;
  the DTC working tree, ``.env`` files and untracked files are never copied;
* the E-pig sandbox is the existing ``dev:after-sales:sandbox`` implementation,
  started from a controlled archive context whose only linked dependency is the
  already-installed E-pig ``node_modules`` tree;
* the DTC PostgreSQL database is created with the canonical
  :mod:`scripts.run_after_sales_pg` ``ContainerSpec``/``DockerOps`` helpers,
  migrated with Alembic ``0001`` -> ``head`` (``0005``) and seeded only after
  the new database is confirmed empty;
* the DTC API/Worker receive only a fixed allowlist child environment, bind to
  loopback, and use the real E-pig service token from the sandbox manifest;
* the Playwright driver only talks to the two owned loopback origins and writes
  private screenshots/report evidence. It never opens a user Chrome profile;
* cleanup removes only resources recorded here, preserves the report and
  screenshots, and deletes every private secret manifest.

Usage::

    .venv/bin/python scripts/run_after_sales_browser.py \
      --epig-repo /Users/lidonghai/castlery/E-pig

No database URL, test path, arbitrary command or browser profile argument is
accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    from app.schemas.after_sales import AfterSalesMarket, StaffRole

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DTC_REF = "2209edd11438d13f734a652efa83cc23f7d6c026"
EPIG_REF = "72f162169de9f17e9188b9a95be5ac2c40a8f44d"
DTC_PG_IMAGE = "postgres:16-alpine"
DTC_MIGRATION_HEAD = "0005"
PLAYWRIGHT_VERSION = "1.65.0"
LOST_RESPONSE_MARKER = "e-pig-sandbox-committed-response-loss"
RUN_ROOT = Path(
    os.environ.get("DTC_BROWSER_RUN_ROOT", Path.home() / ".cache/dtc-after-sales-browser")
)
ENGINE_CACHE = RUN_ROOT / f"engine-playwright-{PLAYWRIGHT_VERSION}"
PLAYWRIGHT_CLI = ROOT / "frontend" / "node_modules" / "playwright" / "cli.js"
FRONTEND_NODE_MODULES = ROOT / "frontend" / "node_modules"

E_MANIFEST_RE = re.compile(r"^\[after-sales-sandbox\] manifest=(?P<path>\S+)$")
E_ORIGIN_RE = re.compile(r"^\[after-sales-sandbox\] browser_origin=(?P<url>\S+)$")
E_FAILED_RE = re.compile(r"^\[after-sales-sandbox\] FAILED: (?P<detail>.+)$")
E_READY_RE = re.compile(r"^\[after-sales-sandbox\] ready\b")
LOST_MARKER_RE = re.compile(re.escape(LOST_RESPONSE_MARKER))

ALLOWED_CHILD_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
    }
)

SAFE_CHILD_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "APP_ENV": "testing",
    "LOG_LEVEL": "WARNING",
    "AFTER_SALES_ENABLED": "true",
    "BUSINESS_CONNECTOR": "epig",
    "EPIG_ALLOWED_MARKETS": "au",
    "EPIG_TIMEOUT_SECONDS": "10",
    "EPIG_MAX_CONNECTIONS": "5",
    "AFTER_SALES_SKU_CATEGORY_MAP": '{"SYNTH-MUG-001":"homewares"}',
    "AFTER_SALES_WORKER_MARKETS": "au",
    "AFTER_SALES_WORKER_POLL_INTERVAL_SECONDS": "0.5",
    "AFTER_SALES_WORKER_BATCH_LIMIT": "10",
    "AFTER_SALES_WORKER_LEASE_SECONDS": "60",
    "AFTER_SALES_WORKER_OPERATION_TIMEOUT_SECONDS": "20",
    "AFTER_SALES_WORKER_LOOKUP_BUDGET": "10",
    "AFTER_SALES_WORKER_WRITE_BUDGET": "3",
    "AFTER_SALES_WORKER_RETRY_BASE_SECONDS": "0.1",
    "AFTER_SALES_WORKER_RETRY_MAX_SECONDS": "2.0",
    "DEEPSEEK_API_KEY": "",
    "DEEPSEEK_BASE_URL": "",
    "DEEPSEEK_MODEL": "offline",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "EMBEDDING_API_KEY": "",
    "EMBEDDING_BASE_URL": "",
    "LOCAL_EMBEDDING_ENABLED": "false",
    "LOCAL_EMBEDDING_FORCE": "false",
    "CROSS_ENCODER_ENABLED": "false",
    "CROSS_ENCODER_FORCE": "false",
    "UNSTRUCTURED_ENABLED": "false",
    "UNSTRUCTURED_FORCE": "false",
    "MCP_ENABLED": "false",
    "MCP_REMOTE_URL": "",
    "REDIS_URL": "",
    "QDRANT_URL": "",
    "ADMIN_API_TOKEN": "",
    "RATE_LIMIT_PER_MINUTE": "1000",
}

SECRET_PATTERNS = (
    re.compile(r"postgres(?:ql)?(?:\+asyncpg)?://[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9._~-]+", re.IGNORECASE),
    re.compile(r"(?i)(authorization\"?\s*[:=]\s*)\"[^\"]+\""),
    re.compile(r"(?i)(password\"?\s*[:=]\s*)\"[^\"]+\""),
)


class BrowserSuiteError(RuntimeError):
    """A safe, user-facing acceptance failure."""


class BrowserSuiteCancelled(BrowserSuiteError):
    """A termination signal arrived while this entry point owned resources."""


StaffSpec: TypeAlias = tuple[
    str,
    str,
    tuple["StaffRole", ...],
    tuple["AfterSalesMarket", ...],
]


@dataclass(frozen=True)
class Expected:
    run_marker: str
    consumer_one_description: str
    consumer_two_description: str
    summary_v1: str
    summary_v2: str
    reply_v1: str
    reply_v2: str
    support_b_summary: str
    support_b_reply: str
    support_a_summary: str
    support_a_reply: str
    material_manual_source: str
    material_manual_content: str
    material_delivery_source: str
    material_delivery_content: str


@dataclass
class OwnedProcess:
    name: str
    process: subprocess.Popen[Any]
    log_path: Path
    handle: Any | None = None


@dataclass
class RunState:
    run_id: str
    run_dir: Path
    screenshots_dir: Path
    secrets_dir: Path
    dtc_source: Path | None = None
    epig_source: Path | None = None
    dtc_spec: Any | None = None
    dtc_url: str | None = None
    dtc_pg_version: str | None = None
    dtc_port: int | None = None
    api: OwnedProcess | None = None
    worker: OwnedProcess | None = None
    epig_process: subprocess.Popen[Any] | None = None
    epig_lines: list[str] = field(default_factory=list)
    epig_manifest: dict[str, Any] | None = None
    epig_manifest_path: Path | None = None
    epig_next_pid: int | None = None
    browser_report: dict[str, Any] | None = None
    cleanup: dict[str, Any] = field(default_factory=dict)


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def redact_text(value: str, secrets_to_hide: Sequence[str] = ()) -> str:
    """Remove generated secrets, DB URLs and bearer values from output."""
    redacted = str(value)
    for secret in secrets_to_hide:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
            redacted = redacted.replace(quote(secret, safe=""), "[redacted]")
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def build_child_env(
    dtc_source: Path,
    database_url: str,
    epig_base_url: str,
    epig_service_token: str,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a fixed allowlist environment with no inherited secrets."""
    source = parent if parent is not None else os.environ
    env: dict[str, str] = {}
    for name in ALLOWED_CHILD_ENV:
        value = source.get(name)
        if value:
            env[name] = value
    env.update(SAFE_CHILD_ENV)
    env.update(
        {
            "DATABASE_URL": database_url,
            "ALEMBIC_DATABASE_URL": database_url,
            "KNOWLEDGE_DIR": str(dtc_source / "knowledge"),
            "EPIG_BASE_URL": epig_base_url,
            "EPIG_SERVICE_TOKEN": epig_service_token,
            "PYTHONPATH": str(dtc_source),
        }
    )
    return env


def build_expected(run_id: str) -> Expected:
    return Expected(
        run_marker=run_id,
        consumer_one_description=(f"Synthetic consumer one reports a damaged mug {run_id}"),
        consumer_two_description=(
            f"Synthetic consumer two unresolved delivery {LOST_RESPONSE_MARKER} {run_id}"
        ),
        summary_v1=f"Synthetic internal summary v1 {run_id}: manual follow-up requested.",
        summary_v2=f"Synthetic internal summary v2 {run_id}: manual follow-up required.",
        reply_v1=f"Synthetic consumer reply v1 {run_id}: forwarded for human follow-up.",
        reply_v2=(
            f"Synthetic consumer reply v2 {run_id}: forwarded for human follow-up; "
            "no financial or replacement action has been executed."
        ),
        support_b_summary=f"Synthetic support B summary {run_id}",
        support_b_reply=f"Synthetic support B reply {run_id}",
        support_a_summary=f"Synthetic support A summary {run_id}",
        support_a_reply=f"Synthetic support A reply {run_id}",
        material_manual_source=f"Synthetic manual source {run_id}",
        material_manual_content=f"Synthetic manual verification content {run_id}",
        material_delivery_source=f"Synthetic delivery source {run_id}",
        material_delivery_content=f"Synthetic delivery verification content {run_id}",
    )


def consumer_subject_mapping(subjects: Mapping[str, Any]) -> dict[str, Any]:
    """Map UI personas onto sandbox subjects.

    The official controlled sandbox injects its committed-response-loss route
    for subject ``one``. Consumer two must exercise its own fault-injected
    order, so the runner maps consumer two to subject ``one`` and consumer one
    to the unaffected subject ``two``. The mapping is reported explicitly.
    """
    one = subjects.get("one")
    two = subjects.get("two")
    if not isinstance(one, Mapping) or not isinstance(two, Mapping):
        raise BrowserSuiteError("the E-pig manifest must contain subjects one and two")
    for name, subject in (("one", one), ("two", two)):
        bootstrap = subject.get("bootstrapUrl")
        order_id = subject.get("orderId")
        if not isinstance(bootstrap, str) or not isinstance(order_id, str):
            raise BrowserSuiteError(f"E-pig subject {name} is missing bootstrap metadata")
    return {
        "consumerOne": {
            "subject": "two",
            "bootstrapUrl": two["bootstrapUrl"],
            "orderId": two["orderId"],
        },
        "consumerTwo": {
            "subject": "one",
            "bootstrapUrl": one["bootstrapUrl"],
            "orderId": one["orderId"],
            "lostResponse": True,
        },
        "lostResponseOrderId": one["orderId"],
    }


def validate_epig_manifest(
    manifest: Mapping[str, Any],
    *,
    runner_pid: int,
    upload_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the private sandbox manifest before any browser is opened."""
    if manifest.get("version") != 1:
        raise BrowserSuiteError("unsupported E-pig sandbox manifest version")
    if manifest.get("market") != "au":
        raise BrowserSuiteError("the browser suite only accepts the AU sandbox market")
    if int(manifest.get("runnerPid", -1)) != runner_pid:
        raise BrowserSuiteError("the E-pig manifest does not belong to this runner process")
    browser_origin = str(manifest.get("browserOrigin", ""))
    internal_origin = str(manifest.get("internalOrigin", ""))
    browser_host = urlsplit(browser_origin).hostname or ""
    if not re.fullmatch(r"epig-qa-[0-9a-f]{16}\.localhost", browser_host):
        raise BrowserSuiteError("the browser origin is not the dedicated loopback host")
    if urlsplit(browser_origin).scheme != "http":
        raise BrowserSuiteError("the browser origin must be plain loopback HTTP")
    internal = urlsplit(internal_origin)
    if internal.scheme != "http" or internal.hostname not in {"127.0.0.1", "localhost"}:
        raise BrowserSuiteError("the internal origin must be loopback HTTP")
    if internal.port is None or not (1 <= internal.port <= 65535):
        raise BrowserSuiteError("the internal origin must carry an explicit port")
    service = manifest.get("service")
    if not isinstance(service, Mapping):
        raise BrowserSuiteError("the sandbox manifest has no service credentials")
    token = service.get("token")
    if not isinstance(token, str) or len(token) < 16:
        raise BrowserSuiteError("the sandbox service token is missing or too short")
    database = manifest.get("database")
    if not isinstance(database, Mapping):
        raise BrowserSuiteError("the sandbox manifest has no database metadata")
    database_url = str(database.get("url", ""))
    if not database_url.startswith("postgres://"):
        raise BrowserSuiteError("the sandbox database URL is not an owned PostgreSQL URL")
    subjects = manifest.get("subjects")
    if not isinstance(subjects, Mapping):
        raise BrowserSuiteError("the sandbox manifest has no subjects")
    consumer_subject_mapping(subjects)
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, Mapping):
        raise BrowserSuiteError("the sandbox manifest has no fixtures")
    png = Path(str(fixtures.get("uploadPng", "")))
    if upload_path is not None:
        png = upload_path
    if not png.is_file():
        raise BrowserSuiteError("the synthetic upload PNG is missing")
    if stat.S_IMODE(png.stat().st_mode) != 0o600:
        raise BrowserSuiteError("the synthetic upload PNG permissions are not 0600")
    boundaries = manifest.get("boundaries")
    if not isinstance(boundaries, Mapping):
        raise BrowserSuiteError("the sandbox manifest has no controlled fault boundaries")
    lost = boundaries.get("lostResponse")
    if not isinstance(lost, Mapping) or lost.get("descriptionMarker") != LOST_RESPONSE_MARKER:
        raise BrowserSuiteError("the committed-response-loss fixture is missing or changed")
    return {
        "browser_origin": browser_origin,
        "internal_origin": internal_origin,
        "service_token": token,
        "database_url": database_url,
        "upload_png": png,
        "subjects": subjects,
        "boundaries": boundaries,
    }


def validate_api_origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None:
        raise BrowserSuiteError("the DTC API origin must be explicit loopback HTTP")
    return f"http://127.0.0.1:{parts.port}"


def _create_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(path, 0o700)
    return path


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


def _require_private_file(path: Path, mode: int = 0o600) -> None:
    if not path.is_file():
        raise BrowserSuiteError(f"required private file is missing: {path.name}")
    if stat.S_IMODE(path.stat().st_mode) != mode:
        raise BrowserSuiteError(f"required private file has unsafe permissions: {path.name}")


def pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _git_archive(repo: Path, ref: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", ref],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise BrowserSuiteError("could not archive the requested immutable source commit")
    return result.stdout


def _extract_context(repo: Path, ref: str, destination: Path) -> Any:
    from scripts.prepare_after_sales_demo import DemoPreparationError, prepare_context

    try:
        return prepare_context(repo, ref, destination)
    except DemoPreparationError as exc:
        raise BrowserSuiteError(f"source context preparation failed: {exc}") from exc


def _link_epig_dependencies(context: Path, epig_repo: Path) -> dict[str, str]:
    root_modules = epig_repo / "node_modules"
    happy_dog_modules = epig_repo / "packages" / "happy-dog" / "node_modules"
    if not root_modules.is_dir():
        raise BrowserSuiteError("the E-pig dependency tree is not installed")
    target_modules = context / "node_modules"
    if target_modules.exists():
        raise BrowserSuiteError("the E-pig archive unexpectedly contains node_modules")
    target_modules.symlink_to(root_modules, target_is_directory=True)
    if happy_dog_modules.is_dir():
        target_happy_dog = context / "packages" / "happy-dog" / "node_modules"
        if target_happy_dog.exists():
            raise BrowserSuiteError("the E-pig archive unexpectedly contains package node_modules")
        target_happy_dog.symlink_to(happy_dog_modules, target_is_directory=True)
    return {
        "root_node_modules": str(root_modules),
        "happy_dog_node_modules": str(happy_dog_modules) if happy_dog_modules.is_dir() else "",
    }


def _check_docker(docker: Any) -> None:
    try:
        docker.require_available()
    except Exception as exc:  # noqa: BLE001 - canonical helper exception type
        raise BrowserSuiteError("Docker is required for the isolated browser suite") from exc


def _published_loopback_port(docker: Any, name: str, timeout: float = 30.0) -> int:
    """Retry the canonical loopback-only port observation over a bounded window."""
    deadline = time.monotonic() + timeout
    last_failure = "no observation"
    while time.monotonic() < deadline:
        try:
            return int(docker.published_port(name))
        except Exception as exc:  # noqa: BLE001 - canonical helper error
            last_failure = type(exc).__name__
        time.sleep(0.25)
    raw = docker._run("port", name, "5432/tcp", timeout=15.0).stdout.strip()
    raise BrowserSuiteError(
        "could not observe a loopback-only PostgreSQL port "
        f"({last_failure}; docker port output={redact_text(raw) or 'empty'})"
    )


def _docker_inspect(docker: Any, name: str, template: str) -> str:
    result = docker._run("inspect", "-f", template, name, timeout=20.0)
    if result.returncode != 0:
        raise BrowserSuiteError("could not inspect the owned PostgreSQL container")
    return result.stdout.strip()


def _remove_dtc_postgres(docker: Any, spec: Any) -> dict[str, Any]:
    if not docker.inspect_exists(spec.name):
        return {
            "container": spec.name,
            "container_removed": True,
            "tracked_volumes": 0,
            "remaining_volumes": [],
            "clean": True,
        }
    if _docker_inspect(docker, spec.name, '{{index .Config.Labels "dtc.pgtest"}}') != "1":
        raise BrowserSuiteError("refusing to remove a PostgreSQL container without this run label")
    mounts = _docker_inspect(
        docker,
        spec.name,
        '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\\n"}}{{end}}{{end}}',
    )
    volumes = [line for line in mounts.splitlines() if line]
    removed = docker.remove(spec.name)
    remaining: list[str] = []
    for volume in volumes:
        result = docker._run("volume", "inspect", volume, timeout=20.0)
        if result.returncode == 0:
            docker._run("volume", "rm", "-f", volume, timeout=20.0)
        if docker._run("volume", "inspect", volume, timeout=20.0).returncode == 0:
            remaining.append(volume)
    exists = docker.inspect_exists(spec.name)
    return {
        "container": spec.name,
        "container_removed": bool(removed) and not exists,
        "tracked_volumes": len(volumes),
        "remaining_volumes": remaining,
        "clean": bool(removed) and not exists and not remaining,
    }


async def _apply_migrations(dtc_source: Path, database_url: str, env: Mapping[str, str]) -> None:
    command = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        "alembic.ini",
        "upgrade",
        "head",
    ]
    child_env = dict(env)
    child_env["ALEMBIC_DATABASE_URL"] = database_url
    result = await asyncio.to_thread(
        subprocess.run,
        command,
        cwd=str(dtc_source),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=180.0,
        check=False,
    )
    if result.returncode != 0:
        detail = redact_text((result.stderr or result.stdout or "").strip()[-1500:])
        raise BrowserSuiteError(f"Alembic migration failed: {detail or 'no diagnostic output'}")


async def _database_revision(database_url: str) -> tuple[str | None, str]:
    import asyncpg

    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn)
    try:
        revision = await connection.fetchval("SELECT version_num FROM alembic_version")
        server_version = await connection.fetchval("SHOW server_version")
        columns = await connection.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'after_sales_operation' "
            "AND column_name = 'lookup_count'"
        )
        if int(columns or 0) != 1:
            raise BrowserSuiteError("migrated PostgreSQL schema is missing 0005 columns")
        return (str(revision) if revision is not None else None, str(server_version))
    finally:
        await connection.close()


async def _seed_staff(
    database_url: str, dtc_source: Path, credentials_path: Path
) -> dict[str, str]:
    """Seed exactly the four synthetic staff roles into the new empty database."""
    import asyncpg
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    sys.path.insert(0, str(dtc_source))
    import app

    if not Path(app.__file__).resolve().is_relative_to(dtc_source.resolve()):
        raise BrowserSuiteError("the seeding helper imported app from outside the archive")

    from app.core.security import generate_staff_token
    from app.core.time import utcnow
    from app.repositories.staff_repository import StaffRepository

    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn)
    try:
        staff_count = await connection.fetchval("SELECT count(*) FROM after_sales_staff")
        case_count = await connection.fetchval("SELECT count(*) FROM after_sales_case")
        if int(staff_count or 0) != 0 or int(case_count or 0) != 0:
            raise BrowserSuiteError("the new DTC database is not empty; refusing to seed")
    finally:
        await connection.close()

    specs: tuple[StaffSpec, ...] = (
        ("staff_support_au", "Synthetic Support AU", ("support",), ("au",)),
        ("staff_approver_au", "Synthetic Approver AU", ("approver",), ("au",)),
        ("staff_auditor_all", "Synthetic Auditor All", ("auditor",), ("au", "sg", "us")),
        ("staff_operator_au", "Synthetic Operator AU", ("operator",), ("au",)),
    )
    tokens: dict[str, str] = {}
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        expires_at = utcnow() + timedelta(days=7)
        async with factory.begin() as session:
            repository = StaffRepository(session)
            for staff_id, display_name, roles, markets in specs:
                token = generate_staff_token()
                await repository.create(
                    staff_id=staff_id,
                    display_name=display_name,
                    token=token,
                    roles=roles,
                    allowed_markets=markets,
                    expires_at=expires_at,
                )
                tokens[staff_id] = token
    finally:
        await engine.dispose()
    _write_private_json(
        credentials_path,
        {
            "schema_version": "1",
            "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
            "staff": [
                {
                    "staff_id": staff_id,
                    "roles": list(roles),
                    "allowed_markets": list(markets),
                    "token": tokens[staff_id],
                }
                for staff_id, _display, roles, markets in specs
            ],
        },
    )
    return tokens


def _run_node_build(dtc_source: Path, log_path: Path) -> str:
    frontend = dtc_source / "frontend"
    if not (frontend / "package.json").is_file():
        raise BrowserSuiteError("the DTC archive has no frontend package")
    modules = frontend / "node_modules"
    if modules.exists():
        raise BrowserSuiteError("the DTC archive unexpectedly contains frontend node_modules")
    modules.symlink_to(FRONTEND_NODE_MODULES, target_is_directory=True)
    env = {name: value for name, value in os.environ.items() if name in ALLOWED_CHILD_ENV and value}
    env.update({"CI": "1", "NODE_ENV": "production", "VITE_API_BASE": ""})
    with log_path.open("wb") as log:
        result = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(frontend),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=600.0,
            check=False,
        )
    if result.returncode != 0:
        raise BrowserSuiteError("the controlled DTC frontend build failed")
    index = frontend / "dist" / "index.html"
    if not index.is_file():
        raise BrowserSuiteError("the controlled DTC build produced no dist/index.html")
    return _sha256_file(index)


def _start_logged_process(
    name: str,
    command: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
) -> OwnedProcess:
    handle = log_path.open("wb")
    os.chmod(log_path, 0o600)
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        handle.close()
        raise
    return OwnedProcess(name=name, process=process, log_path=log_path, handle=handle)


def _process_exited(process: subprocess.Popen[Any]) -> bool:
    return process.poll() is not None


def _group_helpers() -> tuple[Any, Any, Any]:
    from scripts.run_after_sales_pg import _signal_group, _terminate_group, _wait_group_gone

    return _signal_group, _terminate_group, _wait_group_gone


def _group_alive(pgid: int, timeout: float = 0.1) -> bool:
    _, _, wait_group_gone = _group_helpers()
    return not wait_group_gone(pgid, timeout)


def _reap_and_clean_group(
    process: subprocess.Popen[Any], *, grace: float = 15.0
) -> tuple[bool, bool, bool]:
    """Terminate one owned group, reap its leader, then observe the group.

    A zombie leader keeps its process group id observable on macOS until it is
    reaped, so the group observation must happen after ``wait``. Killing the
    leader alone is never accepted as cleanup.
    """
    signal_group, _, _ = _group_helpers()
    pgid = process.pid
    forced = False
    if process.poll() is None:
        signal_group(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        forced = True
        signal_group(pgid, signal.SIGKILL)
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
    group_gone = not _group_alive(pgid, 2.0)
    if not group_gone:
        forced = True
        signal_group(pgid, signal.SIGKILL)
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        group_gone = not _group_alive(pgid, 2.0)
    return process.poll() is not None, group_gone, forced


def _stop_logged_process(owned: OwnedProcess | None, *, grace: float = 15.0) -> dict[str, Any]:
    """Stop one owned leader and require its whole process group to be gone."""
    if owned is None:
        return {
            "stopped": True,
            "clean": True,
            "reason": "not_started",
            "leader_exited": True,
            "group_gone": True,
            "forced": False,
        }
    process = owned.process
    leader_exited, group_gone, forced = _reap_and_clean_group(process, grace=grace)
    if owned.handle is not None:
        try:
            owned.handle.close()
        except OSError:
            pass
    return {
        "stopped": leader_exited and group_gone,
        "clean": leader_exited and group_gone,
        "leader_exited": leader_exited,
        "group_gone": group_gone,
        "forced": forced,
        "exit_code": process.returncode,
    }


def _run_owned_process_group(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    timeout: float,
    state: Any,
    poll_interval: float = 0.25,
    grace: float = 15.0,
) -> dict[str, Any]:
    """Run one fixed child in an owned group with bounded polling and cleanup.

    The leader exiting is not success: the whole group must be gone. A
    timeout or a cancellation still terminates the entire owned group and
    reports the root-level observation, never an inference from a report.
    """
    handle = log_path.open("wb")
    os.chmod(log_path, 0o600)
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        handle.close()
        raise
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if state.requested:
                cancelled = True
                break
            if process.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            time.sleep(min(poll_interval, remaining))
        leader_exited, group_gone, forced = _reap_and_clean_group(process, grace=grace)
        return {
            "pid": process.pid,
            "returncode": process.returncode,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "leader_exited": leader_exited,
            "group_gone": group_gone,
            "forced": forced,
            "clean": leader_exited and group_gone,
            "log_path": str(log_path),
        }
    finally:
        try:
            handle.close()
        except OSError:
            pass


def _wait_for_api_ready(
    origin: str, *, state: Any | None = None, timeout: float = 180.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = "no probe completed"
    while time.monotonic() < deadline:
        if state is not None and state.requested:
            raise BrowserSuiteCancelled("termination requested while waiting for the DTC API")
        try:
            with urllib.request.urlopen(f"{origin}/api/v1/ready", timeout=5.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            database = [
                item
                for item in payload.get("dependencies", [])
                if isinstance(item, Mapping) and item.get("name") == "database"
            ]
            if payload.get("ready") is True and database and database[0].get("ready") is True:
                return payload
            last = "readiness response did not confirm the database"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = type(exc).__name__
        time.sleep(0.25)
    raise BrowserSuiteError(f"the DTC API did not become ready: {last}")


def _read_capabilities(origin: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{origin}/api/v1/capabilities", timeout=10.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _pump_e_output(stream: Any, lines: list[str], events: queue.Queue[str | None]) -> None:
    try:
        for raw in stream:
            line = raw.rstrip("\r\n")
            lines.append(line)
            events.put(line)
    finally:
        events.put(None)


def _start_epig_sandbox(
    epig_source: Path,
    env: Mapping[str, str],
    state: RunState,
    *,
    signal_state: Any | None = None,
) -> dict[str, Any]:
    command = [
        "node",
        str(epig_source / "scripts" / "database" / "run-after-sales-pg.mjs"),
        "--browser",
    ]
    process = subprocess.Popen(
        command,
        cwd=str(epig_source),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    state.epig_process = process
    events: queue.Queue[str | None] = queue.Queue()
    thread = threading.Thread(
        target=_pump_e_output,
        args=(process.stdout, state.epig_lines, events),
        daemon=True,
    )
    thread.start()
    manifest_path: Path | None = None
    browser_origin: str | None = None
    deadline = time.monotonic() + 300.0
    while time.monotonic() < deadline:
        if signal_state is not None and signal_state.requested:
            raise BrowserSuiteCancelled("termination requested while starting the E-pig sandbox")
        try:
            line = events.get(timeout=1.0)
        except queue.Empty:
            if process.poll() is not None:
                raise BrowserSuiteError(
                    "the E-pig sandbox exited before publishing its manifest"
                ) from None
            continue
        if line is None:
            break
        manifest_match = E_MANIFEST_RE.match(line)
        if manifest_match:
            manifest_path = Path(manifest_match.group("path"))
        origin_match = E_ORIGIN_RE.match(line)
        if origin_match:
            browser_origin = origin_match.group("url")
        failed = E_FAILED_RE.match(line)
        if failed:
            raise BrowserSuiteError(
                f"the E-pig sandbox failed: {redact_text(failed.group('detail'))}"
            )
        if manifest_path is not None and browser_origin is not None:
            break
    if manifest_path is None or browser_origin is None:
        raise BrowserSuiteError("the E-pig sandbox did not publish its manifest in time")
    if not manifest_path.is_file():
        raise BrowserSuiteError("the E-pig sandbox manifest is missing")
    _require_private_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validated = validate_epig_manifest(manifest, runner_pid=process.pid)
    state.epig_manifest = manifest
    state.epig_manifest_path = manifest_path
    next_pid = manifest.get("nextPid")
    if not isinstance(next_pid, int) or next_pid <= 0:
        raise BrowserSuiteError("the E-pig manifest has no valid Next process id")
    state.epig_next_pid = next_pid
    return validated


def _stop_epig_sandbox(state: RunState) -> dict[str, Any]:
    process = state.epig_process
    if process is None:
        return {"stopped": True, "clean": True, "reason": "not_started"}
    if process.poll() is None:
        _signal_process_group_by_pid(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 45.0
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    forced = False
    if process.poll() is None:
        forced = True
        _signal_process_group_by_pid(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
    clean_line = any("stopped" in line and "clean=true" in line for line in state.epig_lines)
    manifest_removed = (
        state.epig_manifest_path is not None and not state.epig_manifest_path.exists()
    )
    next_gone = True
    if state.epig_next_pid is not None:
        try:
            os.killpg(state.epig_next_pid, 0)
        except ProcessLookupError:
            next_gone = True
        except PermissionError:
            next_gone = False
        else:
            next_gone = False
            _signal_process_group_by_pid(state.epig_next_pid, signal.SIGTERM)
            time.sleep(2.0)
            try:
                os.killpg(state.epig_next_pid, 0)
            except ProcessLookupError:
                next_gone = True
            except PermissionError:
                next_gone = False
            else:
                _signal_process_group_by_pid(state.epig_next_pid, signal.SIGKILL)
                time.sleep(1.0)
                try:
                    os.killpg(state.epig_next_pid, 0)
                except ProcessLookupError:
                    next_gone = True
    return {
        "stopped": process.poll() is not None,
        "forced": forced,
        "clean_line": clean_line,
        "manifest_removed": manifest_removed,
        "next_group_gone": next_gone,
        "clean": process.poll() is not None and clean_line and manifest_removed and next_gone,
    }


def _signal_process_group_by_pid(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _remove_owned_tree(path: Path | None, parent: Path) -> bool:
    if path is None:
        return True
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise BrowserSuiteError("refusing to remove a path outside this run directory") from exc
    if not resolved.exists():
        return True
    shutil.rmtree(resolved)
    return not resolved.exists()


def _run_browser_driver(
    config_path: Path,
    report_path: Path,
    env: Mapping[str, str],
    *,
    log_path: Path,
    state: Any,
    timeout: float = 3600.0,
) -> dict[str, Any]:
    command = [
        "node",
        str(ROOT / "frontend" / "scripts" / "after-sales-browser.mjs"),
        str(config_path),
        str(report_path),
    ]
    return _run_owned_process_group(
        command,
        cwd=ROOT,
        env=env,
        log_path=log_path,
        timeout=timeout,
        state=state,
    )


def _cleanup_browser_group(sidecar_path: Path, *, grace: float = 15.0) -> dict[str, Any]:
    """Clean the exact browser process group persisted by the driver.

    Playwright spawns Chromium detached, so Node's process group does not own
    it. The sidecar is written immediately after ``launchServer`` and contains
    only the actual PID/PGID and executable identity, never the endpoint. A
    report file alone is never evidence that the browser group is gone.
    """
    if not sidecar_path.exists():
        return {"clean": False, "reason": "missing_lifecycle_sidecar"}
    try:
        _require_private_file(sidecar_path)
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
        pgid = int(data.get("pgid", 0))
    except (BrowserSuiteError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"clean": False, "reason": "invalid_lifecycle_sidecar", "error": type(exc).__name__}
    if pid <= 1 or pgid <= 1:
        return {"clean": False, "reason": "invalid_lifecycle_identity", "pid": pid, "pgid": pgid}
    if data.get("platform") == "win32":
        return {
            "clean": False,
            "reason": "windows_browser_group_not_supported",
            "pid": pid,
            "pgid": pgid,
        }
    signal_group, _, wait_group_gone = _group_helpers()
    group_alive = not wait_group_gone(pgid, 0.1)
    if not group_alive:
        return {
            "clean": True,
            "group_gone": True,
            "pid": pid,
            "pgid": pgid,
            "verified": "group_already_gone",
        }
    try:
        actual_pgid = os.getpgid(pid)
    except ProcessLookupError:
        actual_pgid = None
    except PermissionError:
        return {
            "clean": False,
            "reason": "browser_group_permission_denied",
            "pid": pid,
            "pgid": pgid,
        }
    if actual_pgid is not None and actual_pgid != pgid:
        return {
            "clean": False,
            "reason": "browser_pgid_mismatch",
            "pid": pid,
            "expected_pgid": pgid,
            "actual_pgid": actual_pgid,
        }
    if actual_pgid is not None:
        expected_executable = str(data.get("executablePath", ""))
        if not expected_executable:
            return {
                "clean": False,
                "reason": "missing_browser_executable_identity",
                "pid": pid,
                "pgid": pgid,
            }
        try:
            inspected = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {
                "clean": False,
                "reason": "browser_process_inspection_failed",
                "pid": pid,
                "pgid": pgid,
            }
        if inspected.returncode != 0 or expected_executable not in inspected.stdout:
            return {
                "clean": False,
                "reason": "unverified_browser_process",
                "pid": pid,
                "pgid": pgid,
            }
    forced = False
    signal_group(pgid, signal.SIGTERM)
    group_gone = wait_group_gone(pgid, grace)
    if not group_gone:
        forced = True
        signal_group(pgid, signal.SIGKILL)
        group_gone = wait_group_gone(pgid, 5.0)
    return {
        "clean": group_gone,
        "group_gone": group_gone,
        "pid": pid,
        "pgid": pgid,
        "forced": forced,
        "verified": "leader_or_owned_group",
    }


def browser_close_status(browser_report: Mapping[str, Any] | None) -> dict[str, Any]:
    """Require the browser driver's explicit close observation.

    A report file is evidence that the driver ran, not evidence that Chromium
    closed. Only an explicit successful ``browser.close()`` whose connection is
    observed false counts.
    """
    if not browser_report:
        return {"clean": False, "reason": "no_driver_report"}
    lifecycle = browser_report.get("browserLifecycle")
    if not isinstance(lifecycle, Mapping):
        return {"clean": False, "reason": "missing_explicit_close_observation"}
    attempted = lifecycle.get("closeAttempted") is True
    ok = lifecycle.get("closeOk") is True
    connected = lifecycle.get("connectedAfterClose")
    clean = attempted and ok and connected is False
    return {
        "clean": clean,
        "reason": "closed" if clean else "explicit_close_incomplete",
        "close_attempted": attempted,
        "close_ok": ok,
        "connected_after_close": connected,
        "error": lifecycle.get("error"),
    }


async def _verify_databases(
    dtc_url: str,
    epig_database_url: str,
    expected: Expected,
    mapping: Mapping[str, Any],
    browser_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Read per-request/per-order facts from both real PostgreSQL databases."""
    import asyncpg

    ids = browser_report.get("ids", {})
    consumer_one_request = ids.get("consumerOneRequestId")
    consumer_one_ticket = ids.get("consumerOneTicketId")
    consumer_one_attachment = ids.get("consumerOneAttachmentId")
    if not all(
        isinstance(value, str) and value
        for value in (consumer_one_request, consumer_one_ticket, consumer_one_attachment)
    ):
        raise BrowserSuiteError("browser report did not record the consumer-one identifiers")
    consumer_one_order = mapping["consumerOne"]["orderId"]
    consumer_two_order = mapping["consumerTwo"]["orderId"]

    dtc_dsn = dtc_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    dtc = await asyncpg.connect(dtc_dsn)
    try:
        one_case = await dtc.fetchrow(
            "SELECT case_id, state, request_id, remote_ticket_id "
            "FROM after_sales_case WHERE order_id = $1",
            consumer_one_order,
        )
        if one_case is None:
            raise BrowserSuiteError("no DTC case exists for the consumer-one order")
        drafts = await dtc.fetch(
            "SELECT version, status, consumer_reply, action FROM after_sales_draft "
            "WHERE case_id = $1 ORDER BY version",
            one_case["case_id"],
        )
        approvals = await dtc.fetch(
            "SELECT status FROM after_sales_approval WHERE case_id = $1",
            one_case["case_id"],
        )
        operation = await dtc.fetchrow(
            "SELECT status, receipt_json FROM after_sales_operation WHERE case_id = $1",
            one_case["case_id"],
        )
        two_case = await dtc.fetchrow(
            "SELECT case_id, state FROM after_sales_case WHERE order_id = $1",
            consumer_two_order,
        )
        if two_case is None:
            raise BrowserSuiteError("no DTC case exists for the consumer-two order")
        two_drafts = await dtc.fetch(
            "SELECT version, status, consumer_reply FROM after_sales_draft "
            "WHERE case_id = $1 ORDER BY version",
            two_case["case_id"],
        )
        two_approvals = await dtc.fetchval(
            "SELECT count(*) FROM after_sales_approval WHERE case_id = $1",
            two_case["case_id"],
        )
        two_operations = await dtc.fetchval(
            "SELECT count(*) FROM after_sales_operation WHERE case_id = $1",
            two_case["case_id"],
        )
    finally:
        await dtc.close()

    epig = await asyncpg.connect(epig_database_url)
    try:
        epig_server_version = str(await epig.fetchval("SHOW server_version"))
        one_request = await epig.fetchrow(
            "SELECT id, data FROM after_sales_requests WHERE order_id = $1",
            consumer_one_order,
        )
        one_request_count = await epig.fetchval(
            "SELECT count(*) FROM after_sales_requests WHERE order_id = $1",
            consumer_one_order,
        )
        one_ticket_count = await epig.fetchval(
            "SELECT count(*) FROM after_sales_tickets WHERE request_id = $1",
            consumer_one_request,
        )
        one_attachment_count = await epig.fetchval(
            "SELECT count(*) FROM after_sales_attachments WHERE order_id = $1",
            consumer_one_order,
        )
        two_request = await epig.fetchrow(
            "SELECT id, data FROM after_sales_requests WHERE order_id = $1",
            consumer_two_order,
        )
        two_request_count = await epig.fetchval(
            "SELECT count(*) FROM after_sales_requests WHERE order_id = $1",
            consumer_two_order,
        )
        two_ticket_count = await epig.fetchval(
            "SELECT count(*) FROM after_sales_tickets WHERE order_id = $1",
            consumer_two_order,
        )
    finally:
        await epig.close()

    checks = {
        "consumer_one_case_completed": one_case["state"] == "completed",
        "consumer_one_request_id_matches": str(one_case["request_id"]) == consumer_one_request,
        "consumer_one_remote_ticket_matches": str(one_case["remote_ticket_id"] or "")
        == consumer_one_ticket,
        "consumer_one_two_drafts": [int(row["version"]) for row in drafts] == [1, 2],
        "consumer_one_edited_reply_consumed": bool(drafts)
        and drafts[-1]["status"] == "consumed"
        and drafts[-1]["consumer_reply"] == expected.reply_v2,
        "consumer_one_action_other": bool(drafts)
        and all(row["action"] == "other" for row in drafts),
        "consumer_one_approval_recorded": bool(approvals),
        "consumer_one_operation_succeeded": operation is not None
        and operation["status"] == "succeeded"
        and consumer_one_ticket in str(operation["receipt_json"] or ""),
        "epig_one_request": int(one_request_count or 0) == 1
        and one_request is not None
        and str(one_request["id"]) == consumer_one_request,
        "epig_one_ticket": int(one_ticket_count or 0) == 1,
        "epig_one_attachment": int(one_attachment_count or 0) >= 1,
        "consumer_two_pending": two_case["state"] in {"pending", "processing"},
        "consumer_two_two_drafts": [int(row["version"]) for row in two_drafts] == [1, 2],
        "consumer_two_no_approval": int(two_approvals or 0) == 0,
        "consumer_two_no_operation": int(two_operations or 0) == 0,
        "epig_two_unique_request": int(two_request_count or 0) == 1
        and two_request is not None
        and str(two_request["id"]) == ids.get("consumerTwoRequestId"),
        "epig_two_no_ticket": int(two_ticket_count or 0) == 0,
        "attachment_id_present": isinstance(consumer_one_attachment, str),
    }
    return {
        "checks": checks,
        "ok": all(checks.values()),
        "dtc": {
            "consumer_one_case": dict(one_case),
            "consumer_one_drafts": [
                {"version": int(row["version"]), "status": row["status"], "action": row["action"]}
                for row in drafts
            ],
            "consumer_one_approvals": [dict(row) for row in approvals],
            "consumer_one_operation": (
                {
                    "status": operation["status"],
                    "ticket_present": consumer_one_ticket in str(operation["receipt_json"] or ""),
                }
                if operation is not None
                else None
            ),
            "consumer_two_case": dict(two_case),
            "consumer_two_drafts": [
                {"version": int(row["version"]), "status": row["status"]} for row in two_drafts
            ],
        },
        "epig": {
            "server_version": epig_server_version,
            "consumer_one_request_count": int(one_request_count or 0),
            "consumer_one_ticket_count": int(one_ticket_count or 0),
            "consumer_one_attachment_count": int(one_attachment_count or 0),
            "consumer_two_request_count": int(two_request_count or 0),
            "consumer_two_ticket_count": int(two_ticket_count or 0),
        },
    }


def _base_report(state: RunState, expected: Expected) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "dtc-after-sales-browser-suite",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "run_directory": str(state.run_dir),
        "screenshots_directory": str(state.screenshots_dir),
        "source": {
            "dtc_ref": DTC_REF,
            "epig_ref": EPIG_REF,
            "dataset": "not_used (fixed synthetic UI fixture; no model/dataset evaluation)",
            "playwright_version": PLAYWRIGHT_VERSION,
            "browser_engine": "Playwright Chromium from dedicated marked cache",
            "subject_mapping": {
                "consumerOne": "sandbox subject two (unaffected order)",
                "consumerTwo": "sandbox subject one (committed-response-loss fixture)",
            },
        },
        "expected": {
            "run_marker": expected.run_marker,
            "lost_response_marker": LOST_RESPONSE_MARKER,
        },
        "model_calls": 0,
        "limits": {
            "a11y": "keyboard Tab reachability only; not a full WCAG audit",
            "mobile": "320px render/overflow check; not a device-lab result",
            "desktop_plugin": "not verified; this suite is an independent software E2E surface",
            "expert_ui_review": "screenshots preserved for Codex manual visual review",
        },
        "groups": [],
        "errors": [],
        "cleanup": {},
    }


def _write_report(report: Mapping[str, Any], path: Path) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _summary_lines(report: Mapping[str, Any], report_path: Path) -> list[str]:
    groups = report.get("groups") or []
    group_text = " ".join(f"{item.get('id')}={item.get('status')}" for item in groups)
    cleanup = report.get("cleanup") or {}
    return [
        f"dtc after-sales browser suite: {str(report.get('status', 'failed')).upper()}",
        f"  report={report_path}",
        f"  screenshots={report.get('screenshots_directory')}",
        f"  groups: {group_text or 'none'}",
        "  model_calls=0 resources=postgres+epig-sandbox+api+worker",
        f"  cleanup_clean={cleanup.get('clean')} "
        f"report_preserved={cleanup.get('report_preserved')} "
        f"secrets_removed={cleanup.get('secrets_removed')}",
    ]


def _load_browser_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BrowserSuiteError("the browser driver produced no report")
    _require_private_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BrowserSuiteError("the browser report is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise BrowserSuiteError("the browser report is not an object")
    return payload


def _merge_browser_report(report: dict[str, Any], browser: Mapping[str, Any]) -> None:
    groups = browser.get("groups")
    if isinstance(groups, list):
        report["groups"] = groups
    for key in (
        "browser",
        "ids",
        "counts",
        "blockedExternalRequests",
        "pageErrors",
        "routeErrors",
        "consoleErrors",
        "failureDiagnostics",
        "browserLifecycle",
        "screenshots",
        "limits",
    ):
        if key in browser:
            report[key] = browser[key]
    if browser.get("status") == "failed" or any(
        isinstance(item, Mapping) and item.get("status") == "failed" for item in groups or []
    ):
        report["status"] = "failed"
        for item in groups or []:
            if isinstance(item, Mapping) and item.get("status") == "failed":
                report["errors"].append(f"{item.get('id')}: group failed")
    route_errors = browser.get("routeErrors")
    if isinstance(route_errors, list) and route_errors:
        report["status"] = "failed"
        report["errors"].append(f"unexpected route errors: {len(route_errors)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--epig-repo",
        type=Path,
        default=Path("/Users/lidonghai/castlery/E-pig"),
        help="E-pig git worktree; only the immutable commit is archived",
    )
    args = parser.parse_args(argv)

    from scripts.run_after_sales_pg import (
        DockerOps,
        SignalState,
        build_spec,
        install_signal_handlers,
        restore_signal_handlers,
        target_url,
        validate_target_url,
        wait_for_database,
    )

    signal_state = SignalState()
    previous_handlers = install_signal_handlers(signal_state)
    run_id = secrets.token_hex(8)
    run_root = RUN_ROOT / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    os.chmod(run_root, 0o700)
    run_dir = _create_private_dir(run_root / f"{_utc_stamp()}-{run_id}")
    screenshots_dir = _create_private_dir(run_dir / "screenshots")
    secrets_dir = _create_private_dir(run_dir / "secrets")
    logs_dir = _create_private_dir(run_dir / "logs")
    browser_profile_root = _create_private_dir(run_dir / "browser-profile-root")
    browser_lifecycle_path = run_dir / "browser-lifecycle.json"
    state = RunState(
        run_id=run_id,
        run_dir=run_dir,
        screenshots_dir=screenshots_dir,
        secrets_dir=secrets_dir,
    )
    expected = build_expected(run_id)
    report = _base_report(state, expected)
    report_path = run_dir / "report.json"
    browser_report_path = run_dir / "browser-report.json"
    browser_config_path = secrets_dir / "browser-config.json"
    staff_credentials_path = secrets_dir / "dtc-staff.json"
    docker = DockerOps()
    exit_code = 1
    redaction_values: list[str] = []
    driver_result: dict[str, Any] | None = None
    driver_log_path = logs_dir / "browser-driver.log"

    try:
        if signal_state.requested:
            raise BrowserSuiteCancelled("termination requested before setup")
        if not PLAYWRIGHT_CLI.is_file():
            raise BrowserSuiteError("pinned Playwright is not installed in frontend/node_modules")
        if not ENGINE_CACHE.is_dir() or not (ENGINE_CACHE / ".dtc-owned").is_file():
            raise BrowserSuiteError("the dedicated marked Chromium cache is unavailable")
        _require_private_file(ENGINE_CACHE / ".dtc-owned")
        if not args.epig_repo.is_dir():
            raise BrowserSuiteError("the E-pig source repository path is unavailable")
        _check_docker(docker)

        state.dtc_source = run_dir / "dtc-source"
        state.epig_source = run_dir / "epig-source"
        dtc_context = _extract_context(ROOT, DTC_REF, state.dtc_source)
        epig_context = _extract_context(args.epig_repo, EPIG_REF, state.epig_source)
        epig_links = _link_epig_dependencies(state.epig_source, args.epig_repo)
        report["source"].update(
            {
                "dtc_archive_sha256": dtc_context.archive_sha256,
                "dtc_worktree_excluded": True,
                "epig_archive_sha256": epig_context.archive_sha256,
                "epig_worktree_dirty_excluded": epig_context.worktree_dirty,
                "epig_dependencies": epig_links,
            }
        )

        spec = build_spec()
        state.dtc_spec = spec
        docker.run_container(spec)
        if signal_state.requested:
            raise BrowserSuiteCancelled("termination requested during container creation")
        state.dtc_port = _published_loopback_port(docker, spec.name)
        state.dtc_url = target_url(spec, state.dtc_port)
        info = validate_target_url(state.dtc_url, spec.run_id)
        if (info.username, info.database) != (spec.username, spec.database):
            raise BrowserSuiteError("the generated DTC target does not match this run")
        state.dtc_pg_version = wait_for_database(spec, state.dtc_port, state=signal_state)
        migration_env = build_child_env(
            state.dtc_source,
            state.dtc_url,
            "http://127.0.0.1:1",
            "not-used-for-migrations",
        )
        asyncio.run(_apply_migrations(state.dtc_source, state.dtc_url, migration_env))
        revision, server_version = asyncio.run(_database_revision(state.dtc_url))
        if revision != DTC_MIGRATION_HEAD:
            raise BrowserSuiteError(
                f"DTC migration head is {revision!r}, expected {DTC_MIGRATION_HEAD}"
            )
        report["resources"] = {
            "dtc_postgres": {
                "image": DTC_PG_IMAGE,
                "server_version": server_version,
                "migration_head": revision,
                "loopback": True,
                "database_created_for_this_run": True,
            }
        }
        staff_tokens = asyncio.run(
            _seed_staff(state.dtc_url, state.dtc_source, staff_credentials_path)
        )
        redaction_values.extend(staff_tokens.values())
        _require_private_file(staff_credentials_path)

        build_index_sha = _run_node_build(state.dtc_source, logs_dir / "frontend-build.log")
        report["source"]["frontend_dist_index_sha256"] = build_index_sha

        epig_env = {
            name: value for name, value in os.environ.items() if name in ALLOWED_CHILD_ENV and value
        }
        epig_env["NODE_ENV"] = "development"
        epig_info = _start_epig_sandbox(
            state.epig_source, epig_env, state, signal_state=signal_state
        )
        redaction_values.append(epig_info["service_token"])
        assert state.epig_manifest is not None
        mapping = consumer_subject_mapping(epig_info["subjects"])
        report["source"]["subject_mapping_detail"] = mapping
        report["source"]["asset_sha256"] = _sha256_file(epig_info["upload_png"])
        report["resources"]["epig_sandbox"] = {
            "browser_origin": epig_info["browser_origin"],
            "internal_origin": epig_info["internal_origin"],
            "next_pid": state.epig_next_pid,
            "database_server_version": None,
            "service_token_present": True,
        }

        api_port = pick_free_loopback_port()
        api_origin = validate_api_origin(f"http://127.0.0.1:{api_port}")
        api_env = build_child_env(
            state.dtc_source,
            state.dtc_url,
            epig_info["internal_origin"],
            epig_info["service_token"],
        )
        state.api = _start_logged_process(
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.asgi:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--log-level",
                "warning",
            ],
            state.dtc_source,
            api_env,
            logs_dir / "dtc-api.log",
        )
        ready = _wait_for_api_ready(api_origin, state=signal_state)
        capabilities = _read_capabilities(api_origin)
        report["resources"]["dtc_api"] = {
            "origin": api_origin,
            "pid": state.api.process.pid,
            "ready": True,
            "database_dependency": next(
                item for item in ready["dependencies"] if item.get("name") == "database"
            ),
            "capabilities": capabilities,
        }
        report["resources"]["model_calls"] = 0

        worker_env = build_child_env(
            state.dtc_source,
            state.dtc_url,
            epig_info["internal_origin"],
            epig_info["service_token"],
        )
        state.worker = _start_logged_process(
            "worker",
            [sys.executable, "-m", "app.worker", "--markets", "au", "--poll-interval", "0.5"],
            state.dtc_source,
            worker_env,
            logs_dir / "dtc-worker.log",
        )
        time.sleep(1.0)
        if _process_exited(state.worker.process):
            raise BrowserSuiteError("the DTC worker exited during startup")
        report["resources"]["dtc_worker"] = {"pid": state.worker.process.pid, "markets": ["au"]}

        browser_config = {
            "runId": run_id,
            "dtcBaseUrl": api_origin,
            "epigBrowserOrigin": epig_info["browser_origin"],
            "epigInternalOrigin": epig_info["internal_origin"],
            "uploadPngPath": str(epig_info["upload_png"]),
            "lostResponseMarker": LOST_RESPONSE_MARKER,
            "consumers": mapping,
            "staff": {
                "support": staff_tokens["staff_support_au"],
                "approver": staff_tokens["staff_approver_au"],
                "auditor": staff_tokens["staff_auditor_all"],
                "operator": staff_tokens["staff_operator_au"],
            },
            "expected": {
                "consumerOneDescription": expected.consumer_one_description,
                "consumerTwoDescription": expected.consumer_two_description,
                "summaryV1": expected.summary_v1,
                "summaryV2": expected.summary_v2,
                "replyV1": expected.reply_v1,
                "replyV2": expected.reply_v2,
                "supportBSummary": expected.support_b_summary,
                "supportBReply": expected.support_b_reply,
                "supportASummary": expected.support_a_summary,
                "supportAReply": expected.support_a_reply,
                "materialManualSource": expected.material_manual_source,
                "materialManualContent": expected.material_manual_content,
                "materialDeliverySource": expected.material_delivery_source,
                "materialDeliveryContent": expected.material_delivery_content,
            },
            "screenshotsDirectory": str(screenshots_dir),
            "reportPath": str(browser_report_path),
            "browserLifecyclePath": str(browser_lifecycle_path),
            "browserProfileRoot": str(browser_profile_root),
        }
        _write_private_json(browser_config_path, browser_config)
        _require_private_file(browser_config_path)
        browser_env = {
            name: value for name, value in os.environ.items() if name in ALLOWED_CHILD_ENV and value
        }
        browser_env["PLAYWRIGHT_BROWSERS_PATH"] = str(ENGINE_CACHE)
        browser_env["NODE_OPTIONS"] = ""
        browser_env["TMPDIR"] = str(browser_profile_root)
        browser_env["TMP"] = str(browser_profile_root)
        browser_env["TEMP"] = str(browser_profile_root)
        driver_result = _run_browser_driver(
            browser_config_path,
            browser_report_path,
            browser_env,
            log_path=driver_log_path,
            state=signal_state,
        )
        report["resources"]["browser_driver"] = driver_result
        if driver_result["cancelled"]:
            raise BrowserSuiteCancelled(
                "termination requested while the browser driver was running"
            )
        if driver_result["timed_out"]:
            raise BrowserSuiteError("the browser driver exceeded its bounded runtime")
        if driver_result["returncode"] != 0 and not browser_report_path.is_file():
            detail = ""
            if driver_log_path.is_file():
                detail = redact_text(
                    driver_log_path.read_text(encoding="utf-8", errors="replace")[-2000:],
                    redaction_values,
                )
            raise BrowserSuiteError(
                f"the browser driver failed: {detail.strip() or 'no diagnostic output'}"
            )
        browser_report = _load_browser_report(browser_report_path)
        state.browser_report = browser_report
        _merge_browser_report(report, browser_report)
        if driver_result["returncode"] != 0:
            report["status"] = "failed"

        if signal_state.requested:
            raise BrowserSuiteCancelled("termination requested before database verification")
        verification = asyncio.run(
            _verify_databases(
                state.dtc_url,
                str(state.epig_manifest["database"]["url"]),
                expected,
                mapping,
                browser_report,
            )
        )
        report["database_verification"] = verification
        report["resources"]["epig_sandbox"]["database_server_version"] = verification["epig"][
            "server_version"
        ]
        if not verification["ok"]:
            report["status"] = "failed"
            report["errors"].append("per-request PostgreSQL verification failed")
        if report["status"] != "failed":
            report["status"] = "passed"
            exit_code = 0
    except BrowserSuiteCancelled as exc:
        report["status"] = "failed"
        report["errors"].append(redact_text(str(exc), redaction_values))
        exit_code = 128 + (signal_state.signal_number or signal.SIGTERM)
    except BrowserSuiteError as exc:
        report["status"] = "failed"
        report["errors"].append(redact_text(str(exc), redaction_values))
        exit_code = 1
    except Exception as exc:  # noqa: BLE001 - fail closed with a bounded type-only message
        from scripts.run_after_sales_pg import RunnerError

        if isinstance(exc, RunnerError):
            report["errors"].append(redact_text(str(exc), redaction_values))
        else:
            report["errors"].append(f"unexpected {type(exc).__name__}")
        report["status"] = "failed"
        exit_code = 1
    finally:
        cleanup: dict[str, Any] = {}
        secrets_to_hide = redaction_values
        if driver_result is None:
            cleanup["browser_driver"] = {"clean": True, "reason": "not_started"}
            cleanup["browser"] = {"clean": True, "reason": "not_started"}
            cleanup["browser_profile_removed"] = _remove_owned_tree(browser_profile_root, run_dir)
        else:
            cleanup["browser_driver"] = driver_result
            browser_group = _cleanup_browser_group(browser_lifecycle_path)
            cleanup["browser"] = {
                **browser_group,
                "explicit_close": browser_close_status(state.browser_report),
                "driver_group_gone": driver_result["group_gone"],
                "driver_leader_exited": driver_result["leader_exited"],
            }
            cleanup["browser_profile_removed"] = False
            if browser_group.get("clean") is True:
                cleanup["browser_profile_removed"] = _remove_owned_tree(
                    browser_profile_root, run_dir
                )
        cleanup["worker"] = _stop_logged_process(state.worker)
        cleanup["api"] = _stop_logged_process(state.api)
        try:
            cleanup["epig_sandbox"] = _stop_epig_sandbox(state)
        except Exception as exc:  # noqa: BLE001 - cleanup must keep going
            cleanup["epig_sandbox"] = {"clean": False, "error": type(exc).__name__}
        if state.dtc_spec is not None:
            try:
                cleanup["dtc_postgres"] = _remove_dtc_postgres(docker, state.dtc_spec)
            except Exception as exc:  # noqa: BLE001 - cleanup must keep going
                cleanup["dtc_postgres"] = {"clean": False, "error": type(exc).__name__}
        try:
            cleanup["dtc_source_removed"] = _remove_owned_tree(state.dtc_source, run_dir)
            cleanup["epig_source_removed"] = _remove_owned_tree(state.epig_source, run_dir)
        except Exception as exc:  # noqa: BLE001 - cleanup must keep going
            cleanup["source_copy_removed"] = False
            cleanup["source_copy_error"] = type(exc).__name__
        cleanup["report_preserved"] = True
        cleanup["screenshots_preserved"] = screenshots_dir.is_dir()
        try:
            if secrets_dir.exists():
                shutil.rmtree(secrets_dir)
            cleanup["secrets_removed"] = not secrets_dir.exists()
        except Exception:  # noqa: BLE001 - report is still written below
            cleanup["secrets_removed"] = False
        cleanup["clean"] = (
            all(
                bool(item.get("clean", item.get("stopped", False)))
                for item in cleanup.values()
                if isinstance(item, Mapping)
            )
            and cleanup.get("dtc_source_removed") is True
            and cleanup.get("epig_source_removed") is True
            and cleanup.get("browser_profile_removed") is True
            and cleanup.get("screenshots_preserved") is True
            and cleanup.get("secrets_removed") is True
        )
        report["cleanup"] = cleanup
        report["finished_at"] = datetime.now(UTC).isoformat()
        if report["status"] == "running":
            report["status"] = "failed"
        try:
            _write_report(report, report_path)
        except Exception as exc:  # noqa: BLE001 - the summary still needs to be emitted
            print(
                f"dtc after-sales browser suite: FAIL (could not write report: {type(exc).__name__})"
            )
            exit_code = 1
        if cleanup.get("clean") is not True:
            exit_code = 1
        for line in _summary_lines(report, report_path):
            print(redact_text(line, secrets_to_hide))
        restore_signal_handlers(previous_handlers)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
