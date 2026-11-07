"""Real multi-process PostgreSQL lease scenarios for the after-sales worker.

These tests are intentionally skipped by plain ``pytest``. They run only under
``uv run python scripts/run_after_sales_pg.py``, which creates a disposable
PostgreSQL 16 database and sets ``DTC_TEST_PG_URL``. Each child is a separate
OS process with its own SQLAlchemy pool; the parent only observes PostgreSQL.

The stale-epoch test calls the production core methods with the old claim
(``_complete``, ``_fail_retry``, ``_release_with_backoff`` and
``_schedule_reconciliation``). Assertions are made against the real database
row, so a mocked serial lock cannot make them pass.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import Settings
from app.core.time import utcnow
from app.main import create_app
from app.repositories.models import AfterSalesApprovalRow, AfterSalesOperationRow
from app.services.after_sales.execution import _OUTCOME_SKIPPED
from tests.integration.test_after_sales_execution import (
    Ctx,
    _detail,
    _fetch_row,
    _make_due,
    _operation_id,
    _prepare_approved,
    _seed_staff,
    _update_row,
)

ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).with_name("pg_child.py")
PG_URL = os.getenv("DTC_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="run uv run python scripts/run_after_sales_pg.py for real PostgreSQL",
)

PROVIDER_ENV_KEYS = {
    "DATABASE_URL",
    "DTC_TEST_PG_URL",
    "ALEMBIC_DATABASE_URL",
    "PYTHONPATH",
    "DEEPSEEK_API_KEY",
    "EMBEDDING_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "EPIG_BASE_URL",
    "EPIG_SERVICE_TOKEN",
    "EPIG_ALLOWED_MARKETS",
    "ADMIN_API_TOKEN",
}


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "app_env": "testing",
        "log_level": "CRITICAL",
        "database_url": PG_URL or f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "after_sales_enabled": True,
        "business_connector": "fake",
        "deepseek_api_key": "",
        "embedding_api_key": "",
        "local_embedding_enabled": False,
        "cross_encoder_enabled": False,
        "cross_encoder_force": False,
        "unstructured_enabled": False,
        "mcp_enabled": False,
        "redis_url": "",
        "qdrant_url": "",
        "knowledge_dir": str(ROOT / "knowledge"),
        "after_sales_worker_retry_base_seconds": 0.1,
        "after_sales_worker_retry_max_seconds": 1.0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def ctx(tmp_path: Path) -> Ctx:
    settings = _settings(tmp_path)
    tokens = asyncio.run(_seed_staff(settings.database_url))
    app = create_app(settings)
    with TestClient(app) as client:
        yield Ctx(
            client=client,
            app=app,
            tokens=tokens,
            database_url=settings.database_url,
            settings=settings,
        )


@pytest.fixture
def children() -> list[subprocess.Popen[str]]:
    processes: list[subprocess.Popen[str]] = []
    yield processes
    for process in processes:
        _stop_child(process)


def _child_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in PROVIDER_ENV_KEYS:
            continue
        if key.upper().startswith(("PG", "DTC_TEST_PG_", "DEEPSEEK_", "EMBEDDING_")):
            continue
        if key.upper().startswith(("OPENAI_", "ANTHROPIC_", "EPIG_")):
            continue
        env[key] = value
    env.update(
        {
            "APP_ENV": "testing",
            "LOG_LEVEL": "CRITICAL",
            "PYTHONPATH": str(ROOT),
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
        }
    )
    return env


def _safe(text: str, url: str) -> str:
    if url:
        text = text.replace(url, "[redacted]")
    return text


# PG children deliberately stay in pytest's process group. The runner's
# timeout/cancellation path terminates that whole group; ``start_new_session``
# here would detach real workers and leak them past the runner's bounded
# cleanup. Keep this shared with the runner lifecycle regression tests.
CHILD_POPEN_OPTIONS: dict[str, Any] = {
    "stdout": subprocess.PIPE,
    "stderr": subprocess.PIPE,
    "text": True,
}


def _spawn(
    mode: str,
    *,
    url: str,
    owner: str,
    workdir: Path,
    children: list[subprocess.Popen[str]],
    **paths: Path,
) -> subprocess.Popen[str]:
    workdir.mkdir(parents=True, exist_ok=True)
    config = {
        "mode": mode,
        "url": url,
        "owner": owner,
        **{key: str(value) for key, value in paths.items()},
    }
    config_path = workdir / f"config-{owner}.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)
    process = subprocess.Popen(
        [sys.executable, str(CHILD), str(config_path)],
        cwd=str(workdir),
        env=_child_env(),
        **CHILD_POPEN_OPTIONS,
    )
    children.append(process)
    return process


def _wait_path(path: Path, *, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path.name}")


def _wait_result(
    path: Path,
    process: subprocess.Popen[str],
    *,
    url: str,
    timeout: float = 45.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if process.poll() is not None:
            _, stderr = process.communicate(timeout=5)
            raise AssertionError(
                f"child exited {process.returncode} before writing {path.name}: "
                f"{_safe(stderr or '', url)[-400:]}"
            )
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for child result {path.name}")


def _stop_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        return
    try:
        process.terminate()
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _wait_child(process: subprocess.Popen[str], *, url: str) -> None:
    try:
        _, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate(timeout=5)
        raise AssertionError("child did not exit after its result was reported") from None
    assert process.returncode == 0, _safe(stderr or "", url)[-400:]


async def _query(url: str, statement: str, parameters: dict[str, Any] | None = None):
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(statement), parameters or {})).all()
    finally:
        await engine.dispose()


def _operation_state(url: str, operation_id: str) -> Any:
    rows = asyncio.run(
        _query(
            url,
            "SELECT lease_owner, lease_epoch, status, lease_until "
            "FROM after_sales_operation WHERE operation_id = :operation_id",
            {"operation_id": operation_id},
        )
    )
    assert len(rows) == 1
    return rows[0]


def _event_count(url: str, case_id: int) -> int:
    rows = asyncio.run(
        _query(
            url,
            "SELECT count(*) FROM after_sales_event WHERE case_id = :case_id",
            {"case_id": case_id},
        )
    )
    return int(rows[0][0])


def _record(request: pytest.FixtureRequest, observation: dict[str, Any]) -> None:
    state = getattr(request.config, "_dtc_pg_state", None)
    if state is not None:
        state.observations.append(observation)


def _exactly_one(claim_results: list[dict[str, Any]]) -> dict[str, Any]:
    claimed = [(result, claim) for result in claim_results for claim in result["claims"]]
    assert len(claimed) == 1, f"expected exactly one claim, got {len(claimed)}"
    return claimed[0][1]


def test_two_processes_claim_exactly_one_owner(
    ctx: Ctx,
    tmp_path: Path,
    children: list[subprocess.Popen[str]],
    request: pytest.FixtureRequest,
) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    _make_due(ctx, operation_id)

    go_path = tmp_path / "go"
    release_path = tmp_path / "release"
    processes: list[subprocess.Popen[str]] = []
    results: list[tuple[Path, subprocess.Popen[str]]] = []
    for tag in ("proc-a", "proc-b"):
        ready_path = tmp_path / f"ready-{tag}"
        result_path = tmp_path / f"result-{tag}"
        process = _spawn(
            "concurrent",
            url=ctx.database_url,
            owner=tag,
            workdir=tmp_path / tag,
            children=children,
            ready_path=ready_path,
            go_path=go_path,
            release_path=release_path,
            result_path=result_path,
        )
        processes.append(process)
        _wait_path(ready_path)
        results.append((result_path, process))

    go_path.write_text("go", encoding="utf-8")
    claim_results = [
        _wait_result(result_path, process, url=ctx.database_url) for result_path, process in results
    ]
    claim = _exactly_one(claim_results)
    assert claim["operation_id"] == operation_id
    assert claim["epoch"] == 1

    os_pids = sorted(int(result["os_pid"]) for result in claim_results)
    backend_pids = sorted(int(result["pg_backend_pid"]) for result in claim_results)
    assert len(set(os_pids)) == 2
    assert len(set(backend_pids)) == 2

    activity = asyncio.run(
        _query(
            ctx.database_url,
            "SELECT pid, application_name FROM pg_stat_activity "
            "WHERE application_name = ANY(:names)",
            {"names": ["proc-a", "proc-b"]},
        )
    )
    active_by_name: dict[str, set[int]] = {}
    for row in activity:
        active_by_name.setdefault(str(row[1]), set()).add(int(row[0]))
    for result in claim_results:
        assert result["pg_backend_pid"] in active_by_name.get(result["owner"], set())

    release_path.write_text("release", encoding="utf-8")
    for process in processes:
        _wait_child(process, url=ctx.database_url)

    row = _operation_state(ctx.database_url, operation_id)
    winner = next(result["owner"] for result in claim_results if result["claims"])
    assert row[0] == winner
    assert row[1] == 1
    assert row[3] is not None

    _record(
        request,
        {
            "scenario": "two_process_concurrent_claim",
            "os_pids": os_pids,
            "pg_backend_pids": backend_pids,
            "pg_activity_pids": sorted(pid for pids in active_by_name.values() for pid in pids),
            "owners": sorted(result["owner"] for result in claim_results),
            "epochs": [claim["epoch"]],
            "exactly_one": True,
            "winner": winner,
        },
    )


def test_expired_lease_reclaim_fences_stale_epoch(
    ctx: Ctx,
    tmp_path: Path,
    children: list[subprocess.Popen[str]],
    request: pytest.FixtureRequest,
) -> None:
    case_id, detail, approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    _make_due(ctx, operation_id)
    events_before = _event_count(ctx.database_url, case_id)

    holder_workdir = tmp_path / "holder"
    holder_result = holder_workdir / "result.json"
    stale_path = holder_workdir / "stale"
    stale_result = holder_workdir / "stale-result.json"
    holder = _spawn(
        "hold",
        url=ctx.database_url,
        owner="holder-a",
        workdir=holder_workdir,
        children=children,
        result_path=holder_result,
        stale_path=stale_path,
        stale_result_path=stale_result,
    )
    held_claim = _wait_result(holder_result, holder, url=ctx.database_url)
    assert len(held_claim["claims"]) == 1
    old_claim = held_claim["claims"][0]
    assert old_claim["owner"] == "holder-a"
    assert old_claim["epoch"] == 1

    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            lease_until=utcnow() - timedelta(seconds=1),
        )
    )

    reclaim_workdir = tmp_path / "reclaim"
    reclaim_result = reclaim_workdir / "result.json"
    reclaimer = _spawn(
        "reclaim",
        url=ctx.database_url,
        owner="reclaimer-b",
        workdir=reclaim_workdir,
        children=children,
        result_path=reclaim_result,
    )
    reclaimed = _wait_result(reclaim_result, reclaimer, url=ctx.database_url)
    assert len(reclaimed["claims"]) == 1
    new_claim = reclaimed["claims"][0]
    assert new_claim["operation_id"] == operation_id
    assert new_claim["owner"] == "reclaimer-b"
    assert new_claim["epoch"] == old_claim["epoch"] + 1
    assert new_claim["epoch"] == 2
    assert reclaimed["pg_backend_pid"] != held_claim["pg_backend_pid"]

    stale_path.write_text("stale", encoding="utf-8")
    stale = _wait_result(stale_result, holder, url=ctx.database_url)
    _wait_child(holder, url=ctx.database_url)
    _wait_child(reclaimer, url=ctx.database_url)

    outcomes = stale["stale_outcomes"]
    assert outcomes["load_context"] == "lease_lost"
    assert outcomes["complete"] == _OUTCOME_SKIPPED
    assert outcomes["fail_retry"] == _OUTCOME_SKIPPED
    assert outcomes["release"] == _OUTCOME_SKIPPED
    assert outcomes["schedule_reconciliation"] is None
    assert stale["row"]["lease_owner"] == "reclaimer-b"
    assert stale["row"]["lease_epoch"] == 2
    assert stale["row"]["status"] == "prepared"
    assert stale["row"]["lease_until"] is not None
    assert stale["event_count"] == events_before

    row = _operation_state(ctx.database_url, operation_id)
    assert row[0] == "reclaimer-b"
    assert row[1] == 2
    assert row[2] == "prepared"
    assert row[3] is not None

    after = _detail(ctx, case_id)
    assert after["state"] == "waiting_approval"
    approval = asyncio.run(_fetch_row(ctx.database_url, AfterSalesApprovalRow, approval_id))
    assert approval.status == "approved"
    assert _event_count(ctx.database_url, case_id) == events_before

    _record(
        request,
        {
            "scenario": "expired_lease_reclaim_and_stale_fence",
            "os_pids": [held_claim["os_pid"], reclaimed["os_pid"]],
            "pg_backend_pids": [
                held_claim["pg_backend_pid"],
                reclaimed["pg_backend_pid"],
            ],
            "owners": [old_claim["owner"], new_claim["owner"]],
            "epochs": [old_claim["epoch"], new_claim["epoch"]],
            "exactly_one": True,
            "stale_outcomes": outcomes,
        },
    )


def test_crashed_lease_is_persisted_and_reclaimable(
    ctx: Ctx,
    tmp_path: Path,
    children: list[subprocess.Popen[str]],
    request: pytest.FixtureRequest,
) -> None:
    _case_id, detail, _approval_id = _prepare_approved(ctx)
    operation_id = _operation_id(detail)
    _make_due(ctx, operation_id)

    crash_workdir = tmp_path / "crasher"
    crash_result = crash_workdir / "result.json"
    crasher = _spawn(
        "hold",
        url=ctx.database_url,
        owner="crasher-a",
        workdir=crash_workdir,
        children=children,
        result_path=crash_result,
        stale_path=crash_workdir / "never-stale",
        stale_result_path=crash_workdir / "never-stale-result.json",
    )
    claim_result = _wait_result(crash_result, crasher, url=ctx.database_url)
    crash_claim = claim_result["claims"][0]
    assert crash_claim["epoch"] == 1

    crasher.kill()  # SIGKILL: no Python cleanup, no lease release
    crasher.communicate(timeout=10)
    assert crasher.returncode == -9

    persisted = _operation_state(ctx.database_url, operation_id)
    assert persisted[0] == "crasher-a"
    assert persisted[1] == 1
    assert persisted[3] is not None, "a crashed process must leave the lease row behind"

    asyncio.run(
        _update_row(
            ctx.database_url,
            AfterSalesOperationRow,
            operation_id,
            lease_until=utcnow() - timedelta(seconds=1),
        )
    )
    reclaim_workdir = tmp_path / "after-crash"
    reclaim_result = reclaim_workdir / "result.json"
    reclaimer = _spawn(
        "reclaim",
        url=ctx.database_url,
        owner="after-crash-b",
        workdir=reclaim_workdir,
        children=children,
        result_path=reclaim_result,
    )
    reclaimed = _wait_result(reclaim_result, reclaimer, url=ctx.database_url)
    assert len(reclaimed["claims"]) == 1
    new_claim = reclaimed["claims"][0]
    assert new_claim["owner"] == "after-crash-b"
    assert new_claim["epoch"] == crash_claim["epoch"] + 1
    assert reclaimed["pg_backend_pid"] != claim_result["pg_backend_pid"]
    _wait_child(reclaimer, url=ctx.database_url)

    row = _operation_state(ctx.database_url, operation_id)
    assert row[0] == "after-crash-b"
    assert row[1] == 2

    _record(
        request,
        {
            "scenario": "crashed_lease_persisted_and_reclaimed",
            "os_pids": [claim_result["os_pid"], reclaimed["os_pid"]],
            "pg_backend_pids": [
                claim_result["pg_backend_pid"],
                reclaimed["pg_backend_pid"],
            ],
            "owners": ["crasher-a", "after-crash-b"],
            "epochs": [crash_claim["epoch"], new_claim["epoch"]],
            "exactly_one": True,
            "crash_returncode": crasher.returncode,
        },
    )
