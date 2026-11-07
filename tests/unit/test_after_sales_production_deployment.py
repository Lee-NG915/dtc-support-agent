"""Guardrails for the production deployment configuration baseline.

The tests treat Compose, the env template, the preflight checker, the Tailscale
runbook, ``.dockerignore`` and the CI workflow as configuration data. They do
not deploy anything, contact a cloud service or use a real secret.

The checker is exercised directly with temporary 0400/0600 files so every
positive and negative case is independent of the repository ``.env``.
"""

from __future__ import annotations

import fnmatch
import json
import re
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.production.yml"
TAILSCALE_DOC = ROOT / "deploy" / "tailscale-serve.md"
ENV_EXAMPLE = ROOT / "deploy" / "dtc.production.env.example"
CHECKER = ROOT / "scripts" / "check_production_settings.py"
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

_SECRET_SENTINELS = (
    "pg-password-sentinel",
    "epig-service-token-sentinel",
    "admin-token-sentinel",
    "deepseek-key-sentinel",
)


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


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


# ---------------------------------------------------------------------------
# Production Compose
# ---------------------------------------------------------------------------


def test_compose_has_only_the_expected_services() -> None:
    assert set(_compose()["services"]) == {"postgres", "migrate", "api", "worker"}


def test_postgres_is_private_and_persistent() -> None:
    postgres = _compose()["services"]["postgres"]
    assert re.fullmatch(r"postgres:16(?:-alpine)?", postgres["image"])
    assert not postgres.get("ports")
    assert postgres["volumes"] == ["dtc-postgres-data:/var/lib/postgresql/data"]
    assert postgres["healthcheck"]["test"][0] == "CMD-SHELL"
    assert "pg_isready" in postgres["healthcheck"]["test"][1]


def test_env_file_is_required_and_no_repository_dotenv_is_loaded() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    required = "${DTC_ENV_FILE:?DTC_ENV_FILE is required}"
    for service in _compose()["services"].values():
        assert service["env_file"] == [required]
    assert "${DTC_ENV_FILE:-" not in text
    assert "--env-file /dev/null" in text
    assert not re.search(r"^\s*-\s*['\"]?\.env['\"]?\s*$", text, re.MULTILINE)


def test_api_is_loopback_only_and_no_other_service_publishes_ports() -> None:
    services = _compose()["services"]
    published = {name for name, service in services.items() if service.get("ports")}
    assert published == {"api"}
    assert services["api"]["ports"] == ["127.0.0.1:8000:8000"]
    text = COMPOSE.read_text(encoding="utf-8")
    assert "0.0.0.0:8000" not in text
    assert "network_mode: host" not in text


def test_migration_gates_api_and_worker() -> None:
    services = _compose()["services"]
    migrate = services["migrate"]
    command = " ".join(str(part) for part in migrate["command"])
    assert 'ALEMBIC_DATABASE_URL="$$DATABASE_URL"' in command
    assert "alembic upgrade head" in command
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert migrate["restart"] == "no"

    for name in ("api", "worker"):
        assert (
            services[name]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
        )


def test_worker_is_continuous_and_restartable() -> None:
    services = _compose()["services"]
    assert services["worker"]["command"] == ["python", "-m", "app.worker"]
    assert "--once" not in services["worker"]["command"]
    for name in ("api", "worker"):
        assert services[name]["restart"] == "unless-stopped"
        assert services[name]["stop_grace_period"]


def test_app_containers_are_hardened_and_logs_rotate() -> None:
    services = _compose()["services"]
    for name in ("api", "worker", "migrate"):
        service = services[name]
        assert service["read_only"] is True
        assert "/tmp" in service["tmpfs"]
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["user"] == "1000:1000"
        assert service["logging"]["driver"] == "json-file"
        assert service["logging"]["options"]["max-size"] == "10m"
        assert service["logging"]["options"]["max-file"] == "5"


def test_no_local_redis_qdrant_or_public_edge_proxy() -> None:
    services = _compose()["services"]
    assert not {"redis", "qdrant", "caddy", "nginx"} & set(services)
    joined = " ".join(str(service) for service in services.values()).lower()
    assert "redis:" not in joined
    assert "qdrant:" not in joined


def test_image_defaults_remain_production_and_non_root() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "APP_ENV=production" in text
    assert "LOCAL_EMBEDDING_ENABLED=false" in text
    assert "CROSS_ENCODER_ENABLED=false" in text
    assert "UNSTRUCTURED_ENABLED=false" in text
    assert "USER appuser" in text
    assert "EXPOSE 8000" in text
    assert "org.opencontainers.image.revision" in text
    assert "uv sync --frozen --no-dev" in text


# ---------------------------------------------------------------------------
# Tailscale ingress runbook
# ---------------------------------------------------------------------------


def test_tailscale_doc_mounts_only_reviewed_paths() -> None:
    text = TAILSCALE_DOC.read_text(encoding="utf-8")
    assert "--https=443" in text
    assert "--set-path=/staff http://127.0.0.1:8000/staff" in text
    assert "--set-path=/api http://127.0.0.1:8000/api" in text
    assert "--set-path=/assets http://127.0.0.1:8000/assets" in text
    assert "--set-path=/favicon.svg http://127.0.0.1:8000/favicon.svg" in text
    assert set(re.findall(r"--set-path=(\S+)", text)) == {
        "/staff",
        "/api",
        "/assets",
        "/favicon.svg",
    }
    assert "`/staff`、`/staff/*`" in text
    assert "`/api/*`" in text
    assert "`/assets`、`/assets/*`" in text
    assert "`/favicon.svg`" in text
    assert "不要在 `443` 上挂载 `/`" in text
    assert "当前静态资源限制" not in text


def test_tailscale_doc_excludes_metrics_and_requires_private_boundaries() -> None:
    text = TAILSCALE_DOC.read_text(encoding="utf-8")
    assert "/metrics" in text
    assert "只允许主机本机访问" in text
    assert "不挂载" in text
    assert "tag:dtc-host:443" in text
    assert "curl --connect-timeout" in text
    assert "nc -vz" in text
    assert "ss -ltnp" in text
    assert "tailscale serve reset" in text
    assert "tailscale down" in text


def test_tailscale_doc_has_no_real_tokens_or_identities() -> None:
    text = TAILSCALE_DOC.read_text(encoding="utf-8")
    assert "tskey-" not in text
    assert "ADMIN_API_TOKEN" not in text
    assert "EPIG_SERVICE_TOKEN" not in text
    # Placeholders only; no concrete tailnet name or employee address.
    assert "<host>.<tailnet>.ts.net" in text
    assert "@example.invalid" in text


# ---------------------------------------------------------------------------
# Production env template and its preflight
# ---------------------------------------------------------------------------


def test_env_example_covers_required_production_keys() -> None:
    from scripts.check_production_settings import parse_env_file

    env = parse_env_file(ENV_EXAMPLE.read_text(encoding="utf-8"))
    assert env["APP_ENV"] == "production"
    assert env["AFTER_SALES_ENABLED"] == "true"
    assert env["BUSINESS_CONNECTOR"] == "epig"
    assert env["AFTER_SALES_DEMO_LOGIN_ENABLED"] == "false"
    assert env["AFTER_SALES_WORKER_MARKETS"]
    assert env["EPIG_ALLOWED_MARKETS"]
    assert json.loads(env["AFTER_SALES_SKU_CATEGORY_MAP"])
    assert env["PRODUCTION_ACKNOWLEDGE_BM25_ONLY"] == "false"
    assert env["DATABASE_URL"].startswith("postgresql+asyncpg://")
    assert "@postgres:5432/" in env["DATABASE_URL"]
    assert env["LOCAL_EMBEDDING_ENABLED"] == "false"
    assert env["CROSS_ENCODER_ENABLED"] == "false"
    assert env["UNSTRUCTURED_ENABLED"] == "false"
    assert env["REDIS_URL"] == ""
    assert env["QDRANT_URL"] == ""


def test_env_example_contains_placeholders_not_secrets() -> None:
    from scripts.check_production_settings import parse_env_file

    env = parse_env_file(ENV_EXAMPLE.read_text(encoding="utf-8"))
    for key in ("POSTGRES_PASSWORD", "EPIG_SERVICE_TOKEN", "ADMIN_API_TOKEN", "DEEPSEEK_API_KEY"):
        assert env[key].startswith("replace-with-"), key


_VALID_ENV: dict[str, str] = {
    "APP_ENV": "production",
    "AFTER_SALES_ENABLED": "true",
    "BUSINESS_CONNECTOR": "epig",
    "POSTGRES_USER": "dtc",
    "POSTGRES_PASSWORD": "pg-password-sentinel",
    "POSTGRES_DB": "dtc_production",
    "DATABASE_URL": ("postgresql+asyncpg://dtc:pg-password-sentinel@postgres:5432/dtc_production"),
    "EPIG_BASE_URL": "https://epig.production.example.com",
    "EPIG_SERVICE_TOKEN": "epig-service-token-sentinel",
    "EPIG_ALLOWED_MARKETS": "au,sg,us",
    "AFTER_SALES_SKU_CATEGORY_MAP": '{"SKU-1":"homewares"}',
    "AFTER_SALES_DEMO_LOGIN_ENABLED": "false",
    "AFTER_SALES_WORKER_MARKETS": "au,sg",
    "ADMIN_API_TOKEN": "admin-token-sentinel",
    "DEEPSEEK_API_KEY": "deepseek-key-sentinel",
    "EMBEDDING_API_KEY": "",
    "EMBEDDING_BASE_URL": "",
    "PRODUCTION_ACKNOWLEDGE_BM25_ONLY": "true",
}


def _write_env(
    tmp_path: Path,
    overrides: dict[str, str | None] | None = None,
    *,
    mode: int = 0o600,
    name: str = "production.env",
) -> Path:
    env = dict(_VALID_ENV)
    for key, value in (overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    path = tmp_path / name
    path.write_text("\n".join(f"{key}={value}" for key, value in env.items()) + "\n")
    path.chmod(mode)
    return path


def test_checker_accepts_valid_0600_env(tmp_path: Path, capsys) -> None:
    from scripts.check_production_settings import main

    path = _write_env(tmp_path)
    assert main(["--env-file", str(path)]) == 0
    captured = capsys.readouterr()
    assert "[FAIL]" not in captured.out
    assert "BM25-only retrieval acknowledged" in captured.out
    for sentinel in _SECRET_SENTINELS:
        assert sentinel not in captured.out
        assert sentinel not in captured.err


def test_checker_accepts_0400_env(tmp_path: Path, capsys) -> None:
    from scripts.check_production_settings import main

    path = _write_env(tmp_path, mode=0o400)
    assert main(["--env-file", str(path)]) == 0
    assert "[FAIL]" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"APP_ENV": "development"}, "APP_ENV must be production"),
        ({"BUSINESS_CONNECTOR": "fake"}, "BUSINESS_CONNECTOR must be epig"),
        (
            {"DATABASE_URL": "sqlite+aiosqlite:///./data/local/app.db"},
            "DATABASE_URL dialect is sqlite+aiosqlite",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+asyncpg://dtc:pg-password-sentinel@127.0.0.1:5432/" "dtc_production"
                )
            },
            "must target the Compose host postgres",
        ),
        ({"EPIG_BASE_URL": "http://epig.production.example.com"}, "absolute HTTPS URL"),
        (
            {"EPIG_BASE_URL": "https://epig.example.invalid"},
            "must not use the reserved .invalid domain",
        ),
        ({"EPIG_SERVICE_TOKEN": ""}, "EPIG_SERVICE_TOKEN must be set"),
        ({"EPIG_SERVICE_TOKEN": "short"}, "EPIG_SERVICE_TOKEN must be at least 16 characters"),
        ({"EPIG_ALLOWED_MARKETS": ""}, "EPIG_ALLOWED_MARKETS must list"),
        ({"AFTER_SALES_SKU_CATEGORY_MAP": ""}, "AFTER_SALES_SKU_CATEGORY_MAP"),
        (
            {"AFTER_SALES_SKU_CATEGORY_MAP": ('{"REPLACE-WITH-REVIEWED-SKU":"homewares"}')},
            "must not contain placeholder",
        ),
        ({"AFTER_SALES_DEMO_LOGIN_ENABLED": "true"}, "must be false in production"),
        ({"AFTER_SALES_WORKER_MARKETS": ""}, "AFTER_SALES_WORKER_MARKETS must list"),
        (
            {
                "EPIG_ALLOWED_MARKETS": "au,sg",
                "AFTER_SALES_WORKER_MARKETS": "au,us",
            },
            "outside EPIG_ALLOWED_MARKETS: us",
        ),
        (
            {
                "EPIG_ALLOWED_MARKETS": "au",
                "AFTER_SALES_WORKER_MARKETS": "au,sg",
            },
            "outside EPIG_ALLOWED_MARKETS: sg",
        ),
        ({"ADMIN_API_TOKEN": ""}, "ADMIN_API_TOKEN must be set"),
        ({"ADMIN_API_TOKEN": "short"}, "ADMIN_API_TOKEN must be at least 16 characters"),
        ({"ADMIN_API_TOKEN": "CHANGE_ME"}, "ADMIN_API_TOKEN must not be a placeholder"),
        ({"DEEPSEEK_API_KEY": ""}, "DEEPSEEK_API_KEY must be set"),
        ({"DEEPSEEK_API_KEY": "short"}, "DEEPSEEK_API_KEY must be at least 16 characters"),
        (
            {"PRODUCTION_ACKNOWLEDGE_BM25_ONLY": None},
            "PRODUCTION_ACKNOWLEDGE_BM25_ONLY=true",
        ),
        (
            {"PRODUCTION_ACKNOWLEDGE_BM25_ONLY": None, "EMBEDDING_API_KEY": "key-only"},
            "must be configured together or both omitted",
        ),
        ({"POSTGRES_PASSWORD": ""}, "POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB"),
        (
            {
                "DATABASE_URL": (
                    "postgresql+asyncpg://dtc:different-password@postgres:5432/" "dtc_production"
                )
            },
            "must match POSTGRES_*",
        ),
    ],
)
def test_checker_rejects_each_production_defect(
    tmp_path: Path,
    capsys,
    overrides: dict[str, str | None],
    expected: str,
) -> None:
    from scripts.check_production_settings import main

    path = _write_env(tmp_path, overrides)
    assert main(["--env-file", str(path)]) == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert expected in output
    for sentinel in _SECRET_SENTINELS:
        assert sentinel not in output


def test_checker_rejects_unsafe_file_mode(tmp_path: Path, capsys) -> None:
    from scripts.check_production_settings import main

    path = _write_env(tmp_path, mode=0o644)
    assert main(["--env-file", str(path)]) == 2
    captured = capsys.readouterr()
    assert "permissions must be 0400 or 0600, got 0644" in captured.err
    for sentinel in _SECRET_SENTINELS:
        assert sentinel not in captured.out + captured.err


def test_checker_rejects_missing_and_malformed_input(tmp_path: Path, capsys) -> None:
    from scripts.check_production_settings import main

    assert main(["--env-file", str(tmp_path / "missing.env")]) == 2
    assert "does not exist" in capsys.readouterr().err

    malformed = tmp_path / "malformed.env"
    malformed.write_text("NOT_AN_ASSIGNMENT\n")
    malformed.chmod(0o600)
    assert main(["--env-file", str(malformed)]) == 2
    captured = capsys.readouterr()
    assert "invalid dotenv assignment on line 1" in captured.err
    assert "NOT_AN_ASSIGNMENT" not in captured.out + captured.err


def test_parser_never_executes_shell_syntax(tmp_path: Path) -> None:
    from scripts.check_production_settings import parse_env_file

    marker = tmp_path / "executed"
    values = parse_env_file(f"ADMIN_API_TOKEN=$(touch {marker})\n")
    assert values["ADMIN_API_TOKEN"] == f"$(touch {marker})"
    assert not marker.exists()


def test_checker_requires_explicit_env_file(tmp_path: Path, capsys, monkeypatch) -> None:
    from scripts.check_production_settings import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in _VALID_ENV.items()) + "\n"
    )
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "[PASS]" not in captured.out


def test_checker_template_copy_is_rejected(tmp_path: Path, capsys) -> None:
    from scripts.check_production_settings import main

    path = tmp_path / "template.env"
    path.write_bytes(ENV_EXAMPLE.read_bytes())
    path.chmod(0o600)
    assert main(["--env-file", str(path)]) == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "[FAIL]" in captured.out
    assert "must not be a placeholder" in output
    assert "reserved .invalid domain" in output
    assert "PRODUCTION_ACKNOWLEDGE_BM25_ONLY=true" in output
    for sentinel in _SECRET_SENTINELS:
        assert sentinel not in output


# ---------------------------------------------------------------------------
# Build context and CI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        "deploy/dtc.production.env",
        "deploy/dtc.production.env.example",
        "deploy/production.env",
        "deploy/tls/server.crt",
        "deploy/certs/server.pem",
        "deploy/private.key",
        "deploy/secrets/pg.password",
        "nested/credentials.json",
        ".work/run.env",
    ],
)
def test_build_context_excludes_production_env_secrets_and_certs(path: str) -> None:
    assert is_dockerignored(path), path


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pyproject.toml",
        "Dockerfile",
        "alembic.ini",
        "alembic/env.py",
        "app/main.py",
        "knowledge/shipping.md",
        "frontend/package.json",
        "frontend/package-lock.json",
        "deploy/tailscale-serve.md",
    ],
)
def test_build_context_keeps_locked_runtime_sources(path: str) -> None:
    assert not is_dockerignored(path), path


def test_ci_has_container_build_job_without_registry_access() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["container-build"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] >= 20

    uses = [step.get("uses", "") for step in job["steps"]]
    assert "actions/checkout@v4" in uses
    assert "docker/setup-buildx-action@v3" in uses
    assert not any(step.startswith("docker/login-action") for step in uses)

    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "docker build" in commands
    assert "--build-arg" in commands
    assert "IMAGE_REVISION" in commands
    assert "org.opencontainers.image.revision" in commands
    assert "GITHUB_SHA" in commands
    assert "--push" not in commands
    assert "docker push" not in commands
    assert "push: true" not in commands
    assert "docker compose" not in commands
    assert "registry" not in commands.lower()


def test_checker_script_is_not_a_shell_wrapper() -> None:
    text = CHECKER.read_text(encoding="utf-8")
    assert "subprocess" not in text
    assert "os.system" not in text
    assert "source " not in text
    assert stat.S_IMODE(CHECKER.stat().st_mode) & 0o111 == 0
