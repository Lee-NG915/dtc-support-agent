"""Explicit PostgreSQL adapter for the after-sales acceptance selection.

This plugin is opt-in: it only activates when ``DTC_TEST_PG_URL`` and
``DTC_TEST_PG_RUN_ID`` describe the disposable database created by
``scripts/run_after_sales_pg.py``. Plain ``pytest`` keeps its normal SQLite
behaviour and never starts Docker.

For the workflow/execution/workbench modules the adapter replaces the
module-level ``_settings`` factory on ``request.module`` (the function the
fixtures actually call), rebuilds ``public`` and runs Alembic ``0001`` ->
``head`` before every test, and verifies the live application engine through
``ctx.client.portal`` on the same event loop. A test cannot silently stay on
SQLite: the identity probe compares the engine dialect, ``current_database()``,
``current_user()``, ``pg_backend_pid()`` and the Settings URL with this run's
random identity.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
PG_MODULE = "tests/integration/test_after_sales_workflow.py"
PG_EXECUTION_MODULE = "tests/integration/test_after_sales_execution.py"
PG_WORKBENCH_MODULE = "tests/integration/test_after_sales_workbench.py"
PG_CONCURRENCY_MODULE = "tests/integration/test_after_sales_pg_concurrency.py"
PG_MIGRATION_MODULE = "tests/integration/test_after_sales_migrations.py"
PG_MIGRATION_TEST = "test_dedicated_postgres_migration_target"
PG_RUN_ID_RE = re.compile(r"^[0-9a-f]{16}$")

ADAPTED_MODULES = {
    PG_MODULE,
    PG_EXECUTION_MODULE,
    PG_WORKBENCH_MODULE,
    PG_CONCURRENCY_MODULE,
}
REQUIRED_PG_MODULES = (PG_MODULE, PG_EXECUTION_MODULE, PG_WORKBENCH_MODULE)


@dataclass
class PgState:
    url: str
    run_id: str
    username: str
    database: str
    port: int
    report_path: Path | None
    probes: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    node_status: dict[str, str] = field(default_factory=dict)
    node_durations: dict[str, float] = field(default_factory=dict)
    head: str | None = None
    history: list[str] = field(default_factory=list)
    observed_revisions: list[str] = field(default_factory=list)
    reset_count: int = 0
    reset_durations: list[float] = field(default_factory=list)

    def category(self, nodeid: str) -> str:
        path = nodeid.split("::", 1)[0]
        if path == PG_CONCURRENCY_MODULE:
            return "pg_concurrency"
        if path == PG_MIGRATION_MODULE:
            if nodeid.endswith(PG_MIGRATION_TEST):
                return "pg_migration"
            return "sqlite_migration"
        if path in ADAPTED_MODULES:
            return "pg_adapted"
        return "other"


def _module_path(module: Any) -> str:
    path = getattr(module, "__file__", None)
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return ""


def _state(config: pytest.Config) -> PgState | None:
    return getattr(config, "_dtc_pg_state", None)


def _validate_target(url: str, run_id: str) -> PgState:
    if not PG_RUN_ID_RE.fullmatch(run_id):
        raise pytest.UsageError("DTC_TEST_PG_RUN_ID is not a disposable run id")
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg":
        raise pytest.UsageError("DTC_TEST_PG_URL must use postgresql+asyncpg")
    if parsed.host != "127.0.0.1":
        raise pytest.UsageError("DTC_TEST_PG_URL must target 127.0.0.1")
    if parsed.port is None:
        raise pytest.UsageError("DTC_TEST_PG_URL must include a port")
    expected = f"dtc_pgtest_{run_id}"
    if parsed.username != expected:
        raise pytest.UsageError("DTC_TEST_PG_URL user does not belong to this run")
    if parsed.database != expected:
        raise pytest.UsageError("DTC_TEST_PG_URL database does not belong to this run")
    if not parsed.password:
        raise pytest.UsageError("DTC_TEST_PG_URL must carry the generated password")
    report = os.getenv("DTC_TEST_PG_REPORT")
    return PgState(
        url=url,
        run_id=run_id,
        username=expected,
        database=expected,
        port=int(parsed.port),
        report_path=Path(report) if report else None,
    )


def pytest_configure(config: pytest.Config) -> None:
    url = os.getenv("DTC_TEST_PG_URL")
    if not url:
        return
    run_id = os.getenv("DTC_TEST_PG_RUN_ID", "")
    state = _validate_target(url, run_id)
    config._dtc_pg_state = state  # type: ignore[attr-defined]
    config.addinivalue_line("markers", "dtc_pg: test adapted to the disposable PostgreSQL database")
    config.addinivalue_line("markers", "dtc_pg_concurrency: real multi-process PostgreSQL scenario")


def _alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


async def _reset_schema(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
    finally:
        await engine.dispose()


async def _revision_and_tables(url: str) -> tuple[str | None, bool, bool]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            operation_table = await connection.scalar(
                text("SELECT to_regclass('public.after_sales_operation')")
            )
            lookup_column = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'after_sales_operation' "
                    "AND column_name = 'lookup_count'"
                )
            )
            return (
                str(revision) if revision is not None else None,
                operation_table is not None,
                int(lookup_column or 0) == 1,
            )
    finally:
        await engine.dispose()


def _prepare_database(state: PgState) -> None:
    """Rebuild ``public`` from empty and migrate 0001 -> head for one test."""
    started = time.monotonic()
    asyncio.run(_reset_schema(state.url))
    config = _alembic_config(state.url)
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    history = sorted({revision.revision for revision in script.walk_revisions()})
    if head is None or "0001" not in history:
        raise pytest.fail("Alembic history is missing 0001 or head", pytrace=False)
    command.upgrade(config, "head")
    revision, operation_table, lookup_column = asyncio.run(_revision_and_tables(state.url))
    if revision != head:
        raise pytest.fail(
            f"database revision {revision!r} did not reach Alembic head {head!r}",
            pytrace=False,
        )
    if not operation_table or not lookup_column:
        raise pytest.fail(
            "migrated PostgreSQL schema is missing the 0005 operation columns",
            pytrace=False,
        )
    state.head = head
    state.history = history
    state.observed_revisions.append(str(revision))
    state.reset_count += 1
    state.reset_durations.append(round(time.monotonic() - started, 4))


def _verified_probe(portal: Any, ctx: Any, state: PgState) -> dict[str, Any]:
    engine = ctx.app.state.container.engine

    async def read_identity() -> dict[str, Any]:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT current_database(), current_user, "
                        "pg_backend_pid(), current_setting('server_version')"
                    )
                )
            ).one()
        return {
            "dialect": engine.dialect.name,
            "database": str(row[0]),
            "user": str(row[1]),
            "backend_pid": int(row[2]),
            "server_version": str(row[3]),
        }

    identity = portal.call(read_identity)
    settings_url = make_url(ctx.app.state.container.settings.database_url)
    target_url = make_url(state.url)
    checks = {
        "dialect_postgresql": identity["dialect"] == "postgresql",
        "database_matches": identity["database"] == state.database,
        "user_matches": identity["user"] == state.username,
        "backend_pid_positive": identity["backend_pid"] > 0,
        "settings_driver": settings_url.drivername == "postgresql+asyncpg",
        "settings_host": settings_url.host == "127.0.0.1",
        "settings_port": settings_url.port == state.port,
        "settings_user": settings_url.username == state.username,
        "settings_database": settings_url.database == state.database,
        "expected_target": target_url.database == state.database,
    }
    return {
        **identity,
        "checks": checks,
        "ok": all(checks.values()),
    }


@pytest.fixture(autouse=True)
def _dtc_pg_adapter(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(request.config)
    if state is None:
        return
    module_path = _module_path(request.module)
    nodeid = request.node.nodeid
    if module_path in ADAPTED_MODULES:
        _prepare_database(state)
        original = getattr(request.module, "_settings", None)
        if not callable(original):
            raise pytest.fail(
                f"{module_path} has no module-level _settings to adapt",
                pytrace=False,
            )

        def pg_settings(tmp_path: Path, **overrides: Any) -> Any:
            overrides["database_url"] = state.url
            return original(tmp_path, **overrides)

        monkeypatch.setattr(request.module, "_settings", pg_settings)
        if module_path == PG_CONCURRENCY_MODULE:
            request.node.add_marker(pytest.mark.dtc_pg_concurrency)
        else:
            request.node.add_marker(pytest.mark.dtc_pg)
        if "ctx" in request.fixturenames:
            ctx = request.getfixturevalue("ctx")
            portal = ctx.client.portal
            if portal is None:
                raise pytest.fail("the application TestClient portal is not running", pytrace=False)
            probe = _verified_probe(portal, ctx, state)
            probe["module"] = module_path
            probe["nodeid"] = nodeid
            state.probes.append(probe)
            if not probe["ok"]:
                failed = [name for name, ok in probe["checks"].items() if not ok]
                pytest.fail(
                    "live engine is not this run's PostgreSQL target: " + ", ".join(failed),
                    pytrace=False,
                )
    elif module_path == PG_MIGRATION_MODULE and nodeid.endswith(PG_MIGRATION_TEST):
        _prepare_database(state)
        request.node.add_marker(pytest.mark.dtc_pg)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    outcome = yield
    report = outcome.get_result()
    state = _state(item.config)
    if state is None:
        return
    category = state.category(report.nodeid)
    if category == "other":
        return
    status = state.node_status.get(report.nodeid)
    if report.when == "call":
        state.node_durations[report.nodeid] = round(report.duration, 4)
    if report.failed:
        state.node_status[report.nodeid] = "failed"
    elif report.skipped and status is None and report.when in {"setup", "call"}:
        state.node_status[report.nodeid] = "skipped"
    elif report.when == "call" and report.passed:
        state.node_status[report.nodeid] = "passed"


def _counts(state: PgState) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    counts: dict[str, dict[str, int]] = {}
    module_counts: dict[str, dict[str, int]] = {}
    for nodeid, status in state.node_status.items():
        category = state.category(nodeid)
        counts.setdefault(category, {"passed": 0, "failed": 0, "skipped": 0})
        counts[category][status] += 1
        module = nodeid.split("::", 1)[0]
        module_counts.setdefault(module, {"passed": 0, "failed": 0, "skipped": 0})
        module_counts[module][status] += 1
    return counts, module_counts


def _expected_problems(
    state: PgState,
    counts: dict[str, dict[str, int]],
    module_counts: dict[str, dict[str, int]],
    passed_nodeids: set[str],
) -> list[str]:
    problems: list[str] = []
    adapted = counts.get("pg_adapted", {})
    if adapted.get("passed", 0) < 1:
        problems.append("no adapted tests ran on PostgreSQL")
    if adapted.get("failed", 0) or adapted.get("skipped", 0):
        problems.append("adapted PostgreSQL tests failed or skipped")
    for module in REQUIRED_PG_MODULES:
        if module_counts.get(module, {}).get("passed", 0) < 1:
            problems.append(f"no passing PostgreSQL tests for {module}")
        if not any(probe.get("module") == module for probe in state.probes):
            problems.append(f"no live PostgreSQL probe for {module}")
    if not state.probes:
        problems.append("no live PostgreSQL engine probes ran")
    if any(probe.get("ok") is not True for probe in state.probes):
        problems.append("a live engine probe did not match this run's PostgreSQL")
    concurrency = counts.get("pg_concurrency", {})
    if concurrency.get("passed", 0) < 3:
        problems.append("the real multi-process PostgreSQL scenarios did not all pass")
    if concurrency.get("failed", 0) or concurrency.get("skipped", 0):
        problems.append("a real multi-process PostgreSQL scenario failed or skipped")
    migration = counts.get("pg_migration", {})
    if migration.get("passed", 0) != 1:
        problems.append("the dedicated PostgreSQL migration test did not pass")
    if counts.get("sqlite_migration", {}).get("passed", 0) < 3:
        problems.append("the separate SQLite migration tests did not all pass")
    if state.head is None or state.head not in state.observed_revisions:
        problems.append("the per-test database never reached Alembic head")
    if "0001" not in state.history:
        problems.append("the Alembic history does not include 0001")
    expected_nodes = {
        f"{PG_CONCURRENCY_MODULE}::test_two_processes_claim_exactly_one_owner",
        f"{PG_CONCURRENCY_MODULE}::test_expired_lease_reclaim_fences_stale_epoch",
        f"{PG_CONCURRENCY_MODULE}::test_crashed_lease_is_persisted_and_reclaimable",
        f"{PG_MIGRATION_MODULE}::{PG_MIGRATION_TEST}",
    }
    for nodeid in sorted(expected_nodes - passed_nodeids):
        problems.append(f"required test did not pass: {nodeid.split('::')[-1]}")
    return list(dict.fromkeys(problems))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    state = _state(session.config)
    if state is None:
        return
    counts, module_counts = _counts(state)
    passed_nodeids = {nodeid for nodeid, status in state.node_status.items() if status == "passed"}
    problems = _expected_problems(state, counts, module_counts, passed_nodeids)
    if problems:
        session.exitstatus = 1
    report = {
        "pg_configured": True,
        "counts": counts,
        "module_counts": module_counts,
        "passed_nodeids": sorted(passed_nodeids),
        "node_durations": state.node_durations,
        "probes": state.probes,
        "observations": state.observations,
        "migration": {
            "head": state.head,
            "history": state.history,
            "observed_revisions": state.observed_revisions,
            "reset_count": state.reset_count,
            "reset_durations": state.reset_durations,
        },
        "problems": problems,
    }
    if state.report_path is not None:
        state.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state.report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(state.report_path)
