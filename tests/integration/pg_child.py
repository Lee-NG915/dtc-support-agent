"""Independent worker process used by the real PostgreSQL concurrency tests.

The script is launched as a separate OS process with its own SQLAlchemy pool.
It only ever talks to the disposable database named in the JSON config file,
never reads ``.env``, and never prints the generated password or URL.

Modes
-----
``concurrent``
    Wait on a file barrier, claim due operations, report the claim, then keep
    one PostgreSQL session open until the parent releases it.
``hold``
    Claim, report, then wait. On the ``stale`` command it exercises the real
    lease-fenced core methods (``_load_context``, ``_complete``, ``_fail_retry``,
    ``_release_with_backoff``, ``_schedule_reconciliation``) under its old
    owner/epoch and reports what the database still contains.
``reclaim``
    Claim once and report the new owner/epoch, then exit.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.core.time import utcnow
from app.repositories.models import AfterSalesOperationRow
from app.services.after_sales.execution import (
    _OUTCOME_SKIPPED,
    AfterSalesExecution,
    _LeaseLost,
)

WAIT_TIMEOUT = 90.0


class _UnusedConnector:
    pass


class _UnusedWorkflow:
    pass


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"barrier file was not created: {path.name}")


def _settings(url: str, owner: str):
    return Settings(
        _env_file=None,
        app_env="testing",
        log_level="CRITICAL",
        database_url=url,
        after_sales_enabled=True,
        business_connector="fake",
        deepseek_api_key="",
        embedding_api_key="",
        local_embedding_enabled=False,
        cross_encoder_enabled=False,
        unstructured_enabled=False,
        mcp_enabled=False,
        redis_url="",
        qdrant_url="",
        knowledge_dir=str(Path(__file__).resolve().parents[2] / "knowledge"),
        after_sales_worker_retry_base_seconds=0.1,
        after_sales_worker_retry_max_seconds=1.0,
    )


def _claim_payload(claim: Any) -> dict[str, Any]:
    return {
        "operation_id": claim.operation_id,
        "owner": claim.owner,
        "epoch": claim.epoch,
        "status": claim.status,
        "case_id": claim.case_id,
    }


async def _run(config: dict[str, Any]) -> None:
    mode = str(config["mode"])
    url = str(config["url"])
    owner = str(config["owner"])
    result_path = Path(config["result_path"])
    settings = _settings(url, owner)
    engine = create_async_engine(
        settings.database_url,
        connect_args={"server_settings": {"application_name": owner}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    execution = AfterSalesExecution(
        settings=settings,
        session_factory=factory,
        connector=_UnusedConnector(),  # type: ignore[arg-type]
        workflow=_UnusedWorkflow(),  # type: ignore[arg-type]
        owner=owner,
    )
    held_connection = await engine.connect()
    try:
        backend_pid = int(await held_connection.scalar(text("SELECT pg_backend_pid()")))
        if mode == "concurrent":
            ready = Path(config["ready_path"])
            _write_json(ready, {"os_pid": os.getpid(), "pg_backend_pid": backend_pid})
            _wait_for(Path(config["go_path"]))
        claims = await execution._claim_due(["au"], utcnow())
        _write_json(
            result_path,
            {
                "ok": True,
                "mode": mode,
                "owner": owner,
                "os_pid": os.getpid(),
                "pg_backend_pid": backend_pid,
                "claims": [_claim_payload(claim) for claim in claims],
            },
        )
        if mode == "concurrent":
            _wait_for(Path(config["release_path"]))
        elif mode == "hold":
            _wait_for(Path(config["stale_path"]))
            claim = claims[0] if claims else None
            stale: dict[str, Any] = {"ok": True, "mode": mode, "stale_outcomes": {}}
            outcomes = stale["stale_outcomes"]
            outcomes["load_context"] = "lease_lost"
            try:
                await execution._load_context(claim)
            except _LeaseLost:
                pass
            else:
                outcomes["load_context"] = "unexpectedly_loaded"
            # ``_complete`` checks owner/epoch before touching the context; the
            # real method is exercised against the real database fence.
            outcomes["complete"] = await execution._complete(
                claim,
                None,
                None,  # type: ignore[arg-type]
            )
            outcomes["fail_retry"] = await execution._fail_retry(claim, "STALE_EPOCH")
            outcomes["release"] = await execution._release_with_backoff(claim, "STALE_EPOCH")
            outcomes["schedule_reconciliation"] = await execution._schedule_reconciliation(
                claim,
                "STALE_EPOCH",
                delay=None,
                increment_lookup=False,
                force_manual=True,
            )
            async with factory() as session:
                operation = await session.get(AfterSalesOperationRow, claim.operation_id)
                stale["row"] = {
                    "lease_owner": operation.lease_owner if operation else None,
                    "lease_epoch": operation.lease_epoch if operation else None,
                    "status": operation.status if operation else None,
                    "lease_until": (
                        operation.lease_until.isoformat()
                        if operation is not None and operation.lease_until is not None
                        else None
                    ),
                }
                stale["expected_skipped"] = _OUTCOME_SKIPPED
                event_count = await session.scalar(
                    text("SELECT count(*) FROM after_sales_event WHERE case_id = :case_id"),
                    {"case_id": claim.case_id},
                )
                stale["event_count"] = int(event_count or 0)
            _write_json(Path(config["stale_result_path"]), stale)
    finally:
        await held_connection.close()
        await engine.dispose()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    config = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    try:
        asyncio.run(_run(config))
    except Exception as exc:  # noqa: BLE001 - report only the safe type name
        result_path = Path(config.get("result_path", Path(argv[1]).with_suffix(".out")))
        _write_json(
            result_path,
            {
                "ok": False,
                "mode": config.get("mode"),
                "error_type": type(exc).__name__,
            },
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
