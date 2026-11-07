"""Container build lifecycle: acquired resources are released on failure or cancel."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import app.services.container as container_mod
from app.connectors.fake import FakeConnector
from eval import runner


def _settings(tmp_path: Path, **updates):
    settings = runner.offline_settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'container.sqlite3'}"
    )
    return settings.model_copy(update=updates)


@pytest.fixture
def dispose_spy(monkeypatch):
    disposed: list[object] = []
    real_dispose = AsyncEngine.dispose

    async def tracked_dispose(self, close: bool = True):
        disposed.append(self)
        return await real_dispose(self, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", tracked_dispose)
    return disposed


@pytest.fixture
def llm_close_spy(monkeypatch):
    closed: list[object] = []

    async def tracked_close(self):
        closed.append(self)

    monkeypatch.setattr(
        container_mod.OfflineLLMProvider,
        "aclose",
        tracked_close,
        raising=False,
    )
    return closed


async def test_normal_container_resources_usable_and_closed(
    tmp_path: Path,
    dispose_spy,
    llm_close_spy,
) -> None:
    settings = _settings(
        tmp_path,
        after_sales_enabled=True,
        business_connector="fake",
    )
    container = await container_mod.build_container(settings, seed=True)

    assert dispose_spy == []
    async with container.session_factory() as session:
        assert (await session.execute(text("SELECT 1"))).scalar() == 1
    assert container.retriever is not None
    assert container.vector_store is not None
    assert container.business_connector is not None
    assert container.business_connector.closed is False

    await container.aclose()

    assert container.business_connector.closed is True
    assert llm_close_spy
    assert dispose_spy


async def test_schema_failure_disposes_engine_and_closes_llm(
    tmp_path: Path,
    dispose_spy,
    llm_close_spy,
    monkeypatch,
) -> None:
    async def fail_schema(engine) -> None:
        raise RuntimeError("schema failed")

    monkeypatch.setattr(container_mod, "init_schema", fail_schema)

    with pytest.raises(RuntimeError, match="schema failed"):
        await container_mod.build_container(_settings(tmp_path))

    assert dispose_spy, "engine created before init_schema must be disposed"
    assert llm_close_spy, "llm acquired before infrastructure must be closed"


async def test_redis_connect_failure_disposes_engine(
    tmp_path: Path,
    dispose_spy,
    llm_close_spy,
    monkeypatch,
) -> None:
    async def fail_redis(settings):
        raise RuntimeError("redis failed")

    monkeypatch.setattr(container_mod, "connect_redis", fail_redis)

    with pytest.raises(RuntimeError, match="redis failed"):
        await container_mod.build_container(_settings(tmp_path))

    assert dispose_spy
    assert llm_close_spy


async def test_retrieval_failure_closes_redis_engine_and_llm(
    tmp_path: Path,
    dispose_spy,
    llm_close_spy,
    monkeypatch,
) -> None:
    redis_closed: list[str] = []

    class _Redis:
        async def aclose(self) -> None:
            redis_closed.append("redis")

    async def fake_redis(settings):
        return _Redis()

    async def fail_retrieval(settings, redis):
        raise RuntimeError("retrieval failed")

    monkeypatch.setattr(container_mod, "connect_redis", fake_redis)
    monkeypatch.setattr(container_mod, "build_retrieval", fail_retrieval)

    with pytest.raises(RuntimeError, match="retrieval failed"):
        await container_mod.build_container(_settings(tmp_path))

    assert redis_closed == ["redis"]
    assert dispose_spy
    assert llm_close_spy


async def test_seed_failure_releases_resources(
    tmp_path: Path,
    dispose_spy,
    llm_close_spy,
    monkeypatch,
) -> None:
    async def fail_seed(session) -> None:
        raise RuntimeError("seed failed")

    monkeypatch.setattr(container_mod, "seed_demo_data", fail_seed)

    with pytest.raises(RuntimeError, match="seed failed"):
        await container_mod.build_container(_settings(tmp_path), seed=True)

    assert dispose_spy
    assert llm_close_spy


async def test_connector_close_failure_does_not_mask_original_or_skip_others(
    tmp_path: Path,
    dispose_spy,
    llm_close_spy,
    monkeypatch,
) -> None:
    class _OriginalError(RuntimeError):
        pass

    async def broken_connector_close(self) -> None:
        raise RuntimeError("connector close failed")

    def broken_workflow(*args, **kwargs):
        raise _OriginalError("workflow assembly failed")

    monkeypatch.setattr(FakeConnector, "aclose", broken_connector_close)
    monkeypatch.setattr(container_mod, "AfterSalesWorkflow", broken_workflow)
    settings = _settings(
        tmp_path,
        after_sales_enabled=True,
        business_connector="fake",
    )

    with pytest.raises(_OriginalError, match="workflow assembly failed"):
        await container_mod.build_container(settings)

    assert dispose_spy, "engine must still be disposed when connector close fails"
    assert llm_close_spy, "llm must still be closed when connector close fails"


async def test_build_cancellation_releases_resources(
    tmp_path: Path,
    dispose_spy,
    llm_close_spy,
    monkeypatch,
) -> None:
    async def cancel_retrieval(settings, redis):
        raise asyncio.CancelledError()

    monkeypatch.setattr(container_mod, "build_retrieval", cancel_retrieval)

    with pytest.raises(asyncio.CancelledError):
        await container_mod.build_container(_settings(tmp_path))

    assert dispose_spy
    assert llm_close_spy


class _HealthyEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class _BrokenEngine:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def dispose(self) -> None:
        raise self._error


class _HealthyRedis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _BrokenRedis:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def aclose(self) -> None:
        raise self._error


def _cleanup_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == container_mod.logger.name]


async def test_closer_failure_log_has_no_sensitive_details(
    tmp_path: Path,
    llm_close_spy,
    caplog,
    monkeypatch,
) -> None:
    canary = "synthetic-closer-canary"
    secret_url = f"redis://default:{canary}@cache.internal:6379/0"
    original = RuntimeError("retrieval failed")
    engine = _HealthyEngine()
    redis = _BrokenRedis(RuntimeError(f"close failed url={secret_url} password={canary}"))

    async def fake_infrastructure(settings):
        return engine, object(), redis

    async def fail_retrieval(settings, redis):
        raise original

    monkeypatch.setattr(container_mod, "build_infrastructure", fake_infrastructure)
    monkeypatch.setattr(container_mod, "build_retrieval", fail_retrieval)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError) as excinfo:
            await container_mod.build_container(_settings(tmp_path))

    assert excinfo.value is original, "cleanup must not mask the build failure"
    assert engine.disposed, "engine must still be disposed when redis close fails"
    assert llm_close_spy, "llm must still be closed when redis close fails"

    records = _cleanup_records(caplog)
    assert len(records) == 1
    assert all(record.exc_info is None for record in records)
    message = records[0].getMessage()
    assert "resource=_BrokenRedis" in message
    assert "error_type=RuntimeError" in message
    assert canary not in caplog.text
    assert secret_url not in caplog.text


async def test_engine_dispose_failure_log_has_no_sensitive_details(
    tmp_path: Path,
    llm_close_spy,
    caplog,
    monkeypatch,
) -> None:
    canary = "synthetic-dispose-canary"
    secret_url = f"postgresql+asyncpg://agent:{canary}@db.internal:5432/prod"
    original = RuntimeError("retrieval failed")
    redis = _HealthyRedis()
    engine = _BrokenEngine(RuntimeError(f"dispose failed dsn={secret_url} password={canary}"))

    async def fake_infrastructure(settings):
        return engine, object(), redis

    async def fail_retrieval(settings, redis):
        raise original

    monkeypatch.setattr(container_mod, "build_infrastructure", fake_infrastructure)
    monkeypatch.setattr(container_mod, "build_retrieval", fail_retrieval)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError) as excinfo:
            await container_mod.build_container(_settings(tmp_path))

    assert excinfo.value is original, "cleanup must not mask the build failure"
    assert redis.closed, "redis must still be closed when engine dispose fails"
    assert llm_close_spy, "llm must still be closed when engine dispose fails"

    records = _cleanup_records(caplog)
    assert len(records) == 1
    assert all(record.exc_info is None for record in records)
    message = records[0].getMessage()
    assert "resource=engine" in message
    assert "error_type=RuntimeError" in message
    assert canary not in caplog.text
    assert secret_url not in caplog.text
