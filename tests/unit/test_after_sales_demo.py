"""Guardrails for the fixed two-repository synthetic after-sales demo.

The tests focus on the failure modes that matter for this slice:

* the Compose topology keeps DTC in E-pig's namespace, publishes only loopback
  application ports and gates both stacks on explicit migrations;
* preparation reads an immutable commit (never the dirty worktree), rejects a
  tracked live env file, and refuses unsafe symlinks;
* the generated env file is private and outside both build contexts, while the
  manifest records provenance/digests without copying secret values;
* cleanup refuses an unproven manifest, path prefix or resource label and never
  runs a global or foreign-project command.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "scripts" / "prepare_after_sales_demo.py"
COMPOSE = ROOT / "docker-compose.after-sales-demo.yml"


def _load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("dtc_prepare_after_sales_demo", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def helper() -> Any:
    return _load_helper()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    # A fixture may intentionally track a live env file to prove it is refused.
    if (repo / ".env").exists():
        _git(repo, "add", "-f", ".env")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _init_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "demo@example.invalid")
    _git(path, "config", "user.name", "Demo Fixture")
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return _commit(path, "fixture")


EPIG_DOCKERFILE = """\
# syntax=docker/dockerfile:1

FROM node:24-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

FROM base AS runner
CMD ["node", "server.js"]
"""


@pytest.fixture
def demo_repos(tmp_path: Path) -> dict[str, Any]:
    dtc = tmp_path / "dtc-source"
    epig = tmp_path / "epig-source"
    dtc_sha = _init_repo(
        dtc,
        {
            "Dockerfile": 'FROM python:3.11-slim\nCMD ["uvicorn"]\n',
            "app.py": "COMMITTED = True\n",
            ".env.example": "APP_ENV=development\n",
        },
    )
    epig_sha = _init_repo(
        epig,
        {
            "Dockerfile": EPIG_DOCKERFILE,
            "package.json": '{"name": "e-pig", "packageManager": "pnpm@10.34.5"}\n',
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            ".env.example": "MARKET=au\n",
            ".claude/skills/example/SKILL.md": "# Example\n",
        },
    )
    symlink = epig / ".agents/skills/example"
    symlink.parent.mkdir(parents=True)
    os.symlink("../../.claude/skills/example", symlink)
    external = epig / ".dtc/external.md"
    external.parent.mkdir(parents=True)
    os.symlink("../../DTC-skills/targets/e-pig.md", external)
    epig_sha = _commit(epig, "fixture symlinks")
    return {"dtc": dtc, "dtc_sha": dtc_sha, "epig": epig, "epig_sha": epig_sha}


def _prepare(
    helper: Any, repos: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Any:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return helper.prepare_demo(
        dtc_repo=repos["dtc"],
        dtc_ref=repos["dtc_sha"],
        epig_repo=repos["epig"],
        epig_ref=repos["epig_sha"],
    )


def test_compose_uses_shared_epig_namespace_and_independent_databases() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]

    for name in ("dtc-api", "dtc-worker"):
        assert services[name]["network_mode"] == "service:epig"
        assert "ports" not in services[name]

    assert services["epig"]["ports"] == [
        "127.0.0.1:${EPIG_HOST_PORT:?run scripts/prepare_after_sales_demo.py prepare first}:3000",
        "127.0.0.1:${DTC_HOST_PORT:?run scripts/prepare_after_sales_demo.py prepare first}:8000",
    ]
    assert not services["dtc-postgres"].get("ports")
    assert not services["epig-postgres"].get("ports")
    assert services["dtc-postgres"]["volumes"] != services["epig-postgres"]["volumes"]
    assert (
        services["dtc-postgres"]["environment"]["POSTGRES_USER"]
        != (services["epig-postgres"]["environment"]["POSTGRES_USER"])
    )

    dtc_env = services["dtc-api"]["environment"]
    assert dtc_env is services["dtc-worker"]["environment"]
    assert dtc_env["APP_ENV"] == "development"
    assert dtc_env["EPIG_BASE_URL"] == "http://127.0.0.1:3000"
    assert dtc_env["BUSINESS_CONNECTOR"] == "epig"
    assert dtc_env["AFTER_SALES_ENABLED"] == "true"
    for offline in (
        "DEEPSEEK_API_KEY",
        "EMBEDDING_API_KEY",
        "REDIS_URL",
        "QDRANT_URL",
        "MCP_REMOTE_URL",
        "ADMIN_API_TOKEN",
    ):
        assert dtc_env[offline] == ""
    for disabled in (
        "LOCAL_EMBEDDING_ENABLED",
        "CROSS_ENCODER_ENABLED",
        "UNSTRUCTURED_ENABLED",
        "MCP_ENABLED",
    ):
        assert dtc_env[disabled] == "false"

    epig_env = services["epig"]["environment"]
    assert epig_env["NODE_ENV"] == "production"
    assert epig_env["MARKET"] == "au"
    assert epig_env["COMMERCE_ADAPTER"] == "postgres"
    assert epig_env["AFTER_SALES_ADAPTER"] == "postgres"
    assert epig_env["DATABASE_DRIVER"] == "tcp"
    assert epig_env["APP_ORIGIN"].startswith("http://localhost:${EPIG_HOST_PORT:?")

    for service in ("dtc-migrate", "epig-migrate", "epig"):
        assert services[service]["depends_on"]
    assert services["dtc-migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["epig-migrate"]["command"] == ["pnpm", "database:migrate:all"]
    assert services["epig-migrate"]["build"]["target"] == "build"
    assert services["epig"]["build"]["target"] == "runner"
    assert services["dtc-worker"]["command"][-3:] == ["--once", "--markets", "au"]
    for name in ("dtc-api", "dtc-worker"):
        assert services[name]["depends_on"]["dtc-migrate"]["condition"] == (
            "service_completed_successfully"
        )
    assert services["dtc-api"]["healthcheck"]["test"][1].count("after-sales/me") == 1
    assert "service/requests" in services["epig"]["healthcheck"]["test"][1]


def test_postgres_healthchecks_reference_container_environment_names() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    for name in ("dtc-postgres", "epig-postgres"):
        script = services[name]["healthcheck"]["test"][1]
        referenced = {
            match.group(1) for match in re.finditer(r"\$\$\{([A-Za-z_][A-Za-z0-9_]*)\}", script)
        }
        # The shell expands these names inside the container, so each must be
        # defined by the same service's environment (not merely interpolated
        # from a differently named host/.env variable).
        assert "POSTGRES_USER" in referenced
        assert "POSTGRES_DB" in referenced
        assert referenced <= set(services[name]["environment"])


def test_prepare_uses_immutable_commit_and_keeps_env_outside_contexts(
    helper: Any,
    demo_repos: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Dirty state after the commit must not reach the archived context.
    (demo_repos["dtc"] / "app.py").write_text("COMMITTED = False\n", encoding="utf-8")
    (demo_repos["dtc"] / ".env").write_text("SHOULD_NOT_BE_ARCHIVED=1\n", encoding="utf-8")

    prepared = _prepare(helper, demo_repos, monkeypatch, tmp_path)
    try:
        assert prepared.root.stat().st_mode & 0o777 == 0o700
        assert prepared.env_file.stat().st_mode & 0o777 == 0o600
        assert prepared.manifest.stat().st_mode & 0o777 == 0o600
        assert prepared.env_file.parent == prepared.root
        for context in (prepared.root / "dtc", prepared.root / "epig"):
            assert not prepared.env_file.is_relative_to(context)

        assert (prepared.root / "dtc/app.py").read_text(encoding="utf-8") == "COMMITTED = True\n"
        assert not (prepared.root / "dtc/.env").exists()
        assert (prepared.root / "dtc/.env.example").is_file()
        assert not (prepared.root / "epig/.env").exists()
        assert (prepared.root / "epig/.env.example").is_file()
        assert (prepared.root / "epig/.agents/skills/example").is_symlink()
        assert not (prepared.root / "epig/.dtc/external.md").is_symlink()

        overlay = prepared.root / "epig" / helper.EPIG_DOCKERFILE_NAME
        text = overlay.read_text(encoding="utf-8")
        assert "# syntax=docker/dockerfile:1" not in text
        assert helper.EPIG_NODE_ARG in text
        assert text.count("FROM ${NODE_IMAGE} AS base") == 1
        assert text.index(helper.EPIG_NODE_ARG) < text.index("FROM ${NODE_IMAGE} AS base")
        # The committed Dockerfile never installs workspace dependencies.
        assert "COPY pnpm-workspace.yaml ./" in text
        assert "COPY packages ./packages" in text
        assert "COPY --from=deps /app/packages ./packages" in text
        # The source repository remains untouched.
        assert (demo_repos["epig"] / "Dockerfile").read_text(encoding="utf-8") == EPIG_DOCKERFILE

        manifest = json.loads(prepared.manifest.read_text(encoding="utf-8"))
        assert manifest["sources"]["dtc"]["commit"] == demo_repos["dtc_sha"]
        assert manifest["sources"]["epig"]["commit"] == demo_repos["epig_sha"]
        assert manifest["sources"]["dtc"]["worktree_dirty"] is True
        assert manifest["sources"]["dtc"]["worktree_excluded"] is True
        assert len(manifest["sources"]["dtc"]["archive_sha256"]) == 64
        assert prepared.compose_file.parent == prepared.root
        assert manifest["compose_file"] == str(prepared.compose_file)
        assert (
            manifest["compose_sha256"]
            == helper.hashlib.sha256(prepared.compose_file.read_bytes()).hexdigest()
        )
        assert prepared.compose_file.read_bytes() == COMPOSE.read_bytes()
        assert manifest["sources"]["epig"]["skipped_metadata_symlinks"] == [".dtc/external.md"]
        assert manifest["sources"]["epig"]["dockerfile_overlay"]["sha256"] == (
            helper.hashlib.sha256(overlay.read_bytes()).hexdigest()
        )

        env_values = dict(
            line.split("=", 1)
            for line in prepared.env_file.read_text(encoding="utf-8").splitlines()
        )
        manifest_text = prepared.manifest.read_text(encoding="utf-8")
        for key in manifest["secret_env_keys"]:
            value = env_values[key].strip("'")
            assert value
            assert value not in manifest_text
    finally:
        import shutil

        shutil.rmtree(prepared.root, ignore_errors=True)


def test_epig_overlay_refuses_an_unexpected_dockerfile(helper: Any, tmp_path: Path) -> None:
    context = tmp_path / "unexpected-dockerfile"
    context.mkdir()
    (context / "Dockerfile").write_text(
        "# syntax=docker/dockerfile:1\n\nFROM node:24-alpine AS base\nRUN true\n",
        encoding="utf-8",
    )
    with pytest.raises(helper.DemoPreparationError, match="dependency install block"):
        helper.write_epig_dockerfile_overlay(context)


def test_prepare_rejects_tracked_live_env_and_unsafe_symlinks(helper: Any, tmp_path: Path) -> None:
    live_env = tmp_path / "live-env-source"
    sha = _init_repo(
        live_env,
        {
            "Dockerfile": "FROM python:3.11-slim\n",
            ".env": "DATABASE_URL=postgresql://real\n",
        },
    )
    with pytest.raises(helper.DemoPreparationError, match="live environment file"):
        helper.prepare_context(live_env, sha, tmp_path / "live-env-context")

    escaped = tmp_path / "escaped-link-source"
    sha = _init_repo(escaped, {"README.md": "fixture\n"})
    os.symlink("/etc/passwd", escaped / "metadata-link")
    sha = _commit(escaped, "escape")
    with pytest.raises(helper.DemoPreparationError, match="unexpected symlink"):
        helper.prepare_context(escaped, sha, tmp_path / "escaped-context")

    non_metadata = tmp_path / "non-metadata-source"
    sha = _init_repo(non_metadata, {"README.md": "fixture\n"})
    os.symlink("README.md", non_metadata / "app-link")
    sha = _commit(non_metadata, "link")
    with pytest.raises(helper.DemoPreparationError, match="unexpected symlink"):
        helper.prepare_context(non_metadata, sha, tmp_path / "non-metadata-context")


def test_resolve_commit_refuses_mutable_and_short_refs(
    helper: Any, demo_repos: dict[str, Any]
) -> None:
    repo = demo_repos["dtc"]
    assert helper.resolve_commit(repo, demo_repos["dtc_sha"]) == demo_repos["dtc_sha"]
    with pytest.raises(helper.DemoPreparationError, match="40-character"):
        helper.resolve_commit(repo, "HEAD")
    with pytest.raises(helper.DemoPreparationError, match="40-character"):
        helper.resolve_commit(repo, demo_repos["dtc_sha"][:12])


class _FakeDocker:
    def __init__(self, *, labels: dict[str, str] | None, lingering_volume: bool = False) -> None:
        self.labels = labels
        self.lingering_volume = lingering_volume
        self.calls: list[list[str]] = []
        self.down_done = False

    def __call__(self, argv: Any) -> Any:
        call = [str(item) for item in argv]
        self.calls.append(call)
        if call[:1] == ["ps"]:
            remaining = self.labels and not self.down_done
            return SimpleNamespace(returncode=0, stdout="container-1\n" if remaining else "")
        if call[:1] == ["inspect"] or call[:2] in (
            ["volume", "inspect"],
            ["network", "inspect"],
        ):
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.labels) + "\n")
        if call[:2] == ["volume", "ls"]:
            return SimpleNamespace(
                returncode=0, stdout="volume-1\n" if self.lingering_volume else ""
            )
        if call[:2] == ["network", "ls"]:
            return SimpleNamespace(returncode=0, stdout="")
        if call[:1] == ["compose"]:
            self.down_done = True
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(f"unexpected docker command: {call}")


def test_cleanup_refuses_foreign_labels_and_never_runs_down(
    helper: Any,
    demo_repos: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared = _prepare(helper, demo_repos, monkeypatch, tmp_path)
    try:
        others = {helper.MARKER_LABEL: "1", helper.RUN_LABEL: "0" * 16}
        fake = _FakeDocker(labels=others)
        with pytest.raises(helper.CleanupRefused, match="ownership labels"):
            helper.cleanup_demo(prepared.manifest, docker=fake)
        assert not any(call[:1] == ["compose"] for call in fake.calls)

        wrong_prefix = json.loads(prepared.manifest.read_text(encoding="utf-8"))
        wrong_prefix["project"] = f"{helper.KIND}-{'1' * 16}"
        wrong_prefix["nonce"] = "1" * 16
        bad_manifest = prepared.root / "bad-manifest.json"
        bad_manifest.write_text(json.dumps(wrong_prefix), encoding="utf-8")
        os.chmod(bad_manifest, 0o600)
        with pytest.raises(helper.CleanupRefused, match="project prefix"):
            helper.cleanup_demo(bad_manifest, docker=_FakeDocker(labels=None))
    finally:
        import shutil

        shutil.rmtree(prepared.root, ignore_errors=True)


def test_cleanup_removes_only_the_manifest_project(
    helper: Any,
    demo_repos: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared = _prepare(helper, demo_repos, monkeypatch, tmp_path)
    fake = _FakeDocker(labels={helper.MARKER_LABEL: "1", helper.RUN_LABEL: prepared.nonce})
    helper.cleanup_demo(prepared.manifest, docker=fake)

    compose_calls = [call for call in fake.calls if call[:1] == ["compose"]]
    assert len(compose_calls) == 1
    call = compose_calls[0]
    assert call[:4] == ["compose", "--project-name", prepared.project, "--env-file"]
    assert str(prepared.compose_file) in call
    assert Path(call[call.index("-f") + 1]).name == COMPOSE.name
    assert call[-2:] == ["down", "--volumes"]
    assert not prepared.root.exists()


def test_cleanup_refuses_tampered_frozen_compose_before_docker(
    helper: Any,
    demo_repos: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared = _prepare(helper, demo_repos, monkeypatch, tmp_path)
    try:
        prepared.compose_file.write_text(
            prepared.compose_file.read_text(encoding="utf-8") + "# tampered\n",
            encoding="utf-8",
        )
        fake = _FakeDocker(labels=None)
        with pytest.raises(helper.CleanupRefused, match="frozen compose"):
            helper.cleanup_demo(prepared.manifest, docker=fake)
        assert fake.calls == []
        assert prepared.env_file.exists()
        assert prepared.manifest.exists()
    finally:
        prepared.compose_file.write_bytes(COMPOSE.read_bytes())
        import shutil

        shutil.rmtree(prepared.root, ignore_errors=True)


def test_cleanup_keeps_files_when_a_project_resource_lingers(
    helper: Any,
    demo_repos: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared = _prepare(helper, demo_repos, monkeypatch, tmp_path)
    try:
        fake = _FakeDocker(
            labels={helper.MARKER_LABEL: "1", helper.RUN_LABEL: prepared.nonce},
            lingering_volume=True,
        )
        with pytest.raises(helper.CleanupRefused, match="resources remain"):
            helper.cleanup_demo(prepared.manifest, docker=fake)
        assert prepared.root.exists()
        assert prepared.env_file.exists()
        assert prepared.manifest.exists()
    finally:
        import shutil

        shutil.rmtree(prepared.root, ignore_errors=True)


def test_cleanup_timeout_keeps_manifest_env_and_root(
    helper: Any,
    demo_repos: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared = _prepare(helper, demo_repos, monkeypatch, tmp_path)
    try:

        def timeout(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="docker", timeout=helper.DOCKER_TIMEOUT_SECONDS)

        monkeypatch.setattr(helper.subprocess, "run", timeout)
        with pytest.raises(helper.CleanupRefused, match="timed out"):
            helper.cleanup_demo(prepared.manifest)
        assert prepared.root.exists()
        assert prepared.env_file.exists()
        assert prepared.manifest.exists()
    finally:
        import shutil

        shutil.rmtree(prepared.root, ignore_errors=True)
