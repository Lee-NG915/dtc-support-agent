#!/usr/bin/env python3
"""Run the fixed after-sales acceptance set against a disposable PostgreSQL 16.

The runner is deliberately narrow:

* it creates exactly one randomly named container with a random password,
  database and user, published only on ``127.0.0.1`` with a random port;
* readiness is a real ``SELECT 1`` over a short-lived asyncpg connection to
  that explicit loopback target, not ``pg_isready``;
* inherited ``DATABASE_URL``, ``PG*``, ``DTC_TEST_PG_*`` and provider
  credentials are removed or replaced; ``.env`` is never read;
* the pytest subtree runs in its own process group and is terminated
  TERM-then-KILL with bounded waits;
* cleanup removes only this run's container/volume. There is no global prune;
* the generated password and connection URL are never printed, including in
  failure output.

Usage::

    uv run python scripts/run_after_sales_pg.py

The fixed pytest selection lives in :data:`TEST_FILES` and
:data:`REQUIRED_CONCURRENCY_TESTS`; arbitrary test paths, database URLs or
commands are rejected. Plain ``pytest`` continues to use SQLite fixtures and
never starts Docker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "postgres:16-alpine"
LABEL = "dtc.pgtest=1"
RUN_ID_RE = re.compile(r"^[0-9a-f]{16}$")

TEST_FILES = (
    "tests/integration/test_after_sales_workflow.py",
    "tests/integration/test_after_sales_execution.py",
    "tests/integration/test_after_sales_workbench.py",
    "tests/integration/test_after_sales_migrations.py",
    "tests/integration/test_after_sales_pg_concurrency.py",
)
REQUIRED_PG_MODULES = (
    "tests/integration/test_after_sales_workflow.py",
    "tests/integration/test_after_sales_execution.py",
    "tests/integration/test_after_sales_workbench.py",
)
REQUIRED_CONCURRENCY_TESTS = (
    "tests/integration/test_after_sales_pg_concurrency.py::test_two_processes_claim_exactly_one_owner",
    "tests/integration/test_after_sales_pg_concurrency.py::"
    "test_expired_lease_reclaim_fences_stale_epoch",
    "tests/integration/test_after_sales_pg_concurrency.py::"
    "test_crashed_lease_is_persisted_and_reclaimable",
)
REQUIRED_PG_MIGRATION_TEST = (
    "tests/integration/test_after_sales_migrations.py::test_dedicated_postgres_migration_target"
)
MIN_SQLITE_MIGRATION_PASSES = 3

CREATE_TIMEOUT = 120.0
PORT_TIMEOUT = 15.0
PROBE_TIMEOUT = 3.0
PROBE_RETRY_INTERVAL = 0.25
PROBE_HOST = "127.0.0.1"
READY_TIMEOUT = 90.0
STATEMENT_TIMEOUT = "60s"
LOCK_TIMEOUT = "15s"
PYTEST_TIMEOUT = 1500.0
REMOVE_TIMEOUT = 20.0
GROUP_GRACE = 15.0

EXACT_FORBIDDEN_ENV = {
    "DATABASE_URL",
    "DTC_TEST_PG_URL",
    "ALEMBIC_DATABASE_URL",
    "PYTHONPATH",
    "DEEPSEEK_API_KEY",
    "EMBEDDING_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "EPIG_SERVICE_TOKEN",
    "ADMIN_API_TOKEN",
}
FORBIDDEN_ENV_PREFIXES = (
    "PG",
    "DTC_TEST_PG_",
    "DEEPSEEK_",
    "EMBEDDING_",
    "OPENAI_",
    "ANTHROPIC_",
    "EPIG_",
)
SAFE_TEST_ENV = {
    "APP_ENV": "testing",
    "LOG_LEVEL": "WARNING",
    "DEEPSEEK_API_KEY": "",
    "EMBEDDING_API_KEY": "",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "EPIG_BASE_URL": "",
    "EPIG_SERVICE_TOKEN": "",
    "EPIG_ALLOWED_MARKETS": "",
    "ADMIN_API_TOKEN": "",
    "REDIS_URL": "",
    "QDRANT_URL": "",
    "LOCAL_EMBEDDING_FORCE": "false",
    "CROSS_ENCODER_FORCE": "false",
    "UNSTRUCTURED_FORCE": "false",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}


class RunnerError(RuntimeError):
    """A safe, already-redactable runner failure."""


class RunnerCancelled(RunnerError):
    """A termination signal arrived while the runner owned resources."""


@dataclass(frozen=True)
class ContainerSpec:
    run_id: str
    name: str
    database: str
    username: str
    password: str = field(repr=False)
    label: str = LABEL


@dataclass
class SignalState:
    requested: bool = False
    signal_number: int | None = None

    def request(self, signum: int) -> None:
        self.requested = True
        self.signal_number = signum


@dataclass(frozen=True)
class PytestResult:
    returncode: int
    output: str
    timed_out: bool
    group_gone: bool
    group_lingered: bool = False


def build_spec() -> ContainerSpec:
    run_id = secrets.token_hex(8)
    # PostgreSQL identifiers are limited to 63 bytes; this stays well below.
    return ContainerSpec(
        run_id=run_id,
        name=f"dtc-pgtest-{run_id}",
        database=f"dtc_pgtest_{run_id}",
        username=f"dtc_pgtest_{run_id}",
        password=secrets.token_urlsafe(24),
    )


def target_url(spec: ContainerSpec, port: int) -> str:
    return (
        "postgresql+asyncpg://"
        f"{quote(spec.username, safe='')}:{quote(spec.password, safe='')}"
        f"@127.0.0.1:{port}/{quote(spec.database, safe='')}"
    )


@dataclass(frozen=True)
class TargetInfo:
    username: str
    database: str
    host: str
    port: int


def validate_target_url(url: str, run_id: str) -> TargetInfo:
    """Reject anything that is not this run's loopback disposable database."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise RunnerError("invalid DTC_TEST_PG_RUN_ID")
    parts = urlsplit(url)
    if parts.scheme != "postgresql+asyncpg":
        raise RunnerError("DTC_TEST_PG_URL must use postgresql+asyncpg")
    if parts.hostname != "127.0.0.1":
        raise RunnerError("DTC_TEST_PG_URL must target 127.0.0.1")
    try:
        port = parts.port
    except ValueError as exc:
        raise RunnerError("DTC_TEST_PG_URL has an invalid port") from exc
    if port is None or not (1 <= port <= 65535):
        raise RunnerError("DTC_TEST_PG_URL must include a numeric port")
    expected_user = f"dtc_pgtest_{run_id}"
    expected_database = f"dtc_pgtest_{run_id}"
    if unquote(parts.username or "") != expected_user:
        raise RunnerError("DTC_TEST_PG_URL user does not belong to this run")
    if parts.password is None or not parts.password:
        raise RunnerError("DTC_TEST_PG_URL must include the generated password")
    if unquote(parts.path.lstrip("/")) != expected_database:
        raise RunnerError("DTC_TEST_PG_URL database does not belong to this run")
    return TargetInfo(
        username=expected_user,
        database=expected_database,
        host="127.0.0.1",
        port=port,
    )


def redact(text: str, spec: ContainerSpec, url: str | None = None) -> str:
    """Remove generated secrets and any PostgreSQL URL from captured output."""
    values = [spec.password, quote(spec.password, safe=""), url or ""]
    redacted = text
    for value in values:
        if value:
            redacted = redacted.replace(value, "[redacted]")
    redacted = re.sub(
        r"postgres(?:ql)?(?:\+asyncpg)?://[^\s\"'<>]+",
        "postgresql+asyncpg://[redacted]",
        redacted,
    )
    redacted = re.sub(r"(?i)(pgpassword\s*=\s*)\S+", r"\1[redacted]", redacted)
    redacted = re.sub(r"(?i)(password\s*[=:]\s*)\S+", r"\1[redacted]", redacted)
    return redacted


def build_test_env(
    base: Mapping[str, str],
    spec: ContainerSpec,
    url: str,
    report_path: Path,
) -> dict[str, str]:
    """Build a child environment with no inherited database or provider config."""
    env: dict[str, str] = {}
    for key, value in base.items():
        if key in EXACT_FORBIDDEN_ENV:
            continue
        if key.upper().startswith(FORBIDDEN_ENV_PREFIXES):
            continue
        env[key] = value
    env.update(SAFE_TEST_ENV)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "DATABASE_URL": url,
            "DTC_TEST_PG_URL": url,
            "DTC_TEST_PG_RUN_ID": spec.run_id,
            "DTC_TEST_PG_REPORT": str(report_path),
        }
    )
    return env


def build_pytest_command() -> list[str]:
    """Return the fixed selection; callers cannot append arbitrary targets."""
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "tests.integration.pg_plugin",
        "--tb=short",
        "--disable-warnings",
        "--color=no",
        *TEST_FILES,
    ]


def _remaining(deadline: float) -> float:
    """Return the time left before ``deadline`` or fail once it is exhausted."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RunnerError("postgres probe timed out")
    return remaining


async def _probe_database(spec: ContainerSpec, port: int, *, timeout: float) -> str:
    """Probe one explicit loopback target over a single short-lived connection.

    Connect, queries and close all draw from one deadline, so a slow phase
    cannot silently extend the attempt budget.
    """
    deadline = time.monotonic() + timeout
    connection: asyncpg.Connection | None = None
    try:
        connection = await asyncpg.connect(
            host=PROBE_HOST,
            port=port,
            user=spec.username,
            password=spec.password,
            database=spec.database,
            ssl="disable",
            timeout=_remaining(deadline),
            server_settings={"application_name": "dtc_pgtest_probe"},
        )
        row = await connection.fetchrow(
            "SELECT 1 AS ready, current_setting('server_version') AS server_version",
            timeout=_remaining(deadline),
        )
        if row is None or row["ready"] != 1:
            raise RunnerError("target database SELECT 1 did not return a ready row")
        # Bound only this run's disposable database; never server-wide.
        quoted = '"' + spec.database.replace('"', '""') + '"'
        for statement in (
            f"ALTER DATABASE {quoted} SET statement_timeout = '{STATEMENT_TIMEOUT}'",
            f"ALTER DATABASE {quoted} SET lock_timeout = '{LOCK_TIMEOUT}'",
        ):
            await connection.execute(statement, timeout=_remaining(deadline))
        return str(row["server_version"])
    finally:
        if connection is not None and not connection.is_closed():
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    await connection.close(timeout=remaining)
                except Exception:  # noqa: BLE001 - abort the socket instead
                    pass
            if not connection.is_closed():
                connection.terminate()


def _probe_once(spec: ContainerSpec, port: int, *, timeout: float = PROBE_TIMEOUT) -> str:
    """Run one asyncpg probe and expose only a safe failure type."""
    try:
        return asyncio.run(_probe_database(spec, port, timeout=timeout))
    except RunnerError:
        raise
    except Exception as exc:
        raise RunnerError(f"postgres probe failed: {type(exc).__name__}") from exc


def wait_for_database(
    spec: ContainerSpec,
    port: int,
    *,
    state: SignalState,
    timeout: float = READY_TIMEOUT,
) -> str:
    """Require a real ``SELECT 1`` and return the server version."""
    deadline = time.monotonic() + timeout
    last_error: RunnerError | None = None
    while time.monotonic() < deadline:
        if state.requested:
            raise RunnerCancelled("termination requested during database readiness")
        remaining = deadline - time.monotonic()
        try:
            return _probe_once(spec, port, timeout=min(PROBE_TIMEOUT, remaining))
        except RunnerError as exc:
            last_error = exc
        time.sleep(min(PROBE_RETRY_INTERVAL, max(0.0, deadline - time.monotonic())))
    detail = f" ({last_error})" if last_error is not None else ""
    raise RunnerError(f"target database SELECT 1 readiness timed out{detail}")


def _wait_group_gone(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.05)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # macOS can report EPERM for a not-yet-reaped zombie group leader.
        return False
    return False


def _signal_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        # The leader may still be a zombie; the caller reaps it and retries.
        pass


def _terminate_group(pgid: int, *, grace: float = GROUP_GRACE) -> bool:
    if _wait_group_gone(pgid, 0.01):
        return True
    _signal_group(pgid, signal.SIGTERM)
    if _wait_group_gone(pgid, grace):
        return True
    _signal_group(pgid, signal.SIGKILL)
    return _wait_group_gone(pgid, min(grace, 5.0))


def run_pytest_isolated(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    state: SignalState,
    timeout: float = PYTEST_TIMEOUT,
    group_grace: float = GROUP_GRACE,
) -> PytestResult:
    """Run pytest in its own process group and require the whole group to exit."""
    if state.requested:
        raise RunnerCancelled("termination requested before pytest")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise RunnerError(f"could not start pytest: {type(exc).__name__}") from exc
    timed_out = False
    output = ""
    deadline = time.monotonic() + timeout
    while True:
        if state.requested:
            _signal_group(process.pid, signal.SIGTERM)
            try:
                output, _ = process.communicate(timeout=group_grace)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    output, _ = process.communicate(timeout=5.0)
                except subprocess.TimeoutExpired:
                    output = ""
            _terminate_group(process.pid, grace=group_grace)
            raise RunnerCancelled("termination requested while pytest was running")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            output, _ = process.communicate(timeout=min(0.25, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    if timed_out:
        # Signal first, then reap the leader so a zombie group leader cannot
        # make the liveness check return EPERM, then clear any children.
        _signal_group(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=group_grace)
        except subprocess.TimeoutExpired:
            _signal_group(process.pid, signal.SIGKILL)
            process.kill()
            try:
                output, _ = process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                output = ""
        if not _wait_group_gone(process.pid, 1.0):
            _terminate_group(process.pid, grace=group_grace)
    group_gone = _wait_group_gone(process.pid, group_grace)
    group_lingered = not group_gone
    if group_lingered:
        # The leader may have exited while a child kept the session alive.
        group_gone = _terminate_group(process.pid, grace=group_grace)
    return PytestResult(
        returncode=process.returncode if process.returncode is not None else -1,
        output=output or "",
        timed_out=timed_out,
        group_gone=group_gone,
        group_lingered=group_lingered,
    )


class DockerOps:
    """Small, testable wrapper around the docker CLI."""

    def __init__(self, docker: str | None = None) -> None:
        self._docker = docker or shutil.which("docker") or "docker"

    def _run(self, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self._docker, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RunnerError("docker executable is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"docker {args[0]} timed out") from exc

    def require_available(self) -> None:
        result = self._run("version", "--format", "{{.Server.Version}}", timeout=15.0)
        if result.returncode != 0:
            raise RunnerError("docker is required for this command")

    def run_container(self, spec: ContainerSpec) -> None:
        command = [
            "run",
            "-d",
            "--name",
            spec.name,
            "--label",
            spec.label,
            "-p",
            "127.0.0.1::5432",
            "-e",
            f"POSTGRES_USER={spec.username}",
            "-e",
            f"POSTGRES_DB={spec.database}",
            "-e",
            f"POSTGRES_PASSWORD={spec.password}",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
            IMAGE,
        ]
        # Always wait for the create request to settle, even after SIGTERM.
        # Cleaning up while the daemon may still create the container leaks it.
        result = self._run(*command, timeout=CREATE_TIMEOUT)
        if result.returncode != 0:
            raise RunnerError("dedicated container creation failed")

    def published_port(self, name: str) -> int:
        result = self._run("port", name, "5432/tcp", timeout=PORT_TIMEOUT)
        if result.returncode != 0:
            raise RunnerError("could not read the published PostgreSQL port")
        text = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        host, _, port_text = text.rpartition(":")
        if host not in {"127.0.0.1", "localhost"} or not port_text.isdigit():
            raise RunnerError("published port is not bound to loopback")
        port = int(port_text)
        if not (1 <= port <= 65535):
            raise RunnerError("published port is invalid")
        return port

    def inspect_exists(self, name: str) -> bool:
        result = self._run("inspect", name, timeout=REMOVE_TIMEOUT)
        return result.returncode == 0

    def remove(self, name: str) -> bool:
        """Remove only this run's container and anonymous volumes."""
        result = self._run("rm", "-f", "-v", name, timeout=REMOVE_TIMEOUT)
        if result.returncode == 0:
            return True
        combined = f"{result.stdout}\n{result.stderr}".lower()
        return "no such container" in combined


def install_signal_handlers(state: SignalState) -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        state.request(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handler)
    return previous


def restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _report_failure(report: dict[str, Any], result: PytestResult) -> list[str]:
    problems: list[str] = []
    if result.timed_out:
        problems.append("pytest subtree timed out")
    if not result.group_gone:
        problems.append("pytest process group did not exit")
    if result.group_lingered:
        problems.append("pytest leader exited while its process group was still alive")
    if result.returncode != 0:
        problems.append(f"pytest exited with {result.returncode}")
    configured = report.get("pg_configured")
    if configured is not True:
        problems.append("pytest plugin was not configured for PostgreSQL")
    counts = report.get("counts") or {}
    for category in ("pg_adapted", "pg_concurrency", "pg_migration"):
        item = counts.get(category) or {}
        if int(item.get("failed", 0)) or int(item.get("skipped", 0)):
            problems.append(f"{category} has failed or skipped tests")
    for module in REQUIRED_PG_MODULES:
        module_counts = (report.get("module_counts") or {}).get(module) or {}
        if int(module_counts.get("passed", 0)) < 1:
            problems.append(f"no passing tests for {module}")
    adapted = counts.get("pg_adapted") or {}
    if int(adapted.get("passed", 0)) < 1:
        problems.append("no PostgreSQL-adapted tests ran")
    probes = report.get("probes") or []
    if not probes:
        problems.append("no live PostgreSQL engine probes ran")
    probed_modules = {str(probe.get("module")) for probe in probes}
    for module in REQUIRED_PG_MODULES:
        if module not in probed_modules:
            problems.append(f"no live PostgreSQL probe for {module}")
    for probe in probes:
        if probe.get("ok") is not True:
            problems.append("a live PostgreSQL engine probe failed")
        if probe.get("dialect") != "postgresql":
            problems.append("an app engine is not PostgreSQL")
    passed_nodes = set(report.get("passed_nodeids") or [])
    for nodeid in REQUIRED_CONCURRENCY_TESTS:
        if nodeid not in passed_nodes:
            problems.append(f"required concurrency test did not pass: {nodeid.split('::')[-1]}")
    if REQUIRED_PG_MIGRATION_TEST not in passed_nodes:
        problems.append("required PostgreSQL migration test did not pass")
    sqlite_counts = counts.get("sqlite_migration") or {}
    if int(sqlite_counts.get("passed", 0)) < MIN_SQLITE_MIGRATION_PASSES:
        problems.append("the SQLite migration tests did not all run")
    migration = report.get("migration") or {}
    if migration.get("head") not in migration.get("observed_revisions", []):
        problems.append("per-test database did not reach the Alembic head")
    if "0001" not in (migration.get("history") or []):
        problems.append("Alembic history does not include 0001")
    return list(dict.fromkeys(problems))


def _print_summary(
    report: dict[str, Any],
    *,
    version: str,
    result: PytestResult,
    removed: bool,
) -> None:
    counts = report.get("counts") or {}
    probes = report.get("probes") or []
    migration = report.get("migration") or {}
    observations = report.get("observations") or []

    def line(name: str) -> str:
        item = counts.get(name) or {}
        return (
            f"passed={int(item.get('passed', 0))} "
            f"failed={int(item.get('failed', 0))} "
            f"skipped={int(item.get('skipped', 0))}"
        )

    backend_pids = sorted(
        {int(probe["backend_pid"]) for probe in probes if isinstance(probe.get("backend_pid"), int)}
    )
    print("dtc pg runner: PASS")
    print(f"  postgres_version={version} image={IMAGE}")
    print(f"  pg_adapted={line('pg_adapted')}")
    print(f"  live_probes={len(probes)} distinct_backend_pids={len(backend_pids)}")
    print(f"  pg_concurrency={line('pg_concurrency')}")
    print(f"  pg_migration={line('pg_migration')} sqlite_migration={line('sqlite_migration')}")
    print(
        "  migrations: "
        f"head={migration.get('head')} "
        f"observed={sorted(set(migration.get('observed_revisions') or []))} "
        f"history={migration.get('history')}"
    )
    for observation in observations:
        print(
            "  concurrency_observation: "
            f"os_pids={observation.get('os_pids')} "
            f"pg_backend_pids={observation.get('pg_backend_pids')} "
            f"owners={observation.get('owners')} "
            f"epochs={observation.get('epochs')} "
            f"exactly_one={observation.get('exactly_one')}"
        )
    print(f"  pytest_exit={result.returncode} timed_out={result.timed_out}")
    print(f"  container_removed={removed}")
    print(
        "  bounds: "
        f"create={CREATE_TIMEOUT}s ready={READY_TIMEOUT}s "
        f"probe={PROBE_TIMEOUT}s pytest={PYTEST_TIMEOUT}s remove={REMOVE_TIMEOUT}s "
        f"db_statement_timeout={STATEMENT_TIMEOUT} db_lock_timeout={LOCK_TIMEOUT}"
    )


def execute(
    *,
    spec: ContainerSpec | None = None,
    deps: DockerOps | None = None,
    probe: Callable[..., str] = wait_for_database,
    pytest_runner: Callable[..., PytestResult] = run_pytest_isolated,
    state: SignalState | None = None,
    install_signals: bool = True,
    env_base: Mapping[str, str] | None = None,
) -> int:
    """Provision, verify, test and clean up one disposable PostgreSQL database."""
    spec = spec or build_spec()
    docker = deps or DockerOps()
    signal_state = state or SignalState()
    previous_handlers = install_signal_handlers(signal_state) if install_signals else {}
    temp_dir = Path(tempfile.mkdtemp(prefix="dtc-pgtest-"))
    report_path = temp_dir / "report.json"
    removed = False
    exit_code = 1
    url: str | None = None
    summary: tuple[dict[str, Any], str, PytestResult] | None = None
    try:
        docker.require_available()
        docker.run_container(spec)
        if signal_state.requested:
            raise RunnerCancelled("termination requested during container creation")
        port = docker.published_port(spec.name)
        url = target_url(spec, port)
        info = validate_target_url(url, spec.run_id)
        if (info.username, info.database) != (spec.username, spec.database):
            raise RunnerError("generated target does not match this run")
        version = probe(spec, port, state=signal_state)
        if signal_state.requested:
            raise RunnerCancelled("termination requested before pytest")
        env = build_test_env(
            env_base if env_base is not None else os.environ, spec, url, report_path
        )
        result = pytest_runner(
            build_pytest_command(),
            cwd=ROOT,
            env=env,
            state=signal_state,
        )
        if not report_path.is_file():
            raise RunnerError("pytest plugin did not produce a PostgreSQL report")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        problems = _report_failure(report, result)
        if problems:
            raise RunnerError("; ".join(problems))
        if signal_state.requested:
            raise RunnerCancelled("termination requested before completion")
        exit_code = 0
        summary = (report, version, result)
        sanitized = redact(result.output, spec, url).strip()
        if sanitized:
            print(sanitized)
    except RunnerCancelled as exc:
        exit_code = 128 + (signal_state.signal_number or signal.SIGTERM)
        print(f"dtc pg runner: CANCELLED: {redact(str(exc), spec, url)}")
    except RunnerError as exc:
        exit_code = 1
        print(f"dtc pg runner: FAIL: {redact(str(exc), spec, url)}")
        if "result" in locals() and isinstance(locals()["result"], PytestResult):
            output = redact(locals()["result"].output, spec, url)
            tail = "\n".join(output.strip().splitlines()[-40:])
            if tail:
                print("  sanitized pytest tail:")
                for line in tail.splitlines():
                    print(f"    {line}")
    except Exception as exc:  # noqa: BLE001 - never print generated secrets
        exit_code = 1
        print(f"dtc pg runner: FAIL: unexpected {type(exc).__name__}")
    finally:
        # Only this run's randomly named container/volume is ever removed.
        try:
            removed = docker.remove(spec.name)
        except RunnerError:
            removed = False
        if removed and docker.inspect_exists(spec.name):
            removed = False
        try:
            restore_signal_handlers(previous_handlers)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    if exit_code == 0 and not removed:
        print("dtc pg runner: FAIL: dedicated container cleanup could not be confirmed")
        return 1
    if exit_code == 0 and summary is not None:
        report, version, result = summary
        _print_summary(report, version=version, result=result, removed=removed)
    elif exit_code != 0:
        print(f"  container_removed={removed}")
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_after_sales_pg.py",
        description=(
            "Run the fixed after-sales acceptance selection against one disposable "
            "PostgreSQL 16 container. No arbitrary test paths, URLs or commands."
        ),
    )
    # Intentionally no test-path/URL passthrough arguments.
    return parser


def main(argv: list[str] | None = None) -> int:
    _parser().parse_args(argv)
    return execute()


if __name__ == "__main__":
    raise SystemExit(main())
