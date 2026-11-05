"""pytest bootstrap: never call real LLM/DB from developer .env."""

import os

os.environ["APP_ENV"] = "testing"
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["CROSS_ENCODER_FORCE"] = "false"
os.environ["LOCAL_EMBEDDING_FORCE"] = "false"
os.environ["UNSTRUCTURED_FORCE"] = "false"
os.environ["EMBEDDING_API_KEY"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/local/test.db"
os.environ["REDIS_URL"] = ""
os.environ["QDRANT_URL"] = ""
os.environ["LOG_LEVEL"] = "WARNING"

from pathlib import Path

import pytest

Path("data/local").mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _isolate_sse_app_status():
    """Give every test a shutdown Event bound to its own event loop.

    sse-starlette stores one process-wide ``AppStatus.should_exit_event``.
    ``TestClient`` creates a fresh loop per test, so reusing an Event across
    tests can hang or fail with a closed-loop error.
    """
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    try:
        yield
    finally:
        AppStatus.should_exit = False
        AppStatus.should_exit_event = None
