#!/usr/bin/env python3
"""Prepare and clean up the fixed two-repository after-sales demo.

The helper is deliberately narrow:

* both source contexts are extracted from explicit, full commit SHAs with
  ``git archive``; the working tree, index and any ``.env`` are never read;
* extraction rejects path traversal, non-metadata symlinks, hard links and
  any tracked ``.env``/``.env.*`` other than the ``.env.example`` placeholder;
* the generated directory is a new private 0700 temporary directory outside
  the repository, and the env file/manifest are created 0600;
* the env file lives beside, never inside, the two build contexts;
* E-pig's Dockerfile is the only source overlay: a same-official-base
  ``ARG NODE_IMAGE`` replaces the exact base ``FROM`` line and the explicit
  ``# syntax=`` download is dropped. The committed deps/build stages also omit
  workspace manifests/node_modules, so the controlled copy adds only those
  COPY lines; package.json, pnpm-lock.yaml and all other instructions are
  untouched;
* the Compose file is frozen into the run root with its SHA-256; cleanup reads
  that frozen copy and never the mutable repository file;
* cleanup accepts only this helper's manifest, verifies the path prefix, the
  frozen compose digest and this run's resource labels, runs a project-scoped
  ``compose down``, and only then removes the manifest/env/root when no project
  resource remains. There is no global prune, ``rm -rf`` or foreign command.

Nothing here prints a password, token or connection URL. Only exact paths,
ports, revisions and the next command are shown.

Usage::

    python scripts/prepare_after_sales_demo.py prepare \
      --dtc-ref <full-40-char-sha> \
      --epig-repo /path/to/E-pig \
      --epig-ref <full-40-char-sha>

    python scripts/prepare_after_sales_demo.py cleanup \
      --manifest /tmp/dtc-after-sales-demo-.../manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.after-sales-demo.yml"

SCHEMA_VERSION = 1
KIND = "dtc-after-sales-demo"
NONCE_RE = re.compile(r"^[0-9a-f]{16}$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
PROJECT_RE = re.compile(r"^dtc-after-sales-demo-[0-9a-f]{16}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")

MARKER_LABEL = "com.dtc.after-sales.demo"
RUN_LABEL = "com.dtc.after-sales.demo.run"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

# Committed metadata directories may contain relative symlinks. Anything else
# is refused so a context cannot smuggle a link outside the extracted tree.
ALLOWED_SYMLINK_PREFIXES = (".agents/", ".claude/", ".cursor/", ".dtc/")

EPIG_BASE_FROM = "FROM node:24-alpine AS base"
EPIG_NODE_ARG = "ARG NODE_IMAGE=node:24-alpine"
EPIG_SYNTAX_LINE = "# syntax=docker/dockerfile:1"
EPIG_DOCKERFILE_NAME = "Dockerfile.after-sales-demo"

# The committed E-pig Dockerfile installs only root dependencies: the deps
# stage omits pnpm-workspace.yaml and the per-workspace node_modules are never
# copied into the build stage. The controlled copy keeps package.json and the
# lock file byte-for-byte unchanged but makes the existing workspace install
# complete, so the normal production build can resolve happy-dog dependencies.
EPIG_DEPS_ORIGINAL = """\
FROM base AS deps
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile"""
EPIG_DEPS_OVERLAY = """\
FROM base AS deps
COPY package.json pnpm-lock.yaml ./
COPY pnpm-workspace.yaml ./
COPY packages ./packages
RUN pnpm install --frozen-lockfile"""
EPIG_BUILD_ORIGINAL = """\
FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY . ."""
EPIG_BUILD_OVERLAY = """\
FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/packages ./packages
COPY . ."""

DEFAULT_DTC_NODE_IMAGE = "node:24-alpine"
DEFAULT_DTC_PYTHON_IMAGE = "python:3.11-slim"
DEFAULT_EPIG_NODE_IMAGE = "node:24-alpine"

# Every docker call is bounded; on timeout cleanup keeps the manifest and env
# so an operator can inspect resources and retry safely.
DOCKER_TIMEOUT_SECONDS = 30.0

DockerRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class DemoPreparationError(RuntimeError):
    """A safe, user-facing preparation refusal."""


class CleanupRefused(DemoPreparationError):
    """Cleanup refused because ownership could not be proven."""


@dataclass(frozen=True)
class SourceContext:
    repo: str
    ref: str
    commit: str
    archive_sha256: str
    worktree_dirty: bool
    worktree_excluded: bool = True
    skipped_metadata_symlinks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prepared:
    root: Path
    env_file: Path
    manifest: Path
    compose_file: Path
    project: str
    nonce: str
    epig_port: int
    dtc_port: int


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise DemoPreparationError(f"git command failed for this repository: {' '.join(args)}")
    return result.stdout


def resolve_commit(repo: Path, ref: str) -> str:
    """Resolve an explicit full commit SHA; branches/tags/short refs are refused."""
    if not COMMIT_RE.fullmatch(ref):
        raise DemoPreparationError("each source ref must be a full 40-character commit SHA")
    if not (repo / ".git").exists():
        raise DemoPreparationError("source repository does not contain .git")
    output = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    if not COMMIT_RE.fullmatch(output):
        raise DemoPreparationError("source ref did not resolve to a commit")
    return output.lower()


def _worktree_dirty(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain", "--untracked-files=normal").strip())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _member_path(name: str) -> PurePosixPath:
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts or not member.parts:
        raise DemoPreparationError("archive contains an unsafe path")
    return member


def safe_extract(data: bytes, destination: Path) -> tuple[str, ...]:
    """Extract a trusted git archive without following hostile link types.

    Relative symlinks inside known metadata directories are preserved only when
    they stay inside the context. The E-pig repository intentionally commits a
    few metadata links to an external ``DTC-skills`` checkout; those are skipped
    rather than dereferenced. Metadata directories are not part of either build
    context, so skipping cannot alter the image.
    """
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    root = destination.resolve()
    skipped: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            relative = _member_path(member.name)
            target = destination.joinpath(*relative.parts)
            resolved = target.resolve(strict=False)
            if not _is_within(resolved, root):
                raise DemoPreparationError("archive member escapes the context")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym():
                if not relative.as_posix().startswith(ALLOWED_SYMLINK_PREFIXES):
                    raise DemoPreparationError("archive contains an unexpected symlink")
                link = PurePosixPath(member.linkname)
                if link.is_absolute():
                    raise DemoPreparationError("archive symlink target must be relative")
                link_target = (target.parent / Path(*link.parts)).resolve(strict=False)
                if not _is_within(link_target, root):
                    skipped.append(relative.as_posix())
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    raise DemoPreparationError("archive contains duplicate entries")
                target.symlink_to(member.linkname)
                continue
            if not member.isreg():
                raise DemoPreparationError("archive contains an unsupported entry type")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise DemoPreparationError("archive file could not be read")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise DemoPreparationError("archive contains duplicate entries")
            with open(target, "wb") as handle:
                shutil.copyfileobj(extracted, handle)
            os.chmod(target, member.mode & 0o777)
    return tuple(skipped)


def assert_no_live_env(root: Path) -> None:
    """Allow only the committed ``.env.example`` placeholder."""
    for path in root.rglob("*"):
        name = path.name
        if name == ".env.example":
            continue
        if name == ".env" or name.startswith(".env."):
            raise DemoPreparationError(
                "source context contains a live environment file; commit refs must not carry secrets"
            )


def prepare_context(repo: Path, ref: str, destination: Path) -> SourceContext:
    """Extract one immutable commit into a fresh private destination."""
    try:
        top_level = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip())
    except DemoPreparationError as exc:
        raise DemoPreparationError("source path is not a git worktree") from exc
    commit = resolve_commit(top_level, ref)
    dirty = _worktree_dirty(top_level)
    archive = _git(top_level, "archive", "--format=tar", commit)
    digest = hashlib.sha256(archive).hexdigest()
    skipped = safe_extract(archive, destination)
    assert_no_live_env(destination)
    return SourceContext(
        repo=str(top_level),
        ref=ref.lower(),
        commit=commit,
        archive_sha256=digest,
        worktree_dirty=dirty,
        skipped_metadata_symlinks=skipped,
    )


def write_epig_dockerfile_overlay(context: Path) -> dict[str, str]:
    """Copy E-pig's Dockerfile with only the official base-image indirection.

    The exact ``node:24-alpine`` entrypoint remains the default; an operator may
    point ``--epig-node-image`` at the same official image in a known mirror.
    Dependencies, lock files and every other instruction are untouched.
    """
    source = context / "Dockerfile"
    if not source.is_file():
        raise DemoPreparationError("E-pig context has no Dockerfile")
    text = source.read_text(encoding="utf-8")
    if text.count(EPIG_SYNTAX_LINE) != 1:
        raise DemoPreparationError("expected exactly one explicit dockerfile syntax line")
    if text.count(EPIG_BASE_FROM) != 1:
        raise DemoPreparationError("expected exactly one base image FROM line")
    if "NODE_IMAGE" in text:
        raise DemoPreparationError("E-pig Dockerfile already defines NODE_IMAGE")

    without_syntax = "\n".join(
        line for line in text.splitlines() if line.strip() != EPIG_SYNTAX_LINE
    )
    if without_syntax.count(EPIG_DEPS_ORIGINAL) != 1:
        raise DemoPreparationError("expected exactly one E-pig dependency install block")
    if without_syntax.count(EPIG_BUILD_ORIGINAL) != 1:
        raise DemoPreparationError("expected exactly one E-pig build stage block")
    overlaid = without_syntax.replace(
        EPIG_BASE_FROM, f"{EPIG_NODE_ARG}\nFROM ${{NODE_IMAGE}} AS base", 1
    )
    overlaid = overlaid.replace(EPIG_DEPS_ORIGINAL, EPIG_DEPS_OVERLAY, 1)
    overlaid = overlaid.replace(EPIG_BUILD_ORIGINAL, EPIG_BUILD_OVERLAY, 1)
    if overlaid == without_syntax or overlaid.count(EPIG_NODE_ARG) != 1:
        raise DemoPreparationError("E-pig Dockerfile overlay was not applied exactly once")
    if overlaid.count(EPIG_DEPS_OVERLAY) != 1 or overlaid.count(EPIG_BUILD_OVERLAY) != 1:
        raise DemoPreparationError("E-pig workspace install overlay was not applied exactly once")
    destination = context / EPIG_DOCKERFILE_NAME
    destination.write_text(overlaid + "\n", encoding="utf-8")
    os.chmod(destination, 0o644)
    return {
        "name": EPIG_DOCKERFILE_NAME,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }


def _validate_image(value: str, label: str) -> str:
    if not IMAGE_RE.fullmatch(value):
        raise DemoPreparationError(f"{label} is not a valid explicit image reference")
    return value


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _two_free_ports() -> tuple[int, int]:
    first = _free_port()
    second = _free_port()
    while second == first:
        second = _free_port()
    return first, second


def _quote_env(value: str) -> str:
    if "\n" in value or "\r" in value or "'" in value:
        raise DemoPreparationError("generated environment values contain an unsupported character")
    return f"'{value}'"


def _render_env(values: Mapping[str, str]) -> str:
    return "".join(f"{key}={_quote_env(value)}\n" for key, value in values.items())


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.chmod(path, 0o600)


def _assert_private_temp_root(root: Path, repos: Sequence[Path]) -> None:
    resolved = root.resolve()
    for repo in repos:
        repo_resolved = repo.resolve()
        if _is_within(resolved, repo_resolved):
            raise DemoPreparationError(
                "temporary output must stay outside both source repositories"
            )


def prepare_demo(
    *,
    dtc_repo: Path,
    dtc_ref: str,
    epig_repo: Path,
    epig_ref: str,
    dtc_node_image: str = DEFAULT_DTC_NODE_IMAGE,
    dtc_python_image: str = DEFAULT_DTC_PYTHON_IMAGE,
    epig_node_image: str = DEFAULT_EPIG_NODE_IMAGE,
) -> Prepared:
    """Create both contexts, the protected env file and the ownership manifest."""
    dtc_node_image = _validate_image(dtc_node_image, "--dtc-node-image")
    dtc_python_image = _validate_image(dtc_python_image, "--dtc-python-image")
    epig_node_image = _validate_image(epig_node_image, "--epig-node-image")

    temp_root = Path(tempfile.gettempdir())
    _assert_private_temp_root(temp_root, (dtc_repo, epig_repo))

    nonce = secrets.token_hex(8)
    project = f"{KIND}-{nonce}"
    root = Path(tempfile.mkdtemp(prefix=f"{project}-", dir=str(temp_root)))
    try:
        dtc_context = root / "dtc"
        epig_context = root / "epig"
        dtc_source = prepare_context(dtc_repo, dtc_ref, dtc_context)
        epig_source = prepare_context(epig_repo, epig_ref, epig_context)
        overlay = write_epig_dockerfile_overlay(epig_context)

        epig_port, dtc_port = _two_free_ports()
        service_token = secrets.token_urlsafe(32)
        cart_cookie_secret = secrets.token_urlsafe(32)
        dtc_password = secrets.token_urlsafe(24)
        epig_password = secrets.token_urlsafe(24)

        dtc_image = f"{project}-dtc:local"
        epig_image = f"{project}-epig:local"
        epig_migrate_image = f"{project}-epig-migrate:local"

        env_values = {
            "COMPOSE_PROJECT_NAME": project,
            "DEMO_RUN_NONCE": nonce,
            "DTC_CONTEXT": str(dtc_context),
            "DTC_IMAGE": dtc_image,
            "DTC_REVISION": dtc_source.commit,
            "DTC_NODE_IMAGE": dtc_node_image,
            "DTC_PYTHON_IMAGE": dtc_python_image,
            "DTC_HOST_PORT": str(dtc_port),
            "DTC_POSTGRES_USER": "dtc_demo",
            "DTC_POSTGRES_PASSWORD": dtc_password,
            "DTC_POSTGRES_DB": "dtc_after_sales",
            "EPIG_CONTEXT": str(epig_context),
            "EPIG_DOCKERFILE": EPIG_DOCKERFILE_NAME,
            "EPIG_IMAGE": epig_image,
            "EPIG_MIGRATE_IMAGE": epig_migrate_image,
            "EPIG_NODE_IMAGE": epig_node_image,
            "EPIG_HOST_PORT": str(epig_port),
            "EPIG_POSTGRES_USER": "epig_demo",
            "EPIG_POSTGRES_PASSWORD": epig_password,
            "EPIG_POSTGRES_DB": "epig_after_sales",
            "AFTER_SALES_SERVICE_TOKEN": service_token,
            "CART_COOKIE_SECRET": cart_cookie_secret,
        }

        env_file = root / ".env"
        _write_private(env_file, _render_env(env_values))

        # Freeze the exact Compose file used for this run. Cleanup validates
        # this copy's digest and never consults the mutable repository file.
        if not COMPOSE_FILE.is_file():
            raise DemoPreparationError("docker-compose.after-sales-demo.yml is missing")
        frozen_compose = root / COMPOSE_FILE.name
        compose_bytes = COMPOSE_FILE.read_bytes()
        frozen_compose.write_bytes(compose_bytes)
        os.chmod(frozen_compose, 0o644)
        compose_sha256 = hashlib.sha256(compose_bytes).hexdigest()

        manifest_path = root / "manifest.json"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "created_at": datetime.now(UTC).isoformat(),
            "project": project,
            "nonce": nonce,
            "root_dir": str(root),
            "env_file": str(env_file),
            "manifest_file": str(manifest_path),
            "compose_file": str(frozen_compose),
            "compose_sha256": compose_sha256,
            "ports": {"epig": epig_port, "dtc_api": dtc_port},
            "images": {
                "dtc": dtc_image,
                "epig": epig_image,
                "epig_migrate": epig_migrate_image,
            },
            "build_args": {
                "dtc_node_image": dtc_node_image,
                "dtc_python_image": dtc_python_image,
                "epig_node_image": epig_node_image,
            },
            "sources": {
                "dtc": {
                    "repo": dtc_source.repo,
                    "ref": dtc_source.ref,
                    "commit": dtc_source.commit,
                    "archive_sha256": dtc_source.archive_sha256,
                    "worktree_dirty": dtc_source.worktree_dirty,
                    "worktree_excluded": True,
                    "skipped_metadata_symlinks": list(dtc_source.skipped_metadata_symlinks),
                },
                "epig": {
                    "repo": epig_source.repo,
                    "ref": epig_source.ref,
                    "commit": epig_source.commit,
                    "archive_sha256": epig_source.archive_sha256,
                    "worktree_dirty": epig_source.worktree_dirty,
                    "worktree_excluded": True,
                    "skipped_metadata_symlinks": list(epig_source.skipped_metadata_symlinks),
                    "dockerfile_overlay": overlay,
                },
            },
            "secret_env_keys": [
                "AFTER_SALES_SERVICE_TOKEN",
                "CART_COOKIE_SECRET",
                "DTC_POSTGRES_PASSWORD",
                "EPIG_POSTGRES_PASSWORD",
            ],
        }
        _write_private(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise

    return Prepared(
        root=root,
        env_file=env_file,
        manifest=manifest_path,
        compose_file=frozen_compose,
        project=project,
        nonce=nonce,
        epig_port=epig_port,
        dtc_port=dtc_port,
    )


def _ensure_private_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise CleanupRefused(f"{label} is missing")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise CleanupRefused(f"{label} must not be group/world accessible")


def _load_manifest(path: Path) -> tuple[dict[str, Any], Path, Path, Path]:
    manifest_path = path.resolve()
    _ensure_private_file(manifest_path, "manifest")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupRefused("manifest is not readable JSON") from exc
    if not isinstance(data, dict):
        raise CleanupRefused("manifest must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION or data.get("kind") != KIND:
        raise CleanupRefused("manifest does not belong to this helper")

    nonce = data.get("nonce")
    project = data.get("project")
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise CleanupRefused("manifest nonce is invalid")
    if (
        not isinstance(project, str)
        or project != f"{KIND}-{nonce}"
        or not PROJECT_RE.fullmatch(project)
    ):
        raise CleanupRefused("manifest project does not match its nonce")

    root = Path(str(data.get("root_dir", ""))).resolve()
    if root.name == "" or not root.name.startswith(f"{project}-"):
        raise CleanupRefused("manifest root does not match the project prefix")
    temp_root = Path(tempfile.gettempdir()).resolve()
    if root.parent != temp_root:
        raise CleanupRefused("manifest root is outside the private temporary directory")
    if manifest_path.parent != root:
        raise CleanupRefused("manifest is outside its prepared root")

    env_file = Path(str(data.get("env_file", ""))).resolve()
    if env_file.name != ".env" or env_file.parent != root:
        raise CleanupRefused("env file is outside its prepared root")
    _ensure_private_file(env_file, "env file")

    compose_file = Path(str(data.get("compose_file", ""))).resolve()
    if compose_file.name != COMPOSE_FILE.name or compose_file.parent != root:
        raise CleanupRefused("compose file is outside its prepared root")
    if not compose_file.is_file():
        raise CleanupRefused("frozen compose file is missing")
    expected_sha = data.get("compose_sha256")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise CleanupRefused("manifest has no valid frozen compose digest")
    if hashlib.sha256(compose_file.read_bytes()).hexdigest() != expected_sha:
        raise CleanupRefused("frozen compose file was modified; refusing cleanup")
    return data, root, env_file, compose_file


def _run_docker(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["docker", *argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=DOCKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CleanupRefused(
            "docker command timed out; manifest and env files were kept for retry"
        ) from exc
    except OSError as exc:
        raise CleanupRefused(
            "docker command could not run; manifest and env files were kept for retry"
        ) from exc
    if result.returncode != 0:
        raise CleanupRefused("docker command failed during ownership verification")
    return result


def _list_ids(kind: str, project: str, runner: DockerRunner) -> list[str]:
    if kind == "container":
        argv = ["ps", "-aq", "--filter", f"label={COMPOSE_PROJECT_LABEL}={project}"]
    elif kind == "volume":
        argv = ["volume", "ls", "-q", "--filter", f"label={COMPOSE_PROJECT_LABEL}={project}"]
    else:
        argv = ["network", "ls", "-q", "--filter", f"label={COMPOSE_PROJECT_LABEL}={project}"]
    return [line.strip() for line in runner(argv).stdout.splitlines() if line.strip()]


def _inspect_labels(kind: str, ids: Sequence[str], runner: DockerRunner) -> list[dict[str, str]]:
    prefix = [] if kind == "container" else [kind]
    argv = [
        *prefix,
        "inspect",
        "--format",
        "{{json .Config.Labels}}" if kind == "container" else "{{json .Labels}}",
        *ids,
    ]
    output = runner(argv).stdout
    labels: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CleanupRefused("resource labels are not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise CleanupRefused("resource has no ownership labels")
        labels.append({str(key): str(value) for key, value in decoded.items()})
    if len(labels) != len(ids):
        raise CleanupRefused("could not inspect every resource owned by the project")
    return labels


def _verify_owned_resources(project: str, nonce: str, runner: DockerRunner) -> None:
    for kind in ("container", "volume", "network"):
        ids = _list_ids(kind, project, runner)
        if not ids:
            continue
        for labels in _inspect_labels(kind, ids, runner):
            if labels.get(MARKER_LABEL) != "1" or labels.get(RUN_LABEL) != nonce:
                raise CleanupRefused(
                    "project contains resources without this run's ownership labels"
                )


def _assert_project_resources_absent(project: str, runner: DockerRunner) -> None:
    remaining = {
        kind: _list_ids(kind, project, runner) for kind in ("container", "volume", "network")
    }
    if any(remaining.values()):
        raise CleanupRefused(
            "project resources remain after down; manifest and env files were kept for retry"
        )


def cleanup_demo(manifest_path: Path, *, docker: DockerRunner | None = None) -> None:
    """Project-scoped cleanup that refuses anything not proven to belong here."""
    runner = docker or _run_docker
    data, root, env_file, compose_file = _load_manifest(manifest_path)
    nonce = str(data["nonce"])
    project = str(data["project"])
    _verify_owned_resources(project, nonce, runner)

    if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith(
        f"{project}-"
    ):
        raise CleanupRefused("cleanup target changed after validation")

    compose_down = [
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(env_file),
        "-f",
        str(compose_file),
        "down",
        "--volumes",
    ]
    runner(compose_down)
    # Only remove the manifest/env/root once every project resource is gone.
    _assert_project_resources_absent(project, runner)
    shutil.rmtree(root)


def _print_prepared(prepared: Prepared) -> None:
    compose = (
        f"docker compose --project-name {prepared.project} "
        f"--env-file {prepared.env_file} -f {prepared.compose_file}"
    )
    print("Prepared synthetic after-sales demo sources.")
    print(f"  root: {prepared.root}")
    print(f"  env file (0600): {prepared.env_file}")
    print(f"  manifest (0600): {prepared.manifest}")
    print(f"  frozen compose: {prepared.compose_file}")
    print(f"  E-pig host port: {prepared.epig_port}")
    print(f"  DTC API host port: {prepared.dtc_port}")
    print("Next:")
    print(f"  {compose} up -d --build")
    print("Cleanup:")
    print(f"  {sys.executable} {Path(__file__).resolve()} cleanup --manifest {prepared.manifest}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prepare_after_sales_demo.py",
        description=(
            "Prepare immutable two-repository demo contexts and a protected env file, "
            "or clean up only this run's labelled resources."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    prepare = subcommands.add_parser(
        "prepare",
        help="archive explicit commit SHAs and write a 0600 env file plus manifest",
    )
    prepare.add_argument("--dtc-repo", type=Path, default=ROOT)
    prepare.add_argument("--dtc-ref", required=True)
    prepare.add_argument("--epig-repo", type=Path, required=True)
    prepare.add_argument("--epig-ref", required=True)
    prepare.add_argument("--dtc-node-image", default=DEFAULT_DTC_NODE_IMAGE)
    prepare.add_argument("--dtc-python-image", default=DEFAULT_DTC_PYTHON_IMAGE)
    prepare.add_argument("--epig-node-image", default=DEFAULT_EPIG_NODE_IMAGE)

    cleanup = subcommands.add_parser(
        "cleanup",
        help="stop only this manifest's project after verifying its ownership labels",
    )
    cleanup.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepared = prepare_demo(
                dtc_repo=args.dtc_repo.resolve(),
                dtc_ref=args.dtc_ref,
                epig_repo=args.epig_repo.resolve(),
                epig_ref=args.epig_ref,
                dtc_node_image=args.dtc_node_image,
                dtc_python_image=args.dtc_python_image,
                epig_node_image=args.epig_node_image,
            )
            _print_prepared(prepared)
        else:
            cleanup_demo(args.manifest)
            print("Removed only this manifest's labelled demo project.")
    except DemoPreparationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
