"""Safety and lifecycle regression tests for the dedicated PostgreSQL runner.

These tests never start Docker. They exercise the runner's own env scrubbing,
redaction, target validation, fixed test selection, cancellation, timeout and
cleanup paths with controlled fakes plus real subprocess-group cases, including
the PG child startup contract shared with the concurrency tests.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.integration import test_after_sales_pg_concurrency as pg_concurrency
from tests.integration.test_after_sales_pg_concurrency import (
    CHILD_POPEN_OPTIONS,
    _spawn,
)

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts" / "run_after_sales_pg.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location("dtc_run_after_sales_pg", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> Any:
    return _load_runner()


def _valid_report(runner: Any) -> dict[str, Any]:
    return {
        "pg_configured": True,
        "counts": {
            "pg_adapted": {
                "passed": len(runner.REQUIRED_PG_MODULES),
                "failed": 0,
                "skipped": 0,
            },
            "pg_concurrency": {"passed": 3, "failed": 0, "skipped": 0},
            "pg_migration": {"passed": 1, "failed": 0, "skipped": 0},
            "sqlite_migration": {"passed": 3, "failed": 0, "skipped": 0},
        },
        "module_counts": {
            module: {"passed": 1, "failed": 0, "skipped": 0}
            for module in runner.REQUIRED_PG_MODULES
        },
        "passed_nodeids": [
            *runner.REQUIRED_CONCURRENCY_TESTS,
            runner.REQUIRED_PG_MIGRATION_TEST,
        ],
        "probes": [
            {"module": module, "ok": True, "dialect": "postgresql"}
            for module in runner.REQUIRED_PG_MODULES
        ],
        "observations": [],
        "migration": {
            "head": "0005",
            "history": ["0001", "0002", "0003", "0004", "0005"],
            "observed_revisions": ["0005"],
        },
    }


class _FakeDocker:
    def __init__(self, *, remove_ok: bool = True) -> None:
        self.events: list[str] = []
        self.remove_ok = remove_ok
        self.removed_names: list[str] = []

    def require_available(self) -> None:
        self.events.append("available")

    def run_container(self, spec: Any) -> None:
        self.events.append("create")

    def published_port(self, name: str) -> int:
        self.events.append("port")
        return 55432

    def inspect_exists(self, name: str) -> bool:
        self.events.append("inspect")
        return False

    def remove(self, name: str) -> bool:
        self.events.append("remove")
        self.removed_names.append(name)
        return self.remove_ok


def _base_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "HOME": "/tmp",
        "DATABASE_URL": "postgresql://real-user:real-pass@db.example.com/prod",
        "DTC_TEST_PG_URL": "postgresql://real-user:real-pass@db.example.com/prod",
        "ALEMBIC_DATABASE_URL": "postgresql://real-user:real-pass@db.example.com/prod",
        "PGHOST": "db.example.com",
        "PGPASSWORD": "real-pass",
        "PGPORT": "5432",
        "DEEPSEEK_API_KEY": "paid",
        "EMBEDDING_API_KEY": "paid",
        "OPENAI_API_KEY": "paid",
        "ANTHROPIC_API_KEY": "paid",
        "EPIG_SERVICE_TOKEN": "real-token",
        "ADMIN_API_TOKEN": "real-token",
        "PYTHONPATH": "/tmp/not-this-repo:/tmp",
    }


def test_build_test_env_scrubs_inherited_databases_and_keys(runner: Any) -> None:
    spec = runner.build_spec()
    url = runner.target_url(spec, 55432)
    env = runner.build_test_env(_base_env(), spec, url, Path("/tmp/report.json"))

    assert env["DATABASE_URL"] == url
    assert env["DTC_TEST_PG_URL"] == url
    assert env["DTC_TEST_PG_RUN_ID"] == spec.run_id
    assert env["PYTHONPATH"] == str(ROOT)
    assert "/tmp/not-this-repo" not in env["PYTHONPATH"]
    for forbidden in ("PGHOST", "PGPASSWORD", "PGPORT", "ALEMBIC_DATABASE_URL"):
        assert forbidden not in env
    for cleared in (
        "DEEPSEEK_API_KEY",
        "EMBEDDING_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "EPIG_SERVICE_TOKEN",
        "ADMIN_API_TOKEN",
    ):
        assert env[cleared] == ""
    assert env["APP_ENV"] == "testing"


def test_redact_never_emits_password_or_url(runner: Any) -> None:
    spec = runner.build_spec()
    url = runner.target_url(spec, 55432)
    text = (
        f"connection failed url={url} password={spec.password} "
        f"PGPASSWORD={spec.password} password: {spec.password}"
    )
    sanitized = runner.redact(text, spec, url)
    assert spec.password not in sanitized
    assert url not in sanitized
    assert "postgresql+asyncpg://[redacted]" in sanitized


def test_validate_target_url_only_accepts_this_runs_loopback_database(runner: Any) -> None:
    spec = runner.build_spec()
    accepted = runner.validate_target_url(runner.target_url(spec, 55432), spec.run_id)
    assert (accepted.username, accepted.database, accepted.host, accepted.port) == (
        spec.username,
        spec.database,
        "127.0.0.1",
        55432,
    )
    for rejected in (
        f"postgresql+asyncpg://{spec.username}:{spec.password}@db.example.com:5432/{spec.database}",
        f"postgresql+asyncpg://{spec.username}:{spec.password}@127.0.0.1:5432/other_db",
        f"sqlite+aiosqlite:///{spec.database}",
    ):
        with pytest.raises(runner.RunnerError):
            runner.validate_target_url(rejected, spec.run_id)
    with pytest.raises(runner.RunnerError):
        runner.validate_target_url(runner.target_url(spec, 55432), "not-a-run-id")


def test_container_spec_is_random_and_prefixed(runner: Any) -> None:
    first = runner.build_spec()
    second = runner.build_spec()
    assert first.run_id != second.run_id
    assert first.name.startswith("dtc-pgtest-")
    assert first.username == first.database == f"dtc_pgtest_{first.run_id}"
    assert len(first.password) >= 20
    assert len(first.name) < 64


def test_docker_commands_use_loopback_random_port_and_never_prune(runner: Any) -> None:
    spec = runner.build_spec()
    docker = runner.DockerOps("docker")
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(["docker", *args], 0, "", "")

    docker._run = fake_run  # type: ignore[method-assign]
    docker.run_container(spec)
    create = calls[0]
    assert create[:2] == ("run", "-d")
    assert create[create.index("--name") + 1] == spec.name
    assert create[create.index("--label") + 1] == spec.label
    assert "127.0.0.1::5432" in create
    assert f"POSTGRES_PASSWORD={spec.password}" in create
    assert create[-1] == runner.IMAGE

    calls.clear()
    assert docker.remove(spec.name) is True
    assert calls[0] == ("rm", "-f", "-v", spec.name)
    assert "prune" not in calls[0]


def _port_docker(
    runner: Any, results: list[tuple[int, str, str]]
) -> tuple[Any, list[tuple[str, ...]]]:
    docker = runner.DockerOps("docker")
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        returncode, stdout, stderr = results.pop(0)
        return subprocess.CompletedProcess(["docker", *args], returncode, stdout, stderr)

    docker._run = fake_run  # type: ignore[method-assign]
    return docker, calls


def test_published_port_retries_a_successful_but_empty_binding(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, calls = _port_docker(
        runner,
        [
            (0, "", ""),
            (0, "127.0.0.1:55432\n", ""),
        ],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert docker.published_port("dtc-pgtest-test") == 55432
    assert len(calls) == 2
    assert sleeps == [runner.PORT_RETRY_INTERVAL]


def test_published_port_persistent_empty_binding_fails_after_bounded_attempts(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, calls = _port_docker(
        runner,
        [(0, "", "")] * runner.PORT_RETRY_ATTEMPTS,
    )
    sleeps: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(runner.RunnerError, match="not bound"):
        docker.published_port("dtc-pgtest-test")
    assert len(calls) == runner.PORT_RETRY_ATTEMPTS
    assert sleeps == [runner.PORT_RETRY_INTERVAL] * (runner.PORT_RETRY_ATTEMPTS - 1)


@pytest.mark.parametrize(
    "output",
    [
        "0.0.0.0:55432\n",
        "127.0.0.1:55432\n0.0.0.0:55432\n",
        "127.0.0.1:55432\n[::]:55432\n",
    ],
)
def test_published_port_rejects_any_non_loopback_binding_without_retry(
    runner: Any, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    docker, calls = _port_docker(runner, [(0, output, "")])
    sleeps: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(runner.RunnerError, match="loopback"):
        docker.published_port("dtc-pgtest-test")
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "output",
    ["127.0.0.1:notaport", "127.0.0.1:0", "127.0.0.1:70000", "127.0.0.1:"],
)
def test_published_port_rejects_an_invalid_port_without_retry(
    runner: Any, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    docker, calls = _port_docker(runner, [(0, output + "\n", "")])
    sleeps: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(runner.RunnerError, match="invalid"):
        docker.published_port("dtc-pgtest-test")
    assert len(calls) == 1
    assert sleeps == []


def test_published_port_command_failure_is_immediate_and_never_echoes_output(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, calls = _port_docker(
        runner,
        [(1, "127.0.0.1:55432", "raw docker failure text")],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(runner.RunnerError, match="could not read") as error:
        docker.published_port("dtc-pgtest-test")
    assert len(calls) == 1
    assert sleeps == []
    assert "raw docker failure text" not in str(error.value)


def test_published_port_keeps_a_valid_multi_line_binding(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, calls = _port_docker(
        runner,
        [(0, "127.0.0.1:55432\nlocalhost:55433\n", "")],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert docker.published_port("dtc-pgtest-test") == 55432
    assert len(calls) == 1
    assert sleeps == []


def test_missing_docker_fails_instead_of_skipping(runner: Any) -> None:
    class MissingDocker(_FakeDocker):
        def require_available(self) -> None:
            raise runner.RunnerError("docker is required for this command")

    docker = MissingDocker()
    code = runner.execute(
        spec=runner.build_spec(),
        deps=docker,
        probe=lambda *args, **kwargs: pytest.fail("probe must not run"),
        pytest_runner=lambda *args, **kwargs: pytest.fail("pytest must not run"),
        state=runner.SignalState(),
        install_signals=False,
        env_base=_base_env(),
    )
    assert code == 1
    assert docker.removed_names


def test_wait_for_database_has_a_bounded_overall_timeout(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt_timeouts: list[float] = []

    def failing_probe(*args: Any, timeout: float, **kwargs: Any) -> str:
        attempt_timeouts.append(timeout)
        raise runner.RunnerError("probe failed")

    monkeypatch.setattr(runner, "_probe_once", failing_probe)
    started = time.monotonic()
    with pytest.raises(runner.RunnerError, match="readiness timed out"):
        runner.wait_for_database(runner.build_spec(), 1, state=runner.SignalState(), timeout=0.2)
    assert time.monotonic() - started < 2.0
    assert attempt_timeouts
    assert all(value <= runner.PROBE_TIMEOUT for value in attempt_timeouts)


def test_wait_for_database_stops_when_cancelled(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = runner.SignalState()
    calls = 0

    def cancel_then_fail(*args: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        state.request(signal.SIGINT)
        raise runner.RunnerError("still starting")

    monkeypatch.setattr(runner, "_probe_once", cancel_then_fail)
    with pytest.raises(runner.RunnerCancelled):
        runner.wait_for_database(runner.build_spec(), 1, state=state, timeout=5.0)
    assert calls == 1


class _FakeProbeConnection:
    """The asyncpg connection surface used by the probe, with timing records."""

    def __init__(self, *, delay: float = 0.0, fetch_error: Exception | None = None) -> None:
        self.delay = delay
        self.fetch_error = fetch_error
        self.fetch_timeouts: list[float] = []
        self.execute_timeouts: list[float] = []
        self.statements: list[str] = []
        self.close_timeouts: list[float] = []
        self.close_calls = 0
        self.closed = False
        self.terminated = False

    async def fetchrow(self, query: str, *, timeout: float) -> dict[str, Any]:
        self.fetch_timeouts.append(timeout)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fetch_error is not None:
            raise self.fetch_error
        return {"ready": 1, "server_version": "16.9"}

    async def execute(self, statement: str, *, timeout: float) -> None:
        self.statements.append(statement)
        self.execute_timeouts.append(timeout)
        if self.delay:
            await asyncio.sleep(self.delay)

    async def close(self, *, timeout: float | None = None) -> None:
        self.close_calls += 1
        self.close_timeouts.append(timeout or 0.0)
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed

    def terminate(self) -> None:
        self.terminated = True
        self.closed = True


def _install_fake_connect(
    runner: Any,
    monkeypatch: pytest.MonkeyPatch,
    connection: _FakeProbeConnection,
    captured: dict[str, Any],
) -> None:
    async def fake_connect(**kwargs: Any) -> _FakeProbeConnection:
        captured.update(kwargs)
        return connection

    monkeypatch.setattr(runner.asyncpg, "connect", fake_connect)


def test_probe_connects_only_to_the_explicit_loopback_target(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = runner.build_spec()
    connection = _FakeProbeConnection()
    captured: dict[str, Any] = {}
    _install_fake_connect(runner, monkeypatch, connection, captured)
    for name, value in {
        "PGHOST": "db.example.com",
        "PGPORT": "5432",
        "PGUSER": "prod-user",
        "PGPASSWORD": "prod-password",
        "PGDATABASE": "prod-db",
        "DATABASE_URL": "postgresql://prod-user:prod-password@db.example.com/prod-db",
    }.items():
        monkeypatch.setenv(name, value)

    version = runner._probe_once(spec, 55432, timeout=2.0)

    assert version == "16.9"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 55432
    assert captured["user"] == spec.username
    assert captured["password"] == spec.password
    assert captured["database"] == spec.database
    assert captured["ssl"] == "disable"
    assert captured.get("dsn") is None
    assert connection.closed is True
    assert connection.terminated is False
    assert len(connection.statements) == 2
    assert all(spec.database in statement for statement in connection.statements)
    assert not any("ALTER SYSTEM" in statement for statement in connection.statements)


def test_probe_operations_draw_from_one_deadline(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = runner.build_spec()
    connection = _FakeProbeConnection(delay=0.1)
    captured: dict[str, Any] = {}
    _install_fake_connect(runner, monkeypatch, connection, captured)

    assert runner._probe_once(spec, 55432, timeout=1.0) == "16.9"

    fetch_timeout = connection.fetch_timeouts[0]
    assert 0.0 < fetch_timeout <= 1.0
    assert len(connection.execute_timeouts) == 2
    # Each phase receives what is left of the same budget, not a fresh one.
    assert connection.execute_timeouts[0] <= fetch_timeout - 0.05
    assert connection.execute_timeouts[1] <= connection.execute_timeouts[0] - 0.05
    assert connection.close_timeouts[0] <= connection.execute_timeouts[1]


def test_probe_failure_reports_only_safe_type_and_still_closes(
    runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = runner.build_spec()
    connection = _FakeProbeConnection(
        fetch_error=RuntimeError(f"server said password={spec.password}")
    )
    captured: dict[str, Any] = {}
    _install_fake_connect(runner, monkeypatch, connection, captured)

    with pytest.raises(runner.RunnerError) as info:
        runner._probe_once(spec, 55432, timeout=1.0)

    message = str(info.value)
    assert "RuntimeError" in message
    assert spec.password not in message
    assert connection.close_calls == 1
    assert connection.closed is True


def test_report_requires_a_real_postgresql_engine(runner: Any) -> None:
    report = _valid_report(runner)
    report["probes"][0]["dialect"] = "sqlite"
    problems = runner._report_failure(report, runner.PytestResult(0, "", False, True))
    assert any("not PostgreSQL" in problem for problem in problems)
    report = _valid_report(runner)
    report["probes"] = []
    problems = runner._report_failure(report, runner.PytestResult(0, "", False, True))
    assert any("no live PostgreSQL" in problem for problem in problems)


def test_fixed_test_selection_rejects_arbitrary_arguments(runner: Any) -> None:
    command = runner.build_pytest_command()
    assert "tests.integration.pg_plugin" in command
    assert all(test_file in command for test_file in runner.TEST_FILES)
    with pytest.raises(SystemExit):
        runner.main(["--database-url", "postgresql://elsewhere/db"])
    with pytest.raises(SystemExit):
        runner.main(["tests/unit/test_metrics.py"])


def test_execute_success_uses_fake_docker_and_cleans_only_this_container(
    runner: Any,
) -> None:
    spec = runner.build_spec()
    docker = _FakeDocker()
    observed: dict[str, Any] = {}

    def fake_probe(target: Any, port: int, *, state: Any) -> str:
        observed["probe"] = (target.run_id, port)
        return "16.9"

    def fake_pytest(command: list[str], *, cwd: Path, env: dict[str, str], state: Any):
        observed["command"] = command
        observed["env"] = env
        report_path = Path(env["DTC_TEST_PG_REPORT"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(_valid_report(runner)), encoding="utf-8")
        return runner.PytestResult(0, "fake pytest", False, True)

    code = runner.execute(
        spec=spec,
        deps=docker,
        probe=fake_probe,
        pytest_runner=fake_pytest,
        state=runner.SignalState(),
        install_signals=False,
        env_base=_base_env(),
    )

    assert code == 0
    assert observed["probe"] == (spec.run_id, 55432)
    assert observed["command"] == runner.build_pytest_command()
    assert observed["env"]["DTC_TEST_PG_URL"] == runner.target_url(spec, 55432)
    assert "PGPASSWORD" not in observed["env"]
    assert docker.removed_names == [spec.name]
    assert "prune" not in docker.events


def test_execute_cancellation_during_create_waits_for_create_then_cleans(
    runner: Any,
) -> None:
    spec = runner.build_spec()
    state = runner.SignalState()
    docker = _FakeDocker()
    original_create = docker.run_container

    def create_then_signal(target: Any) -> None:
        original_create(target)
        state.request(signal.SIGTERM)

    docker.run_container = create_then_signal  # type: ignore[method-assign]
    pytest_called = False

    def fake_pytest(*args: Any, **kwargs: Any):
        nonlocal pytest_called
        pytest_called = True
        raise AssertionError("pytest must not run after cancellation during create")

    code = runner.execute(
        spec=spec,
        deps=docker,
        probe=lambda *args, **kwargs: "16.9",
        pytest_runner=fake_pytest,
        state=state,
        install_signals=False,
        env_base=_base_env(),
    )

    assert code == 128 + signal.SIGTERM
    assert pytest_called is False
    assert docker.events[:2] == ["available", "create"]
    assert docker.events.index("remove") > docker.events.index("create")
    assert docker.removed_names == [spec.name]


def test_execute_probe_failure_still_cleans_up(runner: Any) -> None:
    spec = runner.build_spec()
    docker = _FakeDocker()

    def failing_probe(*args: Any, **kwargs: Any) -> str:
        raise runner.RunnerError("target database SELECT 1 readiness timed out")

    code = runner.execute(
        spec=spec,
        deps=docker,
        probe=failing_probe,
        pytest_runner=lambda *args, **kwargs: pytest.fail("pytest must not run"),
        state=runner.SignalState(),
        install_signals=False,
        env_base=_base_env(),
    )

    assert code == 1
    assert docker.removed_names == [spec.name]


def test_execute_cleanup_failure_is_not_reported_as_pass(runner: Any) -> None:
    spec = runner.build_spec()
    docker = _FakeDocker(remove_ok=False)

    def fake_pytest(command: list[str], *, cwd: Path, env: dict[str, str], state: Any):
        Path(env["DTC_TEST_PG_REPORT"]).write_text(
            json.dumps(_valid_report(runner)), encoding="utf-8"
        )
        return runner.PytestResult(0, "", False, True)

    code = runner.execute(
        spec=spec,
        deps=docker,
        probe=lambda *args, **kwargs: "16.9",
        pytest_runner=fake_pytest,
        state=runner.SignalState(),
        install_signals=False,
        env_base=_base_env(),
    )

    assert code == 1


def test_failure_output_is_redacted(runner: Any, capsys: pytest.CaptureFixture[str]) -> None:
    spec = runner.build_spec()
    docker = _FakeDocker()
    url = runner.target_url(spec, 55432)

    def fake_pytest(command: list[str], *, cwd: Path, env: dict[str, str], state: Any):
        return runner.PytestResult(
            1,
            f"traceback url={url} password={spec.password}",
            False,
            True,
        )

    code = runner.execute(
        spec=spec,
        deps=docker,
        probe=lambda *args, **kwargs: "16.9",
        pytest_runner=fake_pytest,
        state=runner.SignalState(),
        install_signals=False,
        env_base=_base_env(),
    )

    captured = capsys.readouterr()
    assert code == 1
    assert spec.password not in captured.out
    assert url not in captured.out


def test_execute_rejects_leader_exit_while_group_lingers(runner: Any) -> None:
    spec = runner.build_spec()
    docker = _FakeDocker()

    def fake_pytest(command: list[str], *, cwd: Path, env: dict[str, str], state: Any):
        Path(env["DTC_TEST_PG_REPORT"]).write_text(
            json.dumps(_valid_report(runner)), encoding="utf-8"
        )
        return runner.PytestResult(0, "", False, True, True)

    code = runner.execute(
        spec=spec,
        deps=docker,
        probe=lambda *args, **kwargs: "16.9",
        pytest_runner=fake_pytest,
        state=runner.SignalState(),
        install_signals=False,
        env_base=_base_env(),
    )

    assert code == 1


def test_run_pytest_isolated_times_out_and_kills_the_group(runner: Any) -> None:
    result = runner.run_pytest_isolated(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=ROOT,
        env=os.environ,
        state=runner.SignalState(),
        timeout=0.4,
        group_grace=0.4,
    )
    assert result.timed_out is True
    assert result.group_gone is True


def test_run_pytest_isolated_cancels_a_running_group(runner: Any) -> None:
    state = runner.SignalState()
    timer = threading.Timer(0.3, state.request, args=(signal.SIGTERM,))
    timer.start()
    try:
        with pytest.raises(runner.RunnerCancelled):
            runner.run_pytest_isolated(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=ROOT,
                env=os.environ,
                state=state,
                timeout=30.0,
                group_grace=0.5,
            )
    finally:
        timer.cancel()


def test_run_pytest_isolated_detects_leader_exit_with_live_child(runner: Any) -> None:
    code = (
        "import subprocess, sys; "
        "subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print('leader done')"
    )
    result = runner.run_pytest_isolated(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=os.environ,
        state=runner.SignalState(),
        timeout=10.0,
        group_grace=0.5,
    )
    assert result.returncode == 0
    assert result.group_lingered is True
    assert result.group_gone is True


# ---------------------------------------------------------------------------
# The real gap: PG concurrency children used to call Popen with
# ``start_new_session=True``. The runner only signals pytest's process group,
# so a detached worker survived timeout, cancellation and leader exit. These
# tests launch a real grandchild with the PG children's actual startup
# options, rather than testing killpg in isolation.
# ---------------------------------------------------------------------------

# The real CHILD_POPEN_OPTIONS are passed in as JSON rather than imported inside
# the leader: importing the integration module pulls in the whole app and
# jieba's generated tables (~1.6s, or ~60s when a coverage hook is inherited),
# which races the deliberately short timeout/cancellation bounds these tests
# exercise. The grandchild and its real startup options stay the same.
_GROUP_LEADER = """\
import json
import subprocess
import sys
import time
from pathlib import Path

pid_path = Path(sys.argv[1])
options = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    **options,
)
pid_path.write_text(str(child.pid), encoding="utf-8")
{suffix}
"""


_COVERAGE_ENV_PREFIXES = ("COV_CORE_", "COVERAGE_PROCESS_")


def _isolated_env() -> dict[str, str]:
    """Drop pytest-cov's auto-start variables from synthetic leader processes.

    ``pytest-cov.pth`` starts coverage in every Python subprocess that inherits
    ``COV_CORE_*``. Tracing the import of jieba's generated tables turns the
    leader's ~1.6s startup into ~60s, which races these tests' deliberately
    short timeout/grace settings. Coverage of the synthetic leader is not what
    these tests assert, so it must not leak into the child environment.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_COVERAGE_ENV_PREFIXES)
    }


def _write_group_leader(tmp_path: Path, *, keep_running: bool) -> tuple[Path, Path]:
    script = tmp_path / "leader.py"
    script.write_text(
        _GROUP_LEADER.format(suffix="time.sleep(60)\n" if keep_running else ""),
        encoding="utf-8",
    )
    options_path = tmp_path / "child_options.json"
    options_path.write_text(json.dumps(pg_concurrency.CHILD_POPEN_OPTIONS), encoding="utf-8")
    return script, options_path


def _read_grandchild_pid(path: Path, *, timeout: float = 15.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.02)
    raise AssertionError("group leader did not report its grandchild pid")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_pid_gone(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.02)
    return not _pid_alive(pid)


def _kill_reported_pid(path: Path) -> None:
    """Best-effort cleanup so a regression cannot leak the test grandchild."""
    try:
        pid = int(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_pg_child_spawn_inherits_the_pytest_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class RecordingPopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["args"] = args
            captured.update(kwargs)

    monkeypatch.setattr(pg_concurrency.subprocess, "Popen", RecordingPopen)
    spawned: list[Any] = []
    _spawn(
        "hold",
        url="postgresql+asyncpg://user:pass@127.0.0.1:5432/db",
        owner="owner-a",
        workdir=tmp_path / "child",
        children=spawned,
    )

    assert "start_new_session" not in captured
    assert "preexec_fn" not in captured
    for key, value in CHILD_POPEN_OPTIONS.items():
        assert captured[key] is value
    assert len(spawned) == 1


def test_runner_timeout_kills_pg_grandchild_in_the_same_group(runner: Any, tmp_path: Path) -> None:
    script, options = _write_group_leader(tmp_path, keep_running=True)
    pid_path = tmp_path / "grandchild.pid"
    started = time.monotonic()
    try:
        result = runner.run_pytest_isolated(
            [sys.executable, str(script), str(pid_path), str(options)],
            cwd=ROOT,
            env=_isolated_env(),
            state=runner.SignalState(),
            timeout=3.0,
            group_grace=0.5,
        )
        pid = _read_grandchild_pid(pid_path)
        assert result.timed_out is True
        assert result.group_gone is True
        assert _wait_pid_gone(
            pid
        ), "the PG child startup options detached the worker from pytest's process group"
        assert time.monotonic() - started < 10.0
    finally:
        _kill_reported_pid(pid_path)


def test_runner_cancellation_kills_pg_grandchild_in_the_same_group(
    runner: Any, tmp_path: Path
) -> None:
    script, options = _write_group_leader(tmp_path, keep_running=True)
    pid_path = tmp_path / "grandchild.pid"
    state = runner.SignalState()
    cancelled = threading.Event()

    def request_cancel() -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not cancelled.is_set():
            if pid_path.exists():
                state.request(signal.SIGTERM)
                return
            time.sleep(0.02)

    timer = threading.Thread(target=request_cancel, daemon=True)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(runner.RunnerCancelled):
            runner.run_pytest_isolated(
                [sys.executable, str(script), str(pid_path), str(options)],
                cwd=ROOT,
                env=_isolated_env(),
                state=state,
                timeout=30.0,
                group_grace=0.5,
            )
        pid = _read_grandchild_pid(pid_path)
        assert _wait_pid_gone(
            pid
        ), "the PG child startup options detached the worker from pytest's process group"
        assert time.monotonic() - started < 10.0
    finally:
        cancelled.set()
        timer.join(timeout=2.0)
        _kill_reported_pid(pid_path)


def test_runner_reaps_pg_grandchild_after_pytest_leader_exits(runner: Any, tmp_path: Path) -> None:
    script, options = _write_group_leader(tmp_path, keep_running=False)
    pid_path = tmp_path / "grandchild.pid"
    started = time.monotonic()
    try:
        result = runner.run_pytest_isolated(
            [sys.executable, str(script), str(pid_path), str(options)],
            cwd=ROOT,
            env=_isolated_env(),
            state=runner.SignalState(),
            timeout=10.0,
            group_grace=0.5,
        )
        pid = _read_grandchild_pid(pid_path)
        assert result.returncode == 0
        assert result.timed_out is False
        assert result.group_lingered is True
        assert result.group_gone is True
        assert _wait_pid_gone(
            pid
        ), "the PG child startup options detached the worker from pytest's process group"
        assert time.monotonic() - started < 5.0
    finally:
        _kill_reported_pid(pid_path)
