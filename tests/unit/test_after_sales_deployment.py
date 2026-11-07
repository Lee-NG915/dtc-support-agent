"""Guardrails for the isolated after-sales deployment assets.

These tests read the deployment files as configuration data. They catch the
failure modes that matter for this slice: Compose accidentally loading a
developer ``.env``, publishing PostgreSQL or a non-loopback API, starting the
API/worker before migrations succeed, reintroducing paid/cloud defaults, and
building the image from a mutable or secret-bearing context.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.after-sales.yml"
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _dockerfile_env() -> dict[str, str]:
    """Read the image's ENV defaults without invoking Docker."""
    defaults: dict[str, str] = {}
    in_env = False
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not in_env:
            if not text.startswith("ENV "):
                continue
            in_env = True
            text = text[4:]
        continued = text.endswith("\\")
        text = text.rstrip("\\").strip()
        for token in text.split():
            key, separator, value = token.partition("=")
            if separator and key:
                defaults[key] = value.strip('"')
        if not continued:
            in_env = False
    return defaults


def _dockerignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _pattern_matches(path: str, pattern: str) -> bool:
    """Approximate Docker's ignore matching for repository-relative paths."""
    negated = pattern.startswith("!")
    pattern = pattern[1:] if negated else pattern
    directory_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if not pattern:
        return False
    path = path.strip("/")
    parts = path.split("/")
    if "/" in pattern:
        candidates = ["/".join(parts[:depth]) for depth in range(1, len(parts) + 1)]
        matched = any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates)
    else:
        matched = any(fnmatch.fnmatchcase(part, pattern) for part in parts)
    # A directory-only pattern also covers everything below the directory.
    if not matched and directory_only and "/" in pattern:
        matched = path.startswith(pattern + "/")
    return matched


def is_dockerignored(path: str) -> bool:
    ignored = False
    for pattern in _dockerignore_patterns():
        negated = pattern.startswith("!")
        stripped = pattern[1:] if negated else pattern
        if _pattern_matches(path, stripped):
            ignored = not negated
    return ignored


def test_postgres_is_network_only_postgres_16() -> None:
    postgres = _compose()["services"]["postgres"]
    assert re.fullmatch(r"postgres:16(?:-alpine)?", postgres["image"])
    assert not postgres.get("ports")
    assert "healthcheck" in postgres


def test_only_api_is_published_and_only_on_loopback() -> None:
    services = _compose()["services"]
    published = {name for name, service in services.items() if service.get("ports")}
    assert published == {"api"}
    for mapping in services["api"]["ports"]:
        assert str(mapping).startswith("127.0.0.1:")


def test_migration_gates_api_and_worker() -> None:
    services = _compose()["services"]
    migrate = services["migrate"]
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert "ALEMBIC_DATABASE_URL" in migrate["environment"]
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"

    for name in ("api", "worker"):
        dependency = services[name]["depends_on"]["migrate"]
        assert dependency["condition"] == "service_completed_successfully"


def test_compose_never_reads_an_env_file() -> None:
    services = _compose()["services"]
    assert all("env_file" not in service for service in services.values())


def test_app_services_default_to_explicit_offline_fake_configuration() -> None:
    services = _compose()["services"]
    env = services["api"]["environment"]
    assert env == services["worker"]["environment"]
    assert env["APP_ENV"] in {"development", "testing"}
    assert env["AFTER_SALES_ENABLED"] == "true"
    assert env["BUSINESS_CONNECTOR"] == "fake"
    assert env["DEEPSEEK_API_KEY"] == ""
    assert env["EMBEDDING_API_KEY"] == ""
    assert env["REDIS_URL"] == ""
    assert env["QDRANT_URL"] == ""
    assert env["MCP_ENABLED"] == "false"
    assert env["LOCAL_EMBEDDING_ENABLED"] == "false"
    assert env["CROSS_ENCODER_ENABLED"] == "false"
    assert env["UNSTRUCTURED_ENABLED"] == "false"
    assert env["EPIG_SERVICE_TOKEN"] == ""
    assert env["ADMIN_API_TOKEN"] == ""


def test_image_defaults_keep_production_and_disable_local_models(monkeypatch) -> None:
    image_env = _dockerfile_env()
    assert image_env["APP_ENV"] == "production"
    for key in (
        "LOCAL_EMBEDDING_ENABLED",
        "CROSS_ENCODER_ENABLED",
        "UNSTRUCTURED_ENABLED",
    ):
        assert image_env[key] == "false"

    managed = (
        "APP_ENV",
        "ADMIN_API_TOKEN",
        "LOCAL_EMBEDDING_ENABLED",
        "CROSS_ENCODER_ENABLED",
        "UNSTRUCTURED_ENABLED",
        "LOCAL_EMBEDDING_FORCE",
        "CROSS_ENCODER_FORCE",
        "UNSTRUCTURED_FORCE",
    )
    for key in managed:
        monkeypatch.delenv(key, raising=False)
    for key in (
        "APP_ENV",
        "LOCAL_EMBEDDING_ENABLED",
        "CROSS_ENCODER_ENABLED",
        "UNSTRUCTURED_ENABLED",
    ):
        monkeypatch.setenv(key, image_env[key])

    from starlette.requests import Request

    from app.api.routes.admin import require_admin
    from app.config.settings import Settings
    from app.core.exceptions import UnauthorizedError

    settings = Settings(_env_file=None)
    assert settings.app_env == "production"
    assert settings.local_embedding_configured is False
    assert settings.cross_encoder_configured is False
    assert settings.unstructured_configured is False

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    container = SimpleNamespace(settings=settings)
    with pytest.raises(UnauthorizedError):
        asyncio.run(require_admin(request, container))

    # Regression context: if the image ENV were removed, Settings would fall
    # back to development and a blank admin token would allow anonymous access.
    monkeypatch.delenv("APP_ENV", raising=False)
    fallback = Settings(_env_file=None)
    assert fallback.app_env == "development"
    assert asyncio.run(require_admin(request, SimpleNamespace(settings=fallback))) is None


def test_database_password_is_interpolated_not_embedded() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${POSTGRES_PASSWORD:?" in text
    # The only startup paths that need the secret use interpolation.
    services = _compose()["services"]
    assert "${POSTGRES_PASSWORD:?" in services["postgres"]["environment"]["POSTGRES_PASSWORD"]
    assert "${POSTGRES_PASSWORD:?" in services["migrate"]["environment"]["ALEMBIC_DATABASE_URL"]
    assert "${POSTGRES_PASSWORD:?" in services["api"]["environment"]["DATABASE_URL"]


def test_dockerfile_installs_only_the_frozen_lock_environment() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+", text)
    assert "uv sync --frozen --no-dev" in text
    assert "pip install" not in text
    assert "apt-get upgrade" not in text
    assert "org.opencontainers.image.revision" in text
    assert re.search(r"COPY\s+.*\buv\.lock\b", text)


def test_dockerfile_copies_runtime_and_migration_assets() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    for required in (
        "COPY alembic.ini",
        "COPY alembic ",
        "COPY app ",
        "COPY knowledge ",
        "frontend/dist",
    ):
        assert required in text
    assert "alembic.ini" in text
    assert 'CMD ["uvicorn", "app.asgi:app"' in text


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        ".work/run.env",
        ".code-review-graph/cache.db",
        "data/local/app.db",
        "eval/reports/run.json",
        "logs/api.log",
        ".coverage",
        "secrets/pg.password",
        "deploy/secrets/token",
        "frontend/node_modules/react/index.js",
        "tests/unit/test_after_sales_deployment.py",
    ],
)
def test_build_context_excludes_local_state_and_secrets(path: str) -> None:
    assert is_dockerignored(path), path


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pyproject.toml",
        "README.md",
        "frontend/package.json",
        "frontend/package-lock.json",
        "alembic.ini",
        "alembic/env.py",
        "alembic/versions/0005_after_sales_operation_reconciliation.py",
        "app/main.py",
        "knowledge/shipping.md",
    ],
)
def test_build_context_keeps_locked_runtime_sources(path: str) -> None:
    assert not is_dockerignored(path), path
