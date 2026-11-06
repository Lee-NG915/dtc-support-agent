"""Sealed after-sales holdout acceptance runner.

This module executes the frozen 12-case holdout described in
``docs/architecture/modules/after-sales/dtc-workflow.md`` ("保留集执行协议").
It is deliberately narrow: one fixed handler per dataset case, no generic
workflow/DSL, no business database target and no changes to production code.

Default (offline) behaviour executes the four engineering guards and marks the
model cases ``not_run``. Real model calls require an explicit
``--allow-real-model`` plus provider/model/call/token bounds. Credentials are
read only from the dedicated, 0600, Git-ignored ``.env.eval.local`` file, and
only ``DTC_EVAL_*`` keys are retained. Values are never printed, logged or
written to the report.

The report records machine-observable evidence only. Semantic 0/1/2 grading on
factual correctness, evidence sufficiency and actionability remains a manual
Codex decision. No combined LLM score is produced.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.connectors.errors import ConnectorUnavailable
from app.core.exceptions import LLMProviderError
from app.core.ids import new_trace_id
from app.core.security import generate_staff_token
from app.core.time import utcnow
from app.main import create_app
from app.prompts.answer import ANSWER_SYSTEM
from app.prompts.complexity import COMPLEXITY_SYSTEM
from app.prompts.faithfulness import FAITHFULNESS_SYSTEM, SELF_CORRECT_SYSTEM
from app.prompts.intent import INTENT_SYSTEM
from app.prompts.planner import PLANNER_SYSTEM
from app.prompts.query_rewrite import REWRITE_SYSTEM
from app.prompts.self_rag import GRADE_SYSTEM
from app.providers.base import LLMProvider
from app.providers.deepseek import DeepSeekProvider
from app.repositories.db import init_schema
from app.repositories.models import (
    AfterSalesApprovalRow,
    AfterSalesCaseRow,
    AfterSalesDraftRow,
    AfterSalesOperationRow,
    AfterSalesSyncCursorRow,
)
from app.repositories.staff_repository import StaffRepository
from app.schemas.after_sales import AfterSalesMarket, StaffActor, StaffRole
from app.schemas.chat import ChatRequest
from app.services.chat_service import ChatService
from app.services.container import AppContainer, build_container
from eval.runner import (
    ModelRun,
    RealModelConfig,
    _BoundedLLM,
    _knowledge_digest,
    _prompt_versions,
    _redact,
)
from eval.tool_trace import ToolTrace

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "eval/dataset/after_sales_holdout_v1.jsonl"
MANIFEST_PATH = ROOT / "eval/dataset/after_sales_holdout_v1.manifest.json"
REPORT_DIR = ROOT / "eval/reports"
KNOWLEDGE_DIR = ROOT / "knowledge"
DEDICATED_ENV_PATH = ROOT / ".env.eval.local"

OFFICIAL_BASE_URL = "https://api.deepseek.com"
MAX_DATASET_CASES = 12
MAX_MODEL_CALLS = 64
MAX_OUTPUT_TOKENS = 1200
FREEZE_REVISION = 3
EXPECTED_CASE_IDS = [f"H{index:02d}" for index in range(1, 13)]
EXPECTED_PARTITIONS = {"policy_graph": 6, "staff_suggestion": 2, "engineering_guard": 4}
MODEL_PARTITIONS = frozenset({"policy_graph", "staff_suggestion"})
DEMO_WRITE_TOOLS = frozenset({"create_refund_request", "create_return_request", "cancel_order"})

BOUNDARY_POLICY = "in_process_chat_graph"
BOUNDARY_ASGI = "asgi_in_process_http"
BOUNDARY_H11 = "asgi_in_process_http+in_process_execution_service"
BOUNDARY_H12 = "tcp_os_restart"

_ALLOWED_EVAL_KEYS = frozenset({"DTC_EVAL_DEEPSEEK_API_KEY", "DTC_EVAL_DEEPSEEK_BASE_URL"})
_SENSITIVE_ENV_KEYS = (
    "DTC_EVAL_DEEPSEEK_API_KEY",
    "DTC_EVAL_DEEPSEEK_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DATABASE_URL",
    "EPIG_SERVICE_TOKEN",
    "ADMIN_API_TOKEN",
)
_STAGE_PROMPTS = (
    (INTENT_SYSTEM, "intent"),
    (COMPLEXITY_SYSTEM, "complexity"),
    (REWRITE_SYSTEM, "query_rewrite"),
    (GRADE_SYSTEM, "self_rag_grade"),
    (PLANNER_SYSTEM, "planner"),
    (ANSWER_SYSTEM, "answer_generation"),
    (FAITHFULNESS_SYSTEM, "faithfulness"),
    (SELF_CORRECT_SYSTEM, "self_correct"),
)
StaffSpec = tuple[str, tuple[StaffRole, ...], tuple[AfterSalesMarket, ...]]
_STAFF_SUPPORT: dict[str, StaffSpec] = {
    "au": ("staff_support_au", ("support",), ("au",)),
    "sg": ("staff_support_sg", ("support",), ("sg",)),
    "us": ("staff_support_us", ("support",), ("us",)),
}
_STAFF_APPROVER: dict[str, StaffSpec] = {
    "au": ("staff_approver_au", ("approver",), ("au",)),
    "sg": ("staff_approver_sg", ("approver",), ("sg",)),
    "us": ("staff_approver_us", ("approver",), ("us",)),
}
_SYNTHETIC_SKU = "SYNTH-MUG-001"
_FIXTURE_DELIVERED_AT = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
_FIXTURE_PHOTO_SOURCE = "Synthetic acceptance photo record (not a real image)"
_FIXTURE_ORDER_SOURCE = "Synthetic acceptance order/delivery record"
_DRAFT_SUMMARY = "Synthetic acceptance draft for the frozen holdout fixture."
_DRAFT_REPLY = "We will review the synthetic request and share the next verified step."
_MANUAL_AXES = ["factual_correctness", "evidence_sufficiency", "actionability"]


@dataclass(frozen=True)
class _SyntheticMaterial:
    material_type: str
    source: str
    content: str
    applies_to_sku: str | None = None
    delivered_at: datetime | None = None


@dataclass(frozen=True)
class _FixtureIdentity:
    market: str
    sku: str
    name: str
    category: str
    request_type: str = "damaged"
    category_known: bool = True
    materials_required: bool = True
    description_from_query: bool = True
    reset_attachments: bool = True


@dataclass(frozen=True)
class _SyntheticFixture:
    market: str
    sku: str
    name: str
    category: str
    request_type: str
    description: str | None
    materials: tuple[_SyntheticMaterial, ...]
    category_known: bool
    description_from_query: bool
    reset_attachments: bool


@dataclass(frozen=True)
class _FixtureValidation:
    ok: bool
    errors: tuple[str, ...]
    facts: dict[str, Any]
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "facts": self.facts,
            "sha256": self.sha256,
        }


# The order/request fixture is fixed per row. H09 deliberately uses an unknown
# SKU/category. H12 keeps the FakeConnector default request (AU homewares
# damaged) because its API children cannot receive an in-process fixture
# mutation; the row query is the restart scenario, not request text.
_FIXTURE_IDENTITIES: dict[str, _FixtureIdentity] = {
    "H07": _FixtureIdentity(
        market="au",
        sku="SYNTH-CUP-007",
        name="Synthetic ceramic cup",
        category="homewares",
    ),
    "H08": _FixtureIdentity(
        market="sg",
        sku="SYNTH-MATTRESS-008",
        name="Synthetic mattress",
        category="mattress",
    ),
    "H09": _FixtureIdentity(
        market="au",
        sku="SYNTH-UNSUPPORTED-009",
        name="Synthetic unsupported item",
        category="unsupported",
        category_known=False,
        materials_required=False,
    ),
    "H10": _FixtureIdentity(
        market="us",
        sku="SYNTH-CUP-010",
        name="Synthetic ceramic cup",
        category="homewares",
        materials_required=False,
    ),
    "H11": _FixtureIdentity(
        market="au",
        sku="SYNTH-CUP-011",
        name="Synthetic ceramic cup",
        category="homewares",
    ),
    "H12": _FixtureIdentity(
        market="au",
        sku=_SYNTHETIC_SKU,
        name="Synthetic ceramic mug",
        category="homewares",
        description_from_query=False,
        reset_attachments=False,
    ),
}


class AcceptanceError(RuntimeError):
    """Safe operational failure for the acceptance runner."""


class FreezeVerificationError(AcceptanceError):
    """The dataset or one of its frozen source files changed."""


class ConfigurationError(AcceptanceError):
    """Invalid CLI/credential configuration; raised before any paid call."""


class _GuardSetupError(RuntimeError):
    """A safe engineering-guard setup failure (stage plus status/detail code)."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(stage)
        self.stage = stage
        self.detail = detail


class _ModelLaneStop(Exception):
    """Stop the model lane without retrying after a budget/provider failure."""

    def __init__(self, result: dict[str, Any], reason: str) -> None:
        super().__init__(reason)
        self.result = result
        self.reason = reason


class _GuardProbeLLM:
    """Counting probe used only to prove that a guard never calls a model."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise LLMProviderError("guard probe invoked")

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        self.calls += 1
        raise LLMProviderError("guard probe invoked")

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class FrozenArtifacts:
    manifest: dict[str, Any] = field(repr=False)
    rows: list[dict[str, Any]] = field(repr=False)
    dataset_sha256: str
    manifest_sha256: str
    freeze_revision: int
    freeze_count: int
    freeze_digest: str
    app_freeze_digest: str
    knowledge_sha256: str

    def verification(self) -> dict[str, Any]:
        return {
            "status": "verified",
            "freeze_revision": self.freeze_revision,
            "freeze_count": self.freeze_count,
            "dataset_sha256": self.dataset_sha256,
            "manifest_sha256": self.manifest_sha256,
            "freeze_digest": self.freeze_digest,
            "app_freeze_digest": self.app_freeze_digest,
        }

    def data_meta(self) -> dict[str, Any]:
        return {
            "source": DATASET_PATH.name,
            "n": len(self.rows),
            "sha256": self.dataset_sha256,
            "role": "sealed_human_holdout",
            "fixture_boundary": "synthetic_only",
        }

    def provenance(self) -> dict[str, Any]:
        return {
            "source_candidate": {
                "baseline_commit": self.manifest.get("baseline_commit"),
                "freeze_revision": self.freeze_revision,
                "freeze_digest": self.freeze_digest,
                "app_freeze_digest": self.app_freeze_digest,
                "knowledge_sha256": self.knowledge_sha256,
                "prompt_versions": _prompt_versions(),
                "protocol_refreeze": self.manifest.get("protocol_refreeze"),
            },
            "note": (
                "Source candidate is derived from the frozen manifest and local content "
                "digests; no git mutation or workspace write is performed."
            ),
        }


@dataclass(frozen=True)
class DedicatedEvalEnv:
    api_key: str = field(repr=False)
    base_url: str
    file_mode: str

    def public(self) -> dict[str, Any]:
        return {
            "credential_source": "dedicated .env.eval.local (0600), DTC_EVAL_* keys only",
            "file_mode": self.file_mode,
            "base_url": self.base_url,
            "api_key_present": bool(self.api_key),
        }


@dataclass(frozen=True)
class RunOutcome:
    report: dict[str, Any]
    report_path: Path
    exit_code: int


@dataclass
class _Batch:
    results: list[dict[str, Any]]
    stop_reason: str | None = None


# ---------------------------------------------------------------- freeze check


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_freeze_path(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise FreezeVerificationError("freeze manifest contains an unsafe path")
    if path.parts[0] not in {"app", "knowledge"}:
        raise FreezeVerificationError("freeze manifest path is outside app/knowledge")
    resolved = (ROOT / path).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        raise FreezeVerificationError("freeze manifest path escapes the repository") from None
    if (ROOT / path).is_symlink():
        raise FreezeVerificationError("freeze manifest may not reference symlinks")
    return ROOT / path


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise FreezeVerificationError("dataset row is not an object")
                rows.append(parsed)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise FreezeVerificationError("dataset is unreadable") from None
    return rows


def verify_frozen(
    *,
    dataset_path: Path = DATASET_PATH,
    manifest_path: Path = MANIFEST_PATH,
) -> FrozenArtifacts:
    """Verify dataset SHA-256 and every frozen app/knowledge hash.

    This runs before any provider is constructed. Any mismatch, missing file,
    unsafe path or unexpected case shape fails closed.
    """
    try:
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise FreezeVerificationError("frozen manifest is unreadable") from None
    if not isinstance(manifest, dict):
        raise FreezeVerificationError("frozen manifest is not an object")

    try:
        dataset_raw = dataset_path.read_bytes()
    except OSError:
        raise FreezeVerificationError("frozen dataset is unreadable") from None
    dataset_sha256 = _sha256_bytes(dataset_raw)
    if manifest.get("dataset_sha256") != dataset_sha256:
        raise FreezeVerificationError("dataset SHA-256 does not match the frozen manifest")
    if manifest.get("freeze_revision") != FREEZE_REVISION:
        raise FreezeVerificationError("freeze revision is not the approved revision")
    if manifest.get("count") != MAX_DATASET_CASES:
        raise FreezeVerificationError("frozen manifest case count is not 12")

    rows = _load_dataset(dataset_path)
    if len(rows) != MAX_DATASET_CASES:
        raise FreezeVerificationError("dataset row count is not 12")
    ids = [row.get("id") for row in rows]
    if ids != EXPECTED_CASE_IDS:
        raise FreezeVerificationError("dataset case IDs/order do not match the frozen holdout")
    partitions = Counter(str(row.get("partition")) for row in rows)
    if dict(partitions) != EXPECTED_PARTITIONS:
        raise FreezeVerificationError("dataset partition counts do not match the frozen holdout")
    for row in rows:
        required = {
            "id",
            "partition",
            "brand",
            "market",
            "product_category",
            "query",
            "expected_documents",
            "acceptance_assertions",
            "forbidden",
            "as_of",
            "fixture_boundary",
        }
        if not required <= set(row):
            raise FreezeVerificationError("dataset row is missing required fields")
        if row.get("brand") != "epig" or row.get("market") not in {"au", "sg", "us"}:
            raise FreezeVerificationError("dataset row scope is not an E-pig market")
        if row.get("fixture_boundary") != "synthetic_only":
            raise FreezeVerificationError("dataset row is not synthetic-only")

    freeze_hashes = manifest.get("freeze_hashes")
    if not isinstance(freeze_hashes, dict) or len(freeze_hashes) != 203:
        raise FreezeVerificationError("frozen source list is not the approved 203-file set")
    for relative, expected_hash in sorted(freeze_hashes.items()):
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise FreezeVerificationError("frozen source entry is malformed")
        path = _validate_freeze_path(relative)
        try:
            actual = _sha256_bytes(path.read_bytes())
        except OSError:
            raise FreezeVerificationError(f"frozen source is missing: {relative}") from None
        if actual != expected_hash:
            raise FreezeVerificationError(f"frozen source changed: {relative}")

    pairs = sorted(freeze_hashes.items())
    app_pairs = [(path, digest) for path, digest in pairs if path.startswith("app/")]
    return FrozenArtifacts(
        manifest=manifest,
        rows=rows,
        dataset_sha256=dataset_sha256,
        manifest_sha256=_sha256_bytes(manifest_raw),
        freeze_revision=FREEZE_REVISION,
        freeze_count=len(pairs),
        freeze_digest=_sha256_bytes(_canonical_json(pairs).encode("utf-8")),
        app_freeze_digest=_sha256_bytes(_canonical_json(app_pairs).encode("utf-8")),
        knowledge_sha256=_knowledge_digest(),
    )


# ----------------------------------------------------------- credentials/config


def load_dedicated_eval_env(path: Path = DEDICATED_ENV_PATH) -> DedicatedEvalEnv:
    """Read only DTC_EVAL_* keys from the exact dedicated 0600 file.

    The parser never mutates ``os.environ``, never reads another ``.env`` and
    never returns/logs a non-DTC_EVAL key. Error messages contain no line
    content or credential value.
    """
    candidate = Path(path)
    if candidate.resolve() != DEDICATED_ENV_PATH.resolve():
        raise ConfigurationError("only the dedicated .env.eval.local file may be read")
    try:
        info = candidate.stat()
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ConfigurationError("dedicated eval env file is missing or unreadable") from None
    if not stat.S_ISREG(info.st_mode):
        raise ConfigurationError("dedicated eval env path is not a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o600:
        raise ConfigurationError("dedicated eval env file must have mode 0600")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ConfigurationError(f"invalid eval env line {line_number}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in _ALLOWED_EVAL_KEYS:
            values[key] = value

    api_key = values.get("DTC_EVAL_DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ConfigurationError("DTC_EVAL_DEEPSEEK_API_KEY is required in real mode")
    base_url = values.get("DTC_EVAL_DEEPSEEK_BASE_URL", "").strip() or OFFICIAL_BASE_URL
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.deepseek.com"
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ConfigurationError(
            "DTC_EVAL_DEEPSEEK_BASE_URL must be the official https://api.deepseek.com"
        )
    return DedicatedEvalEnv(api_key=api_key, base_url=OFFICIAL_BASE_URL, file_mode="0600")


def validate_real_options(
    *,
    allow_real_model: bool,
    provider: str | None,
    model: str | None,
    max_cases: int | None,
    max_calls: int | None,
    max_output_tokens: int,
    thinking_mode: str,
) -> None:
    if not allow_real_model:
        raise ConfigurationError("real mode requires --allow-real-model")
    if provider != "deepseek":
        raise ConfigurationError("--provider deepseek is required in real mode")
    if not model or not model.strip():
        raise ConfigurationError("--model is required in real mode")
    if max_cases != MAX_DATASET_CASES:
        raise ConfigurationError("the sealed holdout must run all 12 cases; pass --max-cases 12")
    if max_calls is None or not 1 <= max_calls <= MAX_MODEL_CALLS:
        raise ConfigurationError(f"--max-calls must be between 1 and {MAX_MODEL_CALLS}")
    if not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise ConfigurationError(f"--max-output-tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
    if thinking_mode != "disabled":
        raise ConfigurationError("real mode requires --thinking-mode disabled")


def real_config_from_explicit_options(
    *,
    allow_real_model: bool,
    provider: str | None,
    model: str | None,
    max_cases: int | None,
    max_calls: int | None,
    max_output_tokens: int,
    thinking_mode: str,
) -> RealModelConfig:
    validate_real_options(
        allow_real_model=allow_real_model,
        provider=provider,
        model=model,
        max_cases=max_cases,
        max_calls=max_calls,
        max_output_tokens=max_output_tokens,
        thinking_mode=thinking_mode,
    )
    dedicated = load_dedicated_eval_env()
    assert provider is not None and model is not None and max_calls is not None
    return RealModelConfig(
        provider=provider,
        model=model.strip(),
        max_cases=MAX_DATASET_CASES,
        max_calls=max_calls,
        max_output_tokens=max_output_tokens,
        thinking_mode="disabled",
        api_key=dedicated.api_key,
        base_url=dedicated.base_url,
    )


# --------------------------------------------------------------- model plumbing


def _prompt_stage(messages: list[dict[str, str]]) -> str:
    system = next(
        (str(item.get("content") or "") for item in messages if item.get("role") == "system"),
        "",
    )
    for prompt, name in _STAGE_PROMPTS:
        prefix = prompt.split("{", 1)[0]
        if system == prompt or (prefix and system.startswith(prefix)):
            return name
    return "other"


class RecordingBoundedLLM(_BoundedLLM):
    """Count/cap calls and retain only whitelisted per-call metadata.

    Prompts, response bodies, reasoning, credentials and HTTP headers are never
    stored. Only the stage, model identifier, token usage, timing and JSON
    validity are retained.
    """

    def __init__(self, provider: Any, *, max_calls: int, max_output_tokens: int) -> None:
        super().__init__(provider, max_calls=max_calls, max_output_tokens=max_output_tokens)
        self.records: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ):
        stage = _prompt_stage(messages)
        started = time.perf_counter()
        try:
            response = await super().generate(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )
        except BaseException as exc:
            self.records.append(
                {
                    "stage": stage,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
            raise

        usage = response.usage
        content = response.content or ""
        effective_cap = min(int(max_tokens), self._max_output_tokens)
        json_valid: bool | None = None
        json_error_type: str | None = None
        if response_format == {"type": "json_object"}:
            try:
                json.loads(content)
                json_valid = True
            except (TypeError, ValueError) as exc:
                json_valid = False
                json_error_type = type(exc).__name__
        record: dict[str, Any] = {
            "stage": stage,
            "status": "ok",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "model": str(getattr(response, "model", "") or ""),
            "usage": {
                "prompt_tokens": int(usage.prompt_tokens),
                "completion_tokens": int(usage.completion_tokens),
                "total_tokens": int(usage.total_tokens),
            },
            "requested_max_tokens": int(max_tokens),
            "effective_max_tokens": effective_cap,
            "response_format": (
                "json_object"
                if response_format == {"type": "json_object"}
                else ("other" if response_format else None)
            ),
            "content_len": len(content),
            "json_valid": json_valid,
            "completion_at_cap": bool(
                effective_cap > 0 and int(usage.completion_tokens) >= effective_cap
            ),
        }
        if json_error_type is not None:
            record["json_error_type"] = json_error_type
        self.records.append(record)
        return response


def _build_model_run(config: RealModelConfig) -> ModelRun:
    settings = _settings_for(Path("unused").resolve()).model_copy(
        update={
            "deepseek_api_key": SecretStr(config.api_key),
            "deepseek_base_url": config.base_url,
            "deepseek_model": config.model,
            "deepseek_thinking_mode": config.thinking_mode,
            "deepseek_temperature": None,
            "deepseek_reasoning_effort": "",
            "deepseek_min_max_tokens": 0,
            "llm_max_retries": 1,
            "llm_timeout_seconds": 30.0,
        }
    )
    provider = DeepSeekProvider(settings)
    return ModelRun(
        mode="real",
        provider=config.provider,
        model=config.model,
        max_cases=config.max_cases,
        max_calls=config.max_calls,
        max_output_tokens=config.max_output_tokens,
        thinking_mode=config.thinking_mode,
        llm=RecordingBoundedLLM(
            provider,
            max_calls=config.max_calls,
            max_output_tokens=config.max_output_tokens,
        ),
    )


async def _close_model_run(model_run: ModelRun) -> None:
    closer = getattr(model_run.llm, "aclose", None)
    if closer is not None:
        await closer()


def _model_recording(model_run: ModelRun) -> RecordingBoundedLLM:
    llm = model_run.llm
    if not isinstance(llm, RecordingBoundedLLM):
        raise ConfigurationError("model run must use RecordingBoundedLLM")
    return llm


# ------------------------------------------------------------------- fixtures


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve()}"


def _settings_for(db_path: Path, *, category_map: str = "", after_sales: bool = True) -> Settings:
    """Offline-only settings with a new temporary SQLite target.

    The database URL is always derived from the runner's own temporary
    directory; no caller-supplied business URL is accepted.
    """
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_name="eval-after-sales-acceptance",
        app_env="testing",
        log_level="WARNING",
        database_url=_db_url(db_path),
        knowledge_dir=str(KNOWLEDGE_DIR),
        after_sales_enabled=after_sales,
        business_connector="fake",
        deepseek_api_key=SecretStr(""),
        deepseek_base_url="",
        deepseek_model="offline",
        deepseek_temperature=None,
        deepseek_reasoning_effort="",
        deepseek_thinking_mode=None,
        deepseek_min_max_tokens=0,
        llm_timeout_seconds=30.0,
        llm_max_retries=1,
        embedding_api_key=SecretStr(""),
        embedding_base_url="",
        local_embedding_enabled=False,
        local_embedding_force=False,
        cross_encoder_enabled=False,
        cross_encoder_force=False,
        unstructured_enabled=False,
        unstructured_force=False,
        mcp_enabled=False,
        mcp_remote_url="",
        redis_url="",
        qdrant_url="",
        admin_api_token=SecretStr(""),
        rate_limit_per_minute=1000,
        epig_base_url="",
        epig_service_token=SecretStr(""),
        epig_allowed_markets="",
        after_sales_sku_category_map=category_map,
        after_sales_worker_markets="",
        after_sales_worker_retry_base_seconds=0.1,
        after_sales_worker_retry_max_seconds=1.0,
    )


def _materials_for(identity: _FixtureIdentity) -> tuple[_SyntheticMaterial, ...]:
    if not identity.materials_required:
        return ()
    photo = (
        f"Synthetic {identity.market.upper()} {identity.name} acceptance photo: "
        "visible damage, packaging and order label."
    )
    record = (
        f"Synthetic {identity.market.upper()} {identity.name} order, delivery and "
        "packaging record."
    )
    return (
        _SyntheticMaterial(
            material_type="manual_verification",
            source=_FIXTURE_PHOTO_SOURCE,
            content=photo,
            applies_to_sku=identity.sku,
            delivered_at=_FIXTURE_DELIVERED_AT,
        ),
        _SyntheticMaterial(
            material_type="evidence",
            source=_FIXTURE_ORDER_SOURCE,
            content=record,
        ),
    )


def _build_fixture(row: dict[str, Any]) -> _SyntheticFixture:
    identity = _FIXTURE_IDENTITIES.get(str(row["id"]))
    if identity is None:
        raise _GuardSetupError("fixture_contract", "fixture_contract_missing")
    return _SyntheticFixture(
        market=identity.market,
        sku=identity.sku,
        name=identity.name,
        category=identity.category,
        request_type=identity.request_type,
        description=str(row["query"]) if identity.description_from_query else None,
        materials=_materials_for(identity),
        category_known=identity.category_known,
        description_from_query=identity.description_from_query,
        reset_attachments=identity.reset_attachments,
    )


def _fixture_for_row(row: dict[str, Any]) -> _SyntheticFixture:
    """Fixture used to configure the owned FakeConnector for this row."""
    return _build_fixture(row)


def _required_fixture_for_row(row: dict[str, Any]) -> _SyntheticFixture:
    """Independent required contract; tests may override only configuration."""
    return _build_fixture(row)


def _category_map_for_fixture(fixture: _SyntheticFixture) -> str:
    if not fixture.category_known:
        return ""
    return json.dumps({fixture.sku: fixture.category}, sort_keys=True)


def _configure_fake_fixture(connector: Any, fixture: _SyntheticFixture) -> None:
    """Apply this row's synthetic order/request facts to the owned connector."""
    state = getattr(connector, "_state", None)
    orders = getattr(state, "orders", None)
    requests = getattr(state, "requests", None)
    if not isinstance(orders, dict) or not isinstance(requests, dict):
        raise _GuardSetupError("fake_fixture", "fake connector fixture state is unavailable")

    order_found = False
    for (market, order_id), order in list(orders.items()):
        if market != fixture.market:
            continue
        items = list(order.items)
        if not items:
            raise _GuardSetupError("fake_fixture", "fake order has no items")
        items[0] = items[0].model_copy(update={"sku": fixture.sku, "name": fixture.name})
        orders[(market, order_id)] = order.model_copy(update={"items": items})
        order_found = True
        break
    if not order_found:
        raise _GuardSetupError("fake_fixture", "fake order for market is unavailable")

    for (market, _request_id), item in requests.items():
        if market != fixture.market:
            continue
        updates: dict[str, Any] = {"type": fixture.request_type}
        if fixture.reset_attachments:
            updates["attachments"] = []
        if fixture.description is not None:
            updates["description"] = fixture.description
        item.request = item.request.model_copy(update=updates)
        return
    raise _GuardSetupError("fake_fixture", "fake request for market is unavailable")


def _material_payloads(fixture: _SyntheticFixture) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for material in fixture.materials:
        payload: dict[str, Any] = {
            "material_type": material.material_type,
            "source": material.source,
            "content": material.content,
        }
        if material.applies_to_sku is not None:
            payload["applies_to_sku"] = material.applies_to_sku
        if material.delivered_at is not None:
            payload["delivered_at"] = material.delivered_at.isoformat()
        payloads.append(payload)
    return payloads


async def _seed_case_materials(
    client: httpx.AsyncClient,
    market: str,
    case_id: int,
    token: str,
    fixture: _SyntheticFixture,
) -> tuple[int, dict[str, Any]]:
    """Write owned synthetic proof materials through the staff material API."""
    detail = await _case_detail(client, market, case_id, token)
    for payload in _material_payloads(fixture):
        response = await client.post(
            f"/api/v1/after-sales/{market}/cases/{case_id}/materials",
            headers=_auth(token),
            json={**payload, "expected_revision": detail["revision"]},
        )
        if response.status_code != 200:
            raise _GuardSetupError("materials", f"status_{response.status_code}")
        detail = _safe_body(response)
    return len(fixture.materials), detail


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _material_facts(materials: list[Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for item in materials:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        facts.append(
            {
                "material_type": item.get("material_type"),
                "source": item.get("source"),
                "content_sha256": _sha256_text(content),
                "content_len": len(content),
                "content_preview": content[:160],
                "applies_to_sku": item.get("applies_to_sku"),
                "delivered_at": item.get("delivered_at"),
                "created_by_staff_id": item.get("created_by_staff_id"),
            }
        )
    return facts


def _validate_fixture(
    detail: dict[str, Any],
    evidence: dict[str, Any],
    required: _SyntheticFixture,
) -> _FixtureValidation:
    """Compare actual API facts with the row's required synthetic fixture.

    This is a hard gate before any model call. It checks order identity, the
    request facts, the server-trusted category and material completeness. It
    never repairs a mismatch and never writes approval/completion state.
    """
    errors: list[str] = []
    order = detail.get("order") if isinstance(detail.get("order"), dict) else None
    item: dict[str, Any] = {}
    if order is None:
        errors.append("order_missing")
    else:
        if order.get("market") != required.market:
            errors.append("order_market_mismatch")
        items = order.get("items") if isinstance(order.get("items"), list) else []
        item = items[0] if items and isinstance(items[0], dict) else {}
        if item.get("sku") != required.sku:
            errors.append("order_sku_mismatch")
        if item.get("name") != required.name:
            errors.append("order_name_mismatch")

    if detail.get("request_type") != required.request_type:
        errors.append("request_type_mismatch")
    actual_description = str(detail.get("description") or "")
    if required.description is not None:
        if actual_description != required.description:
            errors.append("request_description_mismatch")
    elif not actual_description:
        errors.append("request_description_missing")

    raw_categories = evidence.get("trusted_categories")
    categories: list[Any] = raw_categories if isinstance(raw_categories, list) else []
    actual_sku = item.get("sku")
    matching_category = next(
        (
            category
            for category in categories
            if isinstance(category, dict) and category.get("sku") == actual_sku
        ),
        None,
    )
    if required.category_known:
        if matching_category is None:
            errors.append("trusted_category_missing")
        else:
            if matching_category.get("category") != required.category:
                errors.append("trusted_category_mismatch")
            if matching_category.get("source") != "server_config":
                errors.append("trusted_category_source_not_server_config")
    else:
        if matching_category is not None:
            errors.append("unknown_category_was_resolved")
        raw_missing_reasons = evidence.get("missing_reasons")
        missing_reasons: list[Any] = (
            raw_missing_reasons if isinstance(raw_missing_reasons, list) else []
        )
        if "missing_sku_category" not in missing_reasons:
            errors.append("unknown_category_reason_missing")

    raw_materials = detail.get("materials")
    materials: list[Any] = raw_materials if isinstance(raw_materials, list) else []
    actual_signatures = Counter(
        (
            str(material.get("material_type")),
            str(material.get("source")),
            _sha256_text(str(material.get("content") or "")),
        )
        for material in materials
        if isinstance(material, dict)
    )
    for expected in required.materials:
        signature = (
            expected.material_type,
            expected.source,
            _sha256_text(expected.content),
        )
        if actual_signatures[signature] < 1:
            errors.append(f"material_missing:{expected.material_type}")
        if expected.material_type == "manual_verification":
            match = next(
                (
                    material
                    for material in materials
                    if isinstance(material, dict)
                    and material.get("material_type") == "manual_verification"
                    and material.get("source") == expected.source
                    and _sha256_text(str(material.get("content") or ""))
                    == _sha256_text(expected.content)
                ),
                None,
            )
            if match is None:
                errors.append("manual_verification_missing")
            else:
                if match.get("applies_to_sku") != required.sku:
                    errors.append("material_sku_mismatch")
                if not match.get("delivered_at"):
                    errors.append("material_delivered_at_missing")

    material_revision = int(detail.get("material_revision") or 0)
    if material_revision < len(required.materials):
        errors.append("material_revision_incomplete")
    if detail.get("drafts"):
        errors.append("unexpected_pre_suggestion_drafts")
    if detail.get("approvals"):
        errors.append("unexpected_pre_suggestion_approvals")
    if detail.get("operations"):
        errors.append("unexpected_pre_suggestion_operations")

    facts = {
        "schema_version": 1,
        "market": detail.get("market"),
        "order_id": order.get("order_id") if order else None,
        "order_version": order.get("order_version") if order else None,
        "sku": item.get("sku"),
        "name": item.get("name"),
        "quantity": item.get("quantity"),
        "category": matching_category.get("category") if matching_category else None,
        "category_source": matching_category.get("source") if matching_category else None,
        "category_known": required.category_known,
        "request_id": detail.get("request_id"),
        "request_type": detail.get("request_type"),
        "description": actual_description,
        "description_required_from_query": required.description is not None,
        "material_revision": material_revision,
        "materials": _material_facts(materials),
        "pre_suggestion_counts": {
            "drafts": len(detail.get("drafts") or []),
            "approvals": len(detail.get("approvals") or []),
            "operations": len(detail.get("operations") or []),
        },
    }
    return _FixtureValidation(
        ok=not errors,
        errors=tuple(dict.fromkeys(errors)),
        facts=facts,
        sha256=_sha256_text(_canonical_json(facts)),
    )


def _fixture_failure_result(
    row: dict[str, Any],
    *,
    boundary: str,
    validation: _FixtureValidation,
    status: str,
    score_scope: str | None = None,
    elapsed_ms: float = 0.0,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = _case_base(row, boundary)
    result.update(
        {
            "status": status,
            "error_type": "FixtureValidationError",
            "stage": "fixture_validation",
            "fixture": validation.as_dict(),
            "answer": None,
            "citations": [],
            "machine_checks": {"fixture_valid": False},
            "model": _model_slice(records or []),
            "latency_ms": round(elapsed_ms, 3),
        }
    )
    if score_scope is not None:
        result["score_scope"] = score_scope
    return result


def _staff_spec(kind: dict[str, StaffSpec], market: str) -> StaffSpec:
    return kind[market]


async def _seed_staff(
    session_factory: async_sessionmaker[AsyncSession],
    specs: list[tuple[str, tuple[StaffRole, ...], tuple[AfterSalesMarket, ...]]],
) -> dict[str, str]:
    tokens: dict[str, str] = {}
    async with session_factory() as session:
        repository = StaffRepository(session)
        for staff_id, roles, markets in specs:
            token = generate_staff_token()
            tokens[staff_id] = token
            await repository.create(
                staff_id=staff_id,
                display_name=f"Synthetic {staff_id}",
                token=token,
                roles=roles,
                allowed_markets=markets,
                expires_at=utcnow() + timedelta(days=1),
            )
        await session.commit()
    return tokens


async def _authenticate(
    session_factory: async_sessionmaker[AsyncSession], token: str
) -> StaffActor:
    async with session_factory() as session:
        actor = await StaffRepository(session).authenticate(token)
    if actor is None:
        raise _GuardSetupError("staff_auth", "seeded credential did not authenticate")
    return actor


def _seed_staff_at_path(
    db_path: Path,
    specs: list[tuple[str, tuple[StaffRole, ...], tuple[AfterSalesMarket, ...]]],
) -> dict[str, str]:
    async def _run() -> dict[str, str]:
        engine = create_async_engine(_db_url(db_path))
        try:
            await init_schema(engine)
            factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
            return await _seed_staff(factory, specs)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _running_app(settings: Settings) -> AsyncIterator[tuple[Any, AppContainer]]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app, app.state.container


def _asgi_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://acceptance.invalid",
        timeout=httpx.Timeout(30.0),
    )


def _safe_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _error_code(response: httpx.Response) -> str | None:
    value = _safe_body(response).get("error_code")
    return value if isinstance(value, str) else None


async def _case_list(client: httpx.AsyncClient, market: str, token: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/after-sales/{market}/cases", headers=_auth(token))
    if response.status_code != 200:
        raise _GuardSetupError("case_list", f"status_{response.status_code}")
    items = _safe_body(response).get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


async def _case_detail(
    client: httpx.AsyncClient, market: str, case_id: int, token: str
) -> dict[str, Any]:
    response = await client.get(
        f"/api/v1/after-sales/{market}/cases/{case_id}", headers=_auth(token)
    )
    if response.status_code != 200:
        raise _GuardSetupError("case_detail", f"status_{response.status_code}")
    body = _safe_body(response)
    if not body:
        raise _GuardSetupError("case_detail", "empty_body")
    return body


async def _first_case_detail(client: httpx.AsyncClient, market: str, token: str) -> dict[str, Any]:
    items = await _case_list(client, market, token)
    if not items:
        raise _GuardSetupError("case_list", "no_cases")
    case_id = items[0].get("case_id")
    if not isinstance(case_id, int):
        raise _GuardSetupError("case_list", "invalid_case_id")
    return await _case_detail(client, market, case_id, token)


async def _case_evidence_body(
    client: httpx.AsyncClient, market: str, case_id: int, token: str
) -> dict[str, Any]:
    response = await client.get(
        f"/api/v1/after-sales/{market}/cases/{case_id}/evidence",
        headers=_auth(token),
    )
    if response.status_code != 200:
        raise _GuardSetupError("evidence", f"status_{response.status_code}")
    return _safe_body(response)


async def _evidence_chunks(
    client: httpx.AsyncClient, market: str, case_id: int, token: str
) -> list[dict[str, Any]]:
    body = await _case_evidence_body(client, market, case_id, token)
    chunks = body.get("chunks")
    return (
        [chunk for chunk in chunks if isinstance(chunk, dict)] if isinstance(chunks, list) else []
    )


async def _pick_evidence_chunk(
    client: httpx.AsyncClient,
    market: str,
    case_id: int,
    token: str,
    stem: str,
) -> str:
    chunks = await _evidence_chunks(client, market, case_id, token)
    for chunk in chunks:
        source_path = str(chunk.get("source_path") or "")
        if Path(source_path).stem == stem and isinstance(chunk.get("chunk_id"), str):
            return str(chunk["chunk_id"])
    if chunks and isinstance(chunks[0].get("chunk_id"), str):
        return str(chunks[0]["chunk_id"])
    raise _GuardSetupError("evidence", "missing_chunk")


def _detail_counts(detail: dict[str, Any]) -> dict[str, int]:
    return {
        "drafts": len(detail.get("drafts") or []),
        "approvals": len(detail.get("approvals") or []),
        "operations": len(detail.get("operations") or []),
    }


def _count_delta(before: dict[str, int], after: dict[str, int], key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


def _create_calls(container: AppContainer) -> int:
    connector = container.business_connector
    if connector is None:
        return 0
    return int(getattr(connector, "create_ticket_calls", 0))


# ------------------------------------------------------------- result assembly


def _manual_pending() -> dict[str, Any]:
    return {
        "status": "pending_codex",
        "axes": list(_MANUAL_AXES),
        "scale": [0, 1, 2],
        "note": "Machine checks are not a semantic verdict.",
    }


def _case_base(row: dict[str, Any], boundary: str) -> dict[str, Any]:
    manual = (
        _manual_pending()
        if row["partition"] in MODEL_PARTITIONS
        else {"status": "not_applicable", "reason": "engineering_guard"}
    )
    return {
        "id": row["id"],
        "partition": row["partition"],
        "query": row["query"],
        "brand": row["brand"],
        "market": row["market"],
        "product_category": row["product_category"],
        "as_of": row["as_of"],
        "fixture_boundary": row["fixture_boundary"],
        "expected_documents": list(row["expected_documents"]),
        "rubric": {
            "acceptance_assertions": list(row["acceptance_assertions"]),
            "forbidden": list(row["forbidden"]),
            "note": "Human rubric; never supplied to the model as instructions or code.",
        },
        "boundary": boundary,
        "manual_review": manual,
    }


def _not_run(row: dict[str, Any], reason: str, boundary: str) -> dict[str, Any]:
    result = _case_base(row, boundary)
    result.update(
        {
            "status": "not_run",
            "reason": reason,
            "model": {"lane": "not_run", "calls": 0, "records": []},
        }
    )
    return result


def _model_slice(records: list[dict[str, Any]]) -> dict[str, Any]:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    models: set[str] = set()
    errors: list[str] = []
    stages: list[str] = []
    successful = 0
    provider_errors = 0
    budget_blocked = 0
    json_invalid = 0
    truncation = 0
    for record in records:
        stages.append(str(record.get("stage") or "other"))
        if record.get("status") == "ok":
            successful += 1
            raw_usage = record.get("usage") or {}
            for key in usage:
                usage[key] += int(raw_usage.get(key) or 0)
            if record.get("model"):
                models.add(str(record["model"]))
            if record.get("json_valid") is False:
                json_invalid += 1
            if record.get("completion_at_cap") is True:
                truncation += 1
        else:
            error_type = str(record.get("error_type") or "unknown")
            errors.append(error_type)
            if error_type == "ModelBudgetExceeded":
                budget_blocked += 1
            else:
                provider_errors += 1
    actual_provider_calls = len(records) - budget_blocked
    return {
        "calls": len(records),
        "actual_provider_calls": actual_provider_calls,
        "successful_calls": successful,
        "provider_error_calls": provider_errors,
        "budget_blocked_calls": budget_blocked,
        "usage": usage,
        "models_actual": sorted(models),
        "stages": stages,
        "json_invalid_calls": json_invalid,
        "possible_truncation_calls": truncation,
        "errors": errors,
        "records": records,
    }


def _provider_failure(records: list[dict[str, Any]]) -> bool:
    return any(
        record.get("status") == "error" and record.get("error_type") == "LLMProviderError"
        for record in records
    )


def _budget_failure(records: list[dict[str, Any]]) -> bool:
    return any(record.get("error_type") == "ModelBudgetExceeded" for record in records)


def _failure_result(
    row: dict[str, Any],
    *,
    boundary: str,
    status: str,
    error_type: str,
    stage: str,
    records: list[dict[str, Any]],
    elapsed_ms: float,
) -> dict[str, Any]:
    result = _case_base(row, boundary)
    result.update(
        {
            "status": status,
            "error_type": error_type,
            "stage": stage,
            "latency_ms": round(elapsed_ms, 3),
            "answer": None,
            "citations": [],
            "machine_checks": {},
            "model": _model_slice(records),
        }
    )
    return result


# --------------------------------------------------------------- policy graph


class _RecordingChatService(ChatService):
    """ChatService that captures the graph's single final state."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.captured_state: dict[str, Any] | None = None

    async def _invoke(self, state: Any) -> Any:
        result = await super()._invoke(state)
        self.captured_state = dict(result)
        return result


def _chat_service_with_model(container: AppContainer, llm: LLMProvider) -> _RecordingChatService:
    """Mirror eval.runner's verified helper, preserving staff mode explicitly."""
    old = container.chat_service
    service = _RecordingChatService(
        settings=container.settings,
        llm=llm,
        retriever=container.retriever,
        reranker=old._reranker,
        session_factory=container.session_factory,
        rate_limiter=old._rate_limiter,
        registry=old.registry,
        staff_mode=old._staff_mode,
    )
    if not service._staff_mode:
        raise ConfigurationError("staff mode was dropped while injecting the eval model")
    return service


def _citation_view(citation: Any) -> dict[str, Any]:
    return {
        "chunk_id": citation.chunk_id,
        "document_id": citation.document_id,
        "title": citation.title,
        "section": citation.section,
        "policy_version": citation.policy_version,
        "brand": citation.brand,
        "market": citation.market,
        "source_path": citation.source_path,
        "requires_human_review": citation.requires_human_review,
    }


def _chunk_view(chunk: Any) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "title": chunk.title,
        "section": chunk.section,
        "policy_version": chunk.policy_version,
        "source_path": chunk.source_path,
        "requires_human_review": chunk.requires_human_review,
    }


def _document_stems(citations: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            Path(str(citation.get("source_path") or "")).stem
            for citation in citations
            if citation.get("source_path")
        }
    )


def _generation_stage(state: dict[str, Any], records: list[dict[str, Any]]) -> str:
    stages = {str(record.get("stage")) for record in records if record.get("status") == "ok"}
    if not records:
        return "not_run"
    if "answer_generation" in stages:
        return "model_answer_generation_attempted"
    if state.get("refused"):
        return "deterministic_review_or_refusal"
    return "no_answer_generation_call"


async def _one_policy_case(
    row: dict[str, Any],
    *,
    service: _RecordingChatService,
    trace: ToolTrace,
    model_run: ModelRun,
    actors: dict[str, StaffActor],
    corpus: dict[str, Any],
    registry_tools: list[str],
) -> tuple[dict[str, Any], str | None]:
    from app.retrieval.policy import chunk_matches_scope

    llm = _model_recording(model_run)
    market = str(row["market"])
    actor = actors.get(market)
    if actor is None:
        raise _GuardSetupError("staff_actor", f"missing_actor_{market}")

    request = ChatRequest(
        message=str(row["query"]),
        brand="epig",
        market=cast(Any, market),
        policy_as_of=date.fromisoformat(str(row["as_of"])),
        product_category=str(row["product_category"]),
        user_id="client-supplied-identity-is-replaced",
    )
    start_index = len(llm.records)
    started = time.perf_counter()
    response: Any | None = None
    error: BaseException | None = None
    service.captured_state = None
    trace.begin_case(str(row["id"]))
    try:
        response = await service.chat(request, trace_id=new_trace_id(), staff=actor)
    except BaseException as exc:
        error = exc
    finally:
        tool_records = trace.end_case()
    elapsed_ms = (time.perf_counter() - started) * 1000
    records = llm.records[start_index:]

    if error is not None:
        status = (
            "budget_exhausted"
            if isinstance(error, Exception) and type(error).__name__ == "ModelBudgetExceeded"
            else "model_case_failed"
        )
        result = _failure_result(
            row,
            boundary=BOUNDARY_POLICY,
            status=status,
            error_type=type(error).__name__,
            stage="chat_graph",
            records=records,
            elapsed_ms=elapsed_ms,
        )
        if isinstance(error, LLMProviderError):
            return result, "provider_failure_stop"
        if type(error).__name__ == "ModelBudgetExceeded":
            return result, "global_model_budget_exhausted"
        return result, None

    assert response is not None
    state: dict[str, Any] = service.captured_state or {}
    citations = [_citation_view(item) for item in response.citations]
    retrieved = [_chunk_view(item) for item in (state.get("retrieved_chunks") or [])]
    raw_extra = state.get("extra")
    extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    faithfulness = (
        extra.get("faithfulness") if isinstance(extra.get("faithfulness"), dict) else None
    )
    scope = state.get("policy_scope")
    citation_scope_valid = True
    if scope is not None:
        for citation in citations:
            chunk = corpus.get(str(citation["chunk_id"]))
            if chunk is None or not chunk_matches_scope(chunk, scope):
                citation_scope_valid = False
                break
    cited_documents = _document_stems(citations)
    expected_documents = [str(item) for item in row["expected_documents"]]
    tool_names = [str(record.get("name")) for record in tool_records]
    model_slice = _model_slice(records)
    stages = model_slice["stages"]
    result = _case_base(row, BOUNDARY_POLICY)
    result.update(
        {
            "status": "model_case_observed" if records else "model_case_no_calls",
            "transport": "direct_asyncio_chat_service",
            "fixture": {
                "kind": "policy_scope_only",
                "order_fixture": False,
                "note": (
                    "No business order fixture is used; the frozen row scope and query "
                    "drive the policy graph."
                ),
            },
            "retrieval": {
                "kind": "hybrid_bm25_hash_embedding_lexical",
                "real_embedding_quality": "not_measured",
                "note": "Hash embeddings + BM25/lexical fusion; not a real embedding-quality test.",
            },
            "staff_mode": True,
            "staff_id": actor.staff_id,
            "answer": str(response.answer)[:8000],
            "refused": bool(response.refused),
            "error_code": response.error_code,
            "strategy": response.strategy.value if response.strategy else None,
            "intent": response.intent.value if response.intent else None,
            "complexity": response.complexity.value if response.complexity else None,
            "generation_stage": _generation_stage(state, records),
            "answer_generation_calls": sum(
                1
                for record in records
                if record.get("stage") == "answer_generation" and record.get("status") == "ok"
            ),
            "faithfulness": {
                "ran": bool(faithfulness),
                "same_provider_automatic": True,
                "supported": bool(faithfulness.get("supported")) if faithfulness else None,
                "rationale": str(faithfulness.get("rationale") or "")[:500]
                if faithfulness
                else None,
            },
            "self_corrected": extra.get("self_corrected"),
            "stages_called": stages,
            "citations": citations,
            "cited_documents": cited_documents,
            "retrieved_chunks": retrieved,
            "tool_calls": tool_records,
            "tool_names": tool_names,
            "registry_tools": registry_tools,
            "latency_ms": round(elapsed_ms, 3),
            "app_token_usage": response.token_usage.model_dump(),
            "app_latency_ms": response.latency.total_ms,
            "machine_checks": {
                "answer_present": bool(response.answer),
                "citations_present": bool(citations),
                "citation_scope_valid": citation_scope_valid,
                "expected_documents_present": set(expected_documents) <= set(cited_documents),
                "no_demo_write_tools_available": not (set(registry_tools) & DEMO_WRITE_TOOLS),
                "no_write_tool_calls": not (set(tool_names) & DEMO_WRITE_TOOLS),
                "faithfulness_ran": bool(faithfulness),
                "faithfulness_supported": (
                    bool(faithfulness.get("supported")) if faithfulness else None
                ),
            },
            "model": model_slice,
        }
    )
    if _budget_failure(records):
        result["status"] = "budget_exhausted"
        return result, "global_model_budget_exhausted"
    if _provider_failure(records):
        result["status"] = "model_case_failed"
        result["error_type"] = "LLMProviderError"
        result["stage"] = "model_provider"
        return result, "provider_failure_stop"
    return result, None


async def _run_policy_graph_cases(
    rows: list[dict[str, Any]],
    model_run: ModelRun,
    work_root: Path,
) -> _Batch:
    if not rows:
        return _Batch([])
    db_path = work_root / "policy_graph" / "app.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _settings_for(db_path)
    container: AppContainer | None = None
    trace: ToolTrace | None = None
    try:
        container = await build_container(settings)
        specs = [
            _staff_spec(_STAFF_SUPPORT, "au"),
            _staff_spec(_STAFF_SUPPORT, "sg"),
            _staff_spec(_STAFF_SUPPORT, "us"),
        ]
        tokens = await _seed_staff(container.session_factory, specs)
        actors = {
            market: await _authenticate(container.session_factory, tokens[staff_id])
            for market, staff_id in (
                ("au", "staff_support_au"),
                ("sg", "staff_support_sg"),
                ("us", "staff_support_us"),
            )
        }
        service = _chat_service_with_model(container, model_run.llm)
        trace = ToolTrace(sanitize=lambda value: _redact(value))
        trace.install(service.registry)
        registry_tools = sorted(tool.name for tool in service.registry.all())
        corpus = {chunk.chunk_id: chunk for chunk in container.retriever.corpus}
        results: list[dict[str, Any]] = []
        for row in rows:
            result, stop_reason = await _one_policy_case(
                row,
                service=service,
                trace=trace,
                model_run=model_run,
                actors=actors,
                corpus=corpus,
                registry_tools=registry_tools,
            )
            results.append(result)
            if stop_reason is not None:
                return _Batch(results, stop_reason)
        return _Batch(results)
    finally:
        if trace is not None:
            trace.uninstall()
        if container is not None:
            await container.aclose()


# --------------------------------------------------------------- staff suggest


def _suggestion_candidate(detail: dict[str, Any]) -> dict[str, Any] | None:
    current = detail.get("current_draft")
    if not isinstance(current, dict):
        return None
    evidence = current.get("evidence")
    citations = (
        [
            {
                "chunk_id": item.get("chunk_id"),
                "source_path": item.get("source_path"),
                "policy_version": item.get("policy_version"),
            }
            for item in evidence
            if isinstance(item, dict)
        ]
        if isinstance(evidence, list)
        else []
    )
    return {
        "draft_id": current.get("draft_id"),
        "version": current.get("version"),
        "status": current.get("status"),
        "action": current.get("action"),
        "internal_summary": str(current.get("internal_summary") or "")[:4000],
        "consumer_reply": str(current.get("consumer_reply") or "")[:8000],
        "citations": citations,
    }


def _trusted_suggestion_citations(candidate: dict[str, Any] | None, market: str) -> bool:
    if candidate is None:
        return False
    citations = candidate.get("citations") or []
    if not citations:
        return False
    for item in citations:
        source_path = str(item.get("source_path") or "")
        if not source_path.startswith(f"epig/{market}/"):
            return False
        if not item.get("policy_version"):
            return False
    return True


async def _one_suggestion_case(
    row: dict[str, Any],
    model_run: ModelRun,
    work_root: Path,
) -> tuple[dict[str, Any], str | None]:
    llm = _model_recording(model_run)
    market = str(row["market"])
    fixture = _fixture_for_row(row)
    required = _required_fixture_for_row(row)
    case_root = work_root / "staff_suggestion" / str(row["id"])
    case_root.mkdir(parents=True, exist_ok=True)
    settings = _settings_for(
        case_root / "app.db",
        category_map=_category_map_for_fixture(fixture),
    )
    started = time.perf_counter()
    start_index = len(llm.records)
    validation: _FixtureValidation | None = None
    try:
        async with _running_app(settings) as (app, container):
            workflow = container.after_sales_workflow
            if workflow is None:
                raise _GuardSetupError("container", "after_sales_workflow_unavailable")
            tokens = await _seed_staff(
                container.session_factory,
                [_staff_spec(_STAFF_SUPPORT, market)],
            )
            token = tokens[f"staff_support_{market}"]
            _configure_fake_fixture(container.business_connector, fixture)
            async with _asgi_client(app) as client:
                sync = await client.post(
                    f"/api/v1/after-sales/{market}/cases/sync", headers=_auth(token)
                )
                if sync.status_code != 200:
                    raise _GuardSetupError("sync", f"status_{sync.status_code}")
                detail = await _first_case_detail(client, market, token)
                case_id = detail["case_id"]
                material_count, detail = await _seed_case_materials(
                    client, market, case_id, token, fixture
                )
                evidence = await _case_evidence_body(client, market, case_id, token)
                validation = _validate_fixture(detail, evidence, required)
                if not validation.ok:
                    return (
                        _fixture_failure_result(
                            row,
                            boundary=BOUNDARY_ASGI,
                            validation=validation,
                            status="fixture_invalid",
                            elapsed_ms=(time.perf_counter() - started) * 1000,
                        ),
                        "fixture_invalid_stop",
                    )
                workflow._llm = llm
                before = _detail_counts(detail)
                create_before = _create_calls(container)
                response = await client.post(
                    f"/api/v1/after-sales/{market}/cases/{case_id}/suggestion",
                    headers=_auth(token),
                    json={"expected_revision": detail["revision"]},
                )
                after_detail = await _case_detail(client, market, case_id, token)
                after = _detail_counts(after_detail)
                create_after = _create_calls(container)
    except Exception as exc:
        result = _failure_result(
            row,
            boundary=BOUNDARY_ASGI,
            status="model_case_failed",
            error_type=type(exc).__name__,
            stage=getattr(exc, "stage", "suggestion_setup"),
            records=llm.records[start_index:],
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
        if validation is not None:
            result["fixture"] = validation.as_dict()
        return result, None

    assert validation is not None
    elapsed_ms = (time.perf_counter() - started) * 1000
    records = llm.records[start_index:]
    model_slice = _model_slice(records)
    body = _safe_body(response)
    error_code = body.get("error_code") if isinstance(body.get("error_code"), str) else None
    candidate = _suggestion_candidate(after_detail) if response.status_code == 200 else None
    trusted_citations = _trusted_suggestion_citations(candidate, market)
    draft_delta = _count_delta(before, after, "drafts")
    approval_delta = _count_delta(before, after, "approvals")
    operation_delta = _count_delta(before, after, "operations")
    create_delta = create_after - create_before
    checks = {
        "fixture_valid": validation.ok,
        "http_success_or_strict_502": response.status_code in {200, 502},
        "draft_is_editing": bool(candidate and candidate.get("status") == "editing"),
        "trusted_source_citations_only": trusted_citations,
        "no_new_approval": approval_delta == 0,
        "no_new_operation": operation_delta == 0,
        "no_business_create": create_delta == 0,
        "no_early_execution": not (after_detail.get("operations") or []),
    }
    if response.status_code == 200:
        status = "model_case_observed" if all(checks.values()) else "model_case_guard_failed"
    elif response.status_code == 502:
        status = "model_case_failed_closed"
    elif response.status_code == 503 and (_budget_failure(records) or _provider_failure(records)):
        status = "model_case_failed"
    else:
        status = "model_case_unexpected_status"
    if not records:
        status = "model_case_no_calls"
    result = _case_base(row, BOUNDARY_ASGI)
    result.update(
        {
            "status": status,
            "transport": "httpx_asgi_transport_in_process",
            "fixture": validation.as_dict(),
            "material_count": material_count,
            "http_status": response.status_code,
            "error_code": error_code,
            "candidate": candidate,
            "answer": candidate.get("consumer_reply") if candidate else None,
            "citations": candidate.get("citations") if candidate else [],
            "counts_before": before,
            "counts_after": after,
            "deltas": {
                "drafts": draft_delta,
                "approvals": approval_delta,
                "operations": operation_delta,
                "business_create_calls": create_delta,
            },
            "machine_checks": checks,
            "latency_ms": round(elapsed_ms, 3),
            "model": model_slice,
        }
    )
    if candidate is not None:
        result["draft_status"] = candidate.get("status")
        result["trusted_citations_only"] = trusted_citations
    if _budget_failure(records):
        result["status"] = "budget_exhausted"
        return result, "global_model_budget_exhausted"
    if _provider_failure(records):
        result["status"] = "model_case_failed"
        result["error_type"] = "LLMProviderError"
        result["stage"] = "model_provider"
        return result, "provider_failure_stop"
    return result, None


async def _run_suggestion_cases(
    rows: list[dict[str, Any]],
    model_run: ModelRun,
    work_root: Path,
) -> _Batch:
    results: list[dict[str, Any]] = []
    for row in rows:
        result, stop_reason = await _one_suggestion_case(row, model_run, work_root)
        results.append(result)
        if stop_reason is not None:
            return _Batch(results, stop_reason)
    return _Batch(results)


async def _run_model_cases(
    rows: list[dict[str, Any]],
    model_run: ModelRun,
    work_root: Path,
) -> list[dict[str, Any]]:
    policy_rows = [row for row in rows if row["partition"] == "policy_graph"]
    suggestion_rows = [row for row in rows if row["partition"] == "staff_suggestion"]
    results: list[dict[str, Any]] = []
    stop_reason: str | None = None

    if policy_rows:
        try:
            batch = await _run_policy_graph_cases(policy_rows, model_run, work_root)
            results.extend(batch.results)
            stop_reason = batch.stop_reason
        except Exception as exc:
            results.extend(
                _failure_result(
                    row,
                    boundary=BOUNDARY_POLICY,
                    status="model_case_failed",
                    error_type=type(exc).__name__,
                    stage="policy_graph_setup",
                    records=[],
                    elapsed_ms=0.0,
                )
                for row in policy_rows
            )
            stop_reason = "model_lane_setup_failed"

    if suggestion_rows:
        if stop_reason is not None:
            results.extend(_not_run(row, stop_reason, BOUNDARY_ASGI) for row in suggestion_rows)
        else:
            batch = await _run_suggestion_cases(suggestion_rows, model_run, work_root)
            results.extend(batch.results)
            stop_reason = batch.stop_reason
            if stop_reason is not None:
                done = {result["id"] for result in results}
                results.extend(
                    _not_run(row, stop_reason, BOUNDARY_ASGI)
                    for row in suggestion_rows
                    if row["id"] not in done
                )
    return results


async def _run_real_model_cases(
    rows: list[dict[str, Any]],
    config: RealModelConfig,
    work_root: Path,
) -> list[dict[str, Any]]:
    model_run = _build_model_run(config)
    try:
        return await _run_model_cases(rows, model_run, work_root)
    finally:
        await _close_model_run(model_run)


# ------------------------------------------------------------ engineering guards


async def _run_h09(row: dict[str, Any], work_root: Path) -> dict[str, Any]:
    result = _case_base(row, BOUNDARY_ASGI)
    result.update(
        {
            "score_scope": "engineering_only",
            "transport": "httpx_asgi_transport_in_process",
            "fixture_note": (
                "Unknown category is intentional; no materials are required before the "
                "fail-closed guard."
            ),
            "model": {"lane": "not_run", "calls": 0, "guard_probe_invocations": 0},
        }
    )
    probe = _GuardProbeLLM()
    stage = "setup"
    fixture = _fixture_for_row(row)
    required = _required_fixture_for_row(row)
    validation: _FixtureValidation | None = None
    try:
        case_root = work_root / "engineering" / "H09"
        case_root.mkdir(parents=True, exist_ok=True)
        settings = _settings_for(
            case_root / "app.db",
            category_map=_category_map_for_fixture(fixture),
        )
        async with _running_app(settings) as (app, container):
            workflow = container.after_sales_workflow
            if workflow is None:
                raise _GuardSetupError("container", "after_sales_workflow_unavailable")
            tokens = await _seed_staff(
                container.session_factory, [_staff_spec(_STAFF_SUPPORT, "au")]
            )
            _configure_fake_fixture(container.business_connector, fixture)
            token = tokens["staff_support_au"]
            async with _asgi_client(app) as client:
                stage = "sync"
                sync = await client.post("/api/v1/after-sales/au/cases/sync", headers=_auth(token))
                if sync.status_code != 200:
                    raise _GuardSetupError(stage, f"status_{sync.status_code}")
                detail = await _first_case_detail(client, "au", token)
                evidence = await _case_evidence_body(client, "au", detail["case_id"], token)
                validation = _validate_fixture(detail, evidence, required)
                if not validation.ok:
                    result.update(
                        {
                            "status": "guard_failed",
                            "stage": "fixture_validation",
                            "error_type": "FixtureValidationError",
                            "fixture": validation.as_dict(),
                            "machine_checks": {"fixture_valid": False},
                        }
                    )
                    result["model"] = {
                        "lane": "not_run",
                        "calls": 0,
                        "guard_probe_invocations": probe.calls,
                    }
                    return result
                workflow._llm = cast(Any, probe)
                before = _detail_counts(detail)
                create_before = _create_calls(container)
                stage = "suggestion"
                response = await client.post(
                    f"/api/v1/after-sales/au/cases/{detail['case_id']}/suggestion",
                    headers=_auth(token),
                    json={"expected_revision": detail["revision"]},
                )
                after_detail = await _case_detail(client, "au", detail["case_id"], token)
                after = _detail_counts(after_detail)
                create_after = _create_calls(container)
        checks = {
            "fixture_valid": validation.ok,
            "http_409": response.status_code == 409,
            "error_code_state_conflict": _error_code(response) == "STATE_CONFLICT",
            "guard_probe_not_invoked": probe.calls == 0,
            "no_new_draft": _count_delta(before, after, "drafts") == 0,
            "no_new_approval": _count_delta(before, after, "approvals") == 0,
            "no_new_operation": _count_delta(before, after, "operations") == 0,
            "no_business_create": create_after - create_before == 0,
            "case_not_completed": after_detail.get("state") != "completed",
        }
        result.update(
            {
                "fixture": validation.as_dict(),
                "http_status": response.status_code,
                "error_code": _error_code(response),
                "counts_before": before,
                "counts_after": after,
                "machine_checks": checks,
                "status": "guard_passed" if all(checks.values()) else "guard_failed",
            }
        )
    except Exception as exc:
        result.update(
            {
                "status": "guard_failed",
                "stage": getattr(exc, "stage", stage),
                "error_type": type(exc).__name__,
                "detail": getattr(exc, "detail", "guard_failed"),
                "machine_checks": {},
            }
        )
        if validation is not None:
            result["fixture"] = validation.as_dict()
    result["model"] = {
        "lane": "not_run",
        "calls": 0,
        "guard_probe_invocations": probe.calls,
    }
    return result


async def _run_h10(row: dict[str, Any], work_root: Path) -> dict[str, Any]:
    result = _case_base(row, BOUNDARY_ASGI)
    result.update(
        {
            "score_scope": "engineering_only",
            "transport": "httpx_asgi_transport_in_process",
            "fixture_note": (
                "Cross-market read guard; the US order is a valid homewares fixture and "
                "materials are not required for this guard."
            ),
            "model": {"lane": "not_run", "calls": 0},
        }
    )
    stage = "setup"
    fixture = _fixture_for_row(row)
    required = _required_fixture_for_row(row)
    validation: _FixtureValidation | None = None
    try:
        case_root = work_root / "engineering" / "H10"
        case_root.mkdir(parents=True, exist_ok=True)
        settings = _settings_for(
            case_root / "app.db",
            category_map=_category_map_for_fixture(fixture),
        )
        async with _running_app(settings) as (app, container):
            tokens = await _seed_staff(
                container.session_factory,
                [_staff_spec(_STAFF_SUPPORT, "au"), _staff_spec(_STAFF_SUPPORT, "us")],
            )
            _configure_fake_fixture(container.business_connector, fixture)
            au_token = tokens["staff_support_au"]
            us_token = tokens["staff_support_us"]
            async with _asgi_client(app) as client:
                stage = "us_sync"
                us_sync = await client.post(
                    "/api/v1/after-sales/us/cases/sync", headers=_auth(us_token)
                )
                if us_sync.status_code != 200:
                    raise _GuardSetupError(stage, f"status_{us_sync.status_code}")
                us_detail = await _first_case_detail(client, "us", us_token)
                us_case_id = int(us_detail["case_id"])
                us_order_id = str(us_detail["order_id"])
                us_request_id = str(us_detail["request_id"])
                description = str(us_detail.get("description") or "")
                us_evidence = await _case_evidence_body(client, "us", us_case_id, us_token)
                validation = _validate_fixture(us_detail, us_evidence, required)
                if not validation.ok:
                    result.update(
                        {
                            "status": "guard_failed",
                            "stage": "fixture_validation",
                            "error_type": "FixtureValidationError",
                            "fixture": validation.as_dict(),
                            "machine_checks": {"fixture_valid": False},
                        }
                    )
                    return result
                stage = "au_cross_market_reads"
                case_response = await client.get(
                    f"/api/v1/after-sales/us/cases/{us_case_id}",
                    headers=_auth(au_token),
                )
                order_response = await client.get(
                    f"/api/v1/after-sales/us/orders/{us_order_id}",
                    headers=_auth(au_token),
                )
                sync_response = await client.post(
                    "/api/v1/after-sales/us/cases/sync", headers=_auth(au_token)
                )
        sensitive = {
            "order_id": us_order_id,
            "request_id": us_request_id,
        }
        if description:
            sensitive["description"] = description
        leaked: list[str] = []
        business_payload_exposed = False
        case_id_field_exposed = False
        for label, response in (
            ("case", case_response),
            ("order", order_response),
            ("sync", sync_response),
        ):
            text = response.text[:4096]
            body = _safe_body(response)
            for field_name, token in sensitive.items():
                if token and token in text:
                    leaked.append(f"{label}:{field_name}")
            if any(
                key in body for key in ("drafts", "approvals", "operations", "order", "description")
            ):
                business_payload_exposed = True
            if body.get("case_id") == us_case_id:
                case_id_field_exposed = True
        assert validation is not None
        checks = {
            "fixture_valid": validation.ok,
            "au_case_read_403": case_response.status_code == 403,
            "au_order_read_403": order_response.status_code == 403,
            "au_sync_403": sync_response.status_code == 403,
            "no_order_data_leak": not any(":order_id" in item for item in leaked),
            "no_request_data_leak": not any(":request_id" in item for item in leaked),
            "no_description_leak": not any(":description" in item for item in leaked),
            "no_case_payload_leak": not case_id_field_exposed and not business_payload_exposed,
            "no_model_call": True,
        }
        result.update(
            {
                "fixture": validation.as_dict(),
                "http_statuses": {
                    "case_read": case_response.status_code,
                    "order_read": order_response.status_code,
                    "sync": sync_response.status_code,
                },
                "error_codes": {
                    "case_read": _error_code(case_response),
                    "order_read": _error_code(order_response),
                    "sync": _error_code(sync_response),
                },
                "leaked_fields": leaked,
                "business_payload_exposed": business_payload_exposed,
                "case_id_field_exposed": case_id_field_exposed,
                "machine_checks": checks,
                "status": "guard_passed" if all(checks.values()) else "guard_failed",
            }
        )
    except Exception as exc:
        result.update(
            {
                "status": "guard_failed",
                "stage": getattr(exc, "stage", stage),
                "error_type": type(exc).__name__,
                "detail": getattr(exc, "detail", "guard_failed"),
                "machine_checks": {},
            }
        )
        if validation is not None:
            result["fixture"] = validation.as_dict()
    return result


async def _run_h11(row: dict[str, Any], work_root: Path) -> dict[str, Any]:
    result = _case_base(row, BOUNDARY_H11)
    result.update(
        {
            "score_scope": "engineering_only",
            "transport": "httpx_asgi_transport_in_process",
            "execution_transport": "in_process_execution_service",
            "fixture_note": (
                "In-process fixture with materials seeded through the staff API; execution "
                "uses the same configured connector."
            ),
            "model": {"lane": "not_run", "calls": 0},
        }
    )
    stage = "setup"
    fixture = _fixture_for_row(row)
    required = _required_fixture_for_row(row)
    validation: _FixtureValidation | None = None
    try:
        case_root = work_root / "engineering" / "H11"
        case_root.mkdir(parents=True, exist_ok=True)
        settings = _settings_for(
            case_root / "app.db",
            category_map=_category_map_for_fixture(fixture),
        )
        async with _running_app(settings) as (app, container):
            execution = container.after_sales_execution
            connector = container.business_connector
            if execution is None or connector is None:
                raise _GuardSetupError("container", "execution_unavailable")
            tokens = await _seed_staff(
                container.session_factory,
                [_staff_spec(_STAFF_SUPPORT, "au"), _staff_spec(_STAFF_APPROVER, "au")],
            )
            support = tokens["staff_support_au"]
            approver = tokens["staff_approver_au"]
            _configure_fake_fixture(connector, fixture)
            async with _asgi_client(app) as client:
                stage = "sync"
                sync = await client.post(
                    "/api/v1/after-sales/au/cases/sync", headers=_auth(support)
                )
                if sync.status_code != 200:
                    raise _GuardSetupError(stage, f"status_{sync.status_code}")
                detail = await _first_case_detail(client, "au", support)
                case_id = int(detail["case_id"])
                material_count, detail = await _seed_case_materials(
                    client, "au", case_id, support, fixture
                )
                evidence = await _case_evidence_body(client, "au", case_id, support)
                validation = _validate_fixture(detail, evidence, required)
                if not validation.ok:
                    result.update(
                        {
                            "status": "guard_failed",
                            "stage": "fixture_validation",
                            "error_type": "FixtureValidationError",
                            "fixture": validation.as_dict(),
                            "machine_checks": {"fixture_valid": False},
                        }
                    )
                    return result
                chunk_id = await _pick_evidence_chunk(
                    client, "au", case_id, support, "faults-missing"
                )
                stage = "draft"
                created = await client.post(
                    f"/api/v1/after-sales/au/cases/{case_id}/drafts",
                    headers=_auth(support),
                    json={
                        "expected_revision": detail["revision"],
                        "action": "reship",
                        "internal_summary": _DRAFT_SUMMARY,
                        "consumer_reply": _DRAFT_REPLY,
                        "evidence_chunk_ids": [chunk_id],
                    },
                )
                if created.status_code != 200:
                    raise _GuardSetupError(stage, f"status_{created.status_code}")
                detail = _safe_body(created)
                current = detail.get("current_draft") or {}
                stage = "submit"
                submitted = await client.post(
                    f"/api/v1/after-sales/au/cases/{case_id}/drafts/"
                    f"{current.get('version')}/submit",
                    headers=_auth(support),
                    json={"expected_revision": detail["revision"]},
                )
                if submitted.status_code != 200:
                    raise _GuardSetupError(stage, f"status_{submitted.status_code}")
                detail = _safe_body(submitted)
                approvals = detail.get("approvals") or []
                if not approvals:
                    raise _GuardSetupError(stage, "missing_approval")
                approval_id = str(approvals[-1]["approval_id"])
                stage = "approve"
                approved = await client.post(
                    f"/api/v1/after-sales/au/cases/{case_id}/approvals/{approval_id}/approve",
                    headers=_auth(approver),
                    json={"expected_revision": detail["revision"]},
                )
                if approved.status_code != 200:
                    raise _GuardSetupError(stage, f"status_{approved.status_code}")
                detail = _safe_body(approved)
                operations = detail.get("operations") or []
                if not operations:
                    raise _GuardSetupError(stage, "missing_prepared_operation")
                operation_before = operations[-1]
                original_key = str(operation_before["idempotency_key"])
                operation_id = str(operation_before["operation_id"])
                original_current = detail.get("current_draft") or {}
                original_current_id = str(original_current.get("draft_id") or "")
                original_draft_ids = {
                    str(item.get("draft_id")) for item in (detail.get("drafts") or [])
                }

                stage = "unknown_outcome_dispatch"
                create_before = _create_calls(container)
                cast(Any, connector).queue_failure(
                    "create_ticket", ConnectorUnavailable(create_operation=True)
                )
                run_report = await execution.run_once(["au"])
                create_after_dispatch = _create_calls(container)
                post_unknown = await _case_detail(client, "au", case_id, support)
                operation_after = next(
                    (
                        item
                        for item in (post_unknown.get("operations") or [])
                        if str(item.get("operation_id")) == operation_id
                    ),
                    None,
                )

                stage = "new_candidate"
                candidate_response = await client.post(
                    f"/api/v1/after-sales/au/cases/{case_id}/drafts",
                    headers=_auth(support),
                    json={
                        "expected_revision": post_unknown.get("revision", 1),
                        "action": "reship",
                        "internal_summary": "Synthetic candidate retained during unknown outcome.",
                        "consumer_reply": _DRAFT_REPLY,
                        "evidence_chunk_ids": [chunk_id],
                    },
                )
                candidate_detail = (
                    _safe_body(candidate_response) if candidate_response.status_code == 200 else {}
                )
                new_drafts = [
                    item
                    for item in (candidate_detail.get("drafts") or [])
                    if str(item.get("draft_id")) not in original_draft_ids
                ]
                candidate_version = new_drafts[-1].get("version") if new_drafts else None
                stage = "candidate_submit"
                submit_candidate = await client.post(
                    f"/api/v1/after-sales/au/cases/{case_id}/drafts/" f"{candidate_version}/submit",
                    headers=_auth(support),
                    json={"expected_revision": candidate_detail.get("revision", 1)},
                )
                create_after_candidate = _create_calls(container)
                final = await _case_detail(client, "au", case_id, support)

        final_operations = final.get("operations") or []
        final_operation = next(
            (item for item in final_operations if str(item.get("operation_id")) == operation_id),
            None,
        )
        final_approval = next(
            (
                item
                for item in (final.get("approvals") or [])
                if str(item.get("approval_id")) == approval_id
            ),
            None,
        )
        candidate_ok = candidate_response.status_code == 200 and bool(new_drafts)
        assert validation is not None
        checks = {
            "fixture_valid": validation.ok,
            "unknown_outcome_reconciliation_recorded": (
                run_report.reconciling + run_report.manual_review
            )
            >= 1,
            "case_reconciliation_required": post_unknown.get("state") == "reconciliation_required",
            "operation_reconciliation_required": bool(
                operation_after and operation_after.get("status") == "reconciliation_required"
            ),
            "original_key_retained": bool(
                final_operation and str(final_operation.get("idempotency_key")) == original_key
            ),
            "original_approval_retained": bool(
                final_approval and final_approval.get("status") in {"approved", "executing"}
            ),
            "only_one_operation": len(final_operations) == 1,
            "candidate_created": candidate_ok,
            "candidate_is_editing": bool(new_drafts and new_drafts[-1].get("status") == "editing"),
            "current_draft_unchanged": str((final.get("current_draft") or {}).get("draft_id") or "")
            == original_current_id,
            "new_submission_blocked": submit_candidate.status_code == 409,
            "dispatch_create_delta_one": create_after_dispatch - create_before == 1,
            "candidate_create_delta_zero": (create_after_candidate - create_after_dispatch == 0),
            "no_business_write_after_candidate": create_after_candidate == create_after_dispatch,
        }
        result.update(
            {
                "fixture": validation.as_dict(),
                "material_count": material_count,
                "run_report": {
                    "claimed": run_report.claimed,
                    "dispatched": run_report.dispatched,
                    "reconciling": run_report.reconciling,
                    "manual_review": run_report.manual_review,
                    "failed": run_report.failed,
                },
                "operation": {
                    "operation_id": operation_id,
                    "idempotency_key": original_key,
                    "status_before": operation_before.get("status"),
                    "status_after_unknown": (
                        operation_after.get("status") if operation_after else None
                    ),
                    "status_final": (final_operation.get("status") if final_operation else None),
                    "last_error_code": (
                        operation_after.get("last_error_code") if operation_after else None
                    ),
                },
                "approval_id": approval_id,
                "create_calls": {
                    "before": create_before,
                    "after_dispatch": create_after_dispatch,
                    "after_candidate": create_after_candidate,
                },
                "candidate_http_status": candidate_response.status_code,
                "candidate_submit_http_status": submit_candidate.status_code,
                "candidate_submit_error_code": _error_code(submit_candidate),
                "machine_checks": checks,
                "status": "guard_passed" if all(checks.values()) else "guard_failed",
            }
        )
    except Exception as exc:
        result.update(
            {
                "status": "guard_failed",
                "stage": getattr(exc, "stage", stage),
                "error_type": type(exc).__name__,
                "detail": getattr(exc, "detail", "guard_failed"),
                "machine_checks": {},
            }
        )
        if validation is not None:
            result["fixture"] = validation.as_dict()
    return result


# --------------------------------------------------------------- OS restart H12


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _child_env(db_path: Path, category_map: str) -> dict[str, str]:
    """Allowlisted environment for owned API/worker children.

    Parent credentials, DTC_EVAL_* values and business/database configuration
    are never inherited. Only a fixed app/testing environment is passed.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "APP_ENV": "testing",
            "DATABASE_URL": _db_url(db_path),
            "KNOWLEDGE_DIR": str(KNOWLEDGE_DIR),
            "AFTER_SALES_ENABLED": "true",
            "BUSINESS_CONNECTOR": "fake",
            "AFTER_SALES_SKU_CATEGORY_MAP": category_map,
            "AFTER_SALES_WORKER_MARKETS": "au",
            "AFTER_SALES_WORKER_RETRY_BASE_SECONDS": "0.1",
            "AFTER_SALES_WORKER_RETRY_MAX_SECONDS": "1.0",
            "DEEPSEEK_API_KEY": "",
            "DEEPSEEK_BASE_URL": "",
            "EMBEDDING_API_KEY": "",
            "EMBEDDING_BASE_URL": "",
            "LOCAL_EMBEDDING_ENABLED": "false",
            "LOCAL_EMBEDDING_FORCE": "false",
            "CROSS_ENCODER_ENABLED": "false",
            "CROSS_ENCODER_FORCE": "false",
            "UNSTRUCTURED_ENABLED": "false",
            "UNSTRUCTURED_FORCE": "false",
            "MCP_ENABLED": "false",
            "MCP_REMOTE_URL": "",
            "REDIS_URL": "",
            "QDRANT_URL": "",
            "LOG_LEVEL": "WARNING",
            "RATE_LIMIT_PER_MINUTE": "1000",
        }
    )
    return env


class _OwnedProcess:
    """One owned child process and its exact process group."""

    def __init__(self, cmd: list[str], env: dict[str, str], *, role: str) -> None:
        self.role = role
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        self.pid = self.proc.pid
        try:
            self.pgid: int | None = os.getpgid(self.pid)
        except OSError:
            self.pgid = None
        if self.pgid != self.pid:
            self._kill_ungrouped()
            raise RuntimeError("child process did not create its own process group")
        self.base_url = ""

    def _kill_ungrouped(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def poll(self) -> int | None:
        return self.proc.poll()

    def wait(self, timeout: float) -> int:
        return int(self.proc.wait(timeout=timeout))

    def stop(self, timeout: float = 10.0) -> dict[str, Any]:
        if self.proc.poll() is not None:
            return {
                "role": self.role,
                "pid": self.pid,
                "pgid": self.pgid,
                "returncode": self.proc.returncode,
                "already_exited": True,
                "group_alive": False,
            }
        if self.pgid != self.pid:
            self._kill_ungrouped()
            raise RuntimeError("refusing to signal an unknown process group")
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=timeout)
        try:
            os.killpg(self.pid, 0)
            group_alive = True
        except ProcessLookupError:
            group_alive = False
        return {
            "role": self.role,
            "pid": self.pid,
            "pgid": self.pgid,
            "returncode": self.proc.returncode,
            "already_exited": False,
            "group_alive": group_alive,
        }


def _http_json(
    method: str,
    url: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = int(exc.code)
    try:
        decoded = json.loads(body) if body else {}
    except (TypeError, ValueError):
        decoded = {}
    return status, decoded if isinstance(decoded, dict) else {}


def _require_status(status: int, expected: int, stage: str) -> dict[str, Any]:
    if status != expected:
        raise _GuardSetupError(stage, f"status_{status}")
    return {}


def _wait_http(url: str, process: _OwnedProcess, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise _GuardSetupError("api_startup", f"exited_{returncode}")
        try:
            status, _body = _http_json("GET", url, timeout=2.0)
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise _GuardSetupError("api_startup", "timeout")


def _start_api(env: dict[str, str], db_path: Path) -> _OwnedProcess:
    port = _free_port()
    process = _OwnedProcess(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.asgi:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        env,
        role="api",
    )
    process.base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_http(f"{process.base_url}/api/v1/health", process)
    except BaseException:
        process.stop()
        raise
    return process


def _start_worker(env: dict[str, str]) -> _OwnedProcess:
    return _OwnedProcess(
        [sys.executable, "-m", "app.worker", "--once", "--markets", "au"],
        env,
        role="worker",
    )


async def _read_cursor(db_path: Path, market: str) -> dict[str, Any]:
    engine = create_async_engine(_db_url(db_path))
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            row = await session.get(AfterSalesSyncCursorRow, market)
            if row is None:
                return {"exists": False, "cursor": None, "high_watermark": None, "revision": None}
            return {
                "exists": True,
                "cursor": row.cursor,
                "high_watermark": row.high_watermark,
                "revision": row.revision,
            }
    finally:
        await engine.dispose()


async def _read_case_db_state(db_path: Path, case_id: int) -> dict[str, Any]:
    engine = create_async_engine(_db_url(db_path))
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            case = await session.get(AfterSalesCaseRow, case_id)
            approvals = list(
                await session.scalars(
                    select(AfterSalesApprovalRow)
                    .where(AfterSalesApprovalRow.case_id == case_id)
                    .order_by(AfterSalesApprovalRow.created_at)
                )
            )
            drafts = list(
                await session.scalars(
                    select(AfterSalesDraftRow)
                    .where(AfterSalesDraftRow.case_id == case_id)
                    .order_by(AfterSalesDraftRow.version)
                )
            )
            operations = list(
                await session.scalars(
                    select(AfterSalesOperationRow)
                    .where(AfterSalesOperationRow.case_id == case_id)
                    .order_by(AfterSalesOperationRow.created_at)
                )
            )
            return {
                "case": (
                    {
                        "state": case.state,
                        "request_id": case.request_id,
                        "revision": case.revision,
                        "current_draft_id": case.current_draft_id,
                    }
                    if case is not None
                    else None
                ),
                "approvals": [
                    {
                        "approval_id": row.approval_id,
                        "status": row.status,
                        "requester_staff_id": row.requester_staff_id,
                        "decided_by_staff_id": row.decided_by_staff_id,
                    }
                    for row in approvals
                ],
                "drafts": [
                    {
                        "draft_id": row.draft_id,
                        "version": row.version,
                        "status": row.status,
                    }
                    for row in drafts
                ],
                "operations": [
                    {
                        "operation_id": row.operation_id,
                        "status": row.status,
                        "attempt": row.attempt,
                        "idempotency_key": row.idempotency_key,
                        "receipt_present": bool(row.receipt_json),
                        "last_error_code": row.last_error_code,
                    }
                    for row in operations
                ],
            }
    finally:
        await engine.dispose()


def _seed_materials_http(
    base: str,
    case_id: int,
    token: str,
    fixture: _SyntheticFixture,
) -> tuple[int, dict[str, Any]]:
    status, detail = _http_json("GET", f"{base}/after-sales/au/cases/{case_id}", token=token)
    _require_status(status, 200, "materials")
    for payload in _material_payloads(fixture):
        status, detail = _http_json(
            "POST",
            f"{base}/after-sales/au/cases/{case_id}/materials",
            token=token,
            payload={**payload, "expected_revision": detail["revision"]},
        )
        _require_status(status, 200, "materials")
    return len(fixture.materials), detail


def _run_h12(row: dict[str, Any], work_root: Path) -> dict[str, Any]:
    result = _case_base(row, BOUNDARY_H12)
    result.update(
        {
            "score_scope": "engineering_only",
            "transport": "uvicorn_child_loopback_tcp",
            "execution_transport": "worker_child_process",
            "fixture_note": (
                "API children use the FakeConnector default AU homewares request; the row "
                "query is the restart scenario, not request text. Category and materials are "
                "API-seeded/validated."
            ),
            "model": {"lane": "not_run", "calls": 0},
        }
    )
    case_root = work_root / "engineering" / "H12"
    case_root.mkdir(parents=True, exist_ok=True)
    db_path = case_root / "app.db"
    fixture = _fixture_for_row(row)
    required = _required_fixture_for_row(row)
    validation: _FixtureValidation | None = None
    env = _child_env(db_path, _category_map_for_fixture(fixture))
    processes: list[_OwnedProcess] = []
    stage = "setup"
    data: dict[str, Any] = {}
    try:
        tokens = _seed_staff_at_path(
            db_path,
            [_staff_spec(_STAFF_SUPPORT, "au"), _staff_spec(_STAFF_APPROVER, "au")],
        )
        support = tokens["staff_support_au"]
        approver = tokens["staff_approver_au"]
        stage = "api_start"
        api_first = _start_api(env, db_path)
        processes.append(api_first)
        first_pid = api_first.pid
        first_pgid = api_first.pgid
        base = f"{api_first.base_url}/api/v1"

        stage = "sync"
        status, _ = _http_json("POST", f"{base}/after-sales/au/cases/sync", token=support)
        _require_status(status, 200, stage)
        status, cases = _http_json("GET", f"{base}/after-sales/au/cases", token=support)
        _require_status(status, 200, stage)
        items = cases.get("items") or []
        if not items:
            raise _GuardSetupError(stage, "no_cases")
        case_id = int(items[0]["case_id"])
        status, detail = _http_json("GET", f"{base}/after-sales/au/cases/{case_id}", token=support)
        _require_status(status, 200, stage)
        material_count, detail = _seed_materials_http(base, case_id, support, fixture)
        status, evidence = _http_json(
            "GET", f"{base}/after-sales/au/cases/{case_id}/evidence", token=support
        )
        _require_status(status, 200, stage)
        validation = _validate_fixture(detail, evidence, required)
        data["fixture"] = validation.as_dict()
        data["material_count"] = material_count
        if not validation.ok:
            raise _GuardSetupError("fixture_validation", "fixture_invalid")
        chunk_id = ""
        for chunk in evidence.get("chunks") or []:
            if (
                isinstance(chunk, dict)
                and Path(str(chunk.get("source_path") or "")).stem == "faults-missing"
                and isinstance(chunk.get("chunk_id"), str)
            ):
                chunk_id = str(chunk["chunk_id"])
                break
        if not chunk_id:
            chunks = evidence.get("chunks") or []
            if chunks and isinstance(chunks[0].get("chunk_id"), str):
                chunk_id = str(chunks[0]["chunk_id"])
        if not chunk_id:
            raise _GuardSetupError(stage, "missing_chunk")

        stage = "draft"
        status, detail = _http_json(
            "POST",
            f"{base}/after-sales/au/cases/{case_id}/drafts",
            token=support,
            payload={
                "expected_revision": detail["revision"],
                "action": "reship",
                "internal_summary": _DRAFT_SUMMARY,
                "consumer_reply": _DRAFT_REPLY,
                "evidence_chunk_ids": [chunk_id],
            },
        )
        _require_status(status, 200, stage)
        current = detail.get("current_draft") or {}
        stage = "submit"
        status, detail = _http_json(
            "POST",
            f"{base}/after-sales/au/cases/{case_id}/drafts/{current.get('version')}/submit",
            token=support,
            payload={"expected_revision": detail["revision"]},
        )
        _require_status(status, 200, stage)
        approvals = detail.get("approvals") or []
        if not approvals:
            raise _GuardSetupError(stage, "missing_approval")
        approval = approvals[-1]
        approval_id = str(approval["approval_id"])
        pre = {
            "case_id": case_id,
            "request_id": str(detail["request_id"]),
            "case_revision": int(detail["revision"]),
            "draft_id": str(current.get("draft_id") or ""),
            "draft_version": current.get("version"),
            "draft_status": current.get("status"),
            "approval_id": approval_id,
            "approval_status": approval.get("status"),
            "requester_staff_id": approval.get("requester_staff_id"),
        }
        stage = "api_stop"
        first_stop = api_first.stop()
        processes.remove(api_first)
        cursor_pre = asyncio.run(_read_cursor(db_path, "au"))

        stage = "api_restart"
        api_second = _start_api(env, db_path)
        processes.append(api_second)
        second_pid = api_second.pid
        second_pgid = api_second.pgid
        base = f"{api_second.base_url}/api/v1"
        stage = "persisted_read"
        status, restored = _http_json(
            "GET", f"{base}/after-sales/au/cases/{case_id}", token=support
        )
        _require_status(status, 200, stage)
        restored_current = restored.get("current_draft") or {}
        restored_approvals = restored.get("approvals") or []
        restored_approval = next(
            (item for item in restored_approvals if str(item.get("approval_id")) == approval_id),
            None,
        )
        stage = "distinct_approver"
        status, approved = _http_json(
            "POST",
            f"{base}/after-sales/au/cases/{case_id}/approvals/{approval_id}/approve",
            token=approver,
            payload={"expected_revision": restored.get("revision", 1)},
        )
        _require_status(status, 200, stage)
        approved_approvals = approved.get("approvals") or []
        approved_approval = next(
            (item for item in approved_approvals if str(item.get("approval_id")) == approval_id),
            None,
        )
        prepared_operations = approved.get("operations") or []
        if not prepared_operations:
            raise _GuardSetupError(stage, "missing_prepared_operation")
        prepared_key = str(prepared_operations[-1].get("idempotency_key") or "")

        stage = "api_stop_after_approval"
        second_stop = api_second.stop()
        processes.remove(api_second)
        cursor_after_restart = asyncio.run(_read_cursor(db_path, "au"))

        stage = "worker"
        worker = _start_worker(env)
        processes.append(worker)
        worker_pid = worker.pid
        worker_returncode: int | None = None
        try:
            worker_returncode = worker.wait(90.0)
        finally:
            worker_stop = worker.stop()
            processes.remove(worker)
        cursor_final = asyncio.run(_read_cursor(db_path, "au"))
        final = asyncio.run(_read_case_db_state(db_path, case_id))

        final_case = final.get("case") or {}
        final_approvals = final.get("approvals") or []
        final_drafts = final.get("drafts") or []
        final_operations = final.get("operations") or []
        approval_final = next(
            (item for item in final_approvals if item.get("approval_id") == approval_id),
            None,
        )
        draft_final = next(
            (item for item in final_drafts if item.get("draft_id") == pre["draft_id"]),
            None,
        )
        operation_final = final_operations[0] if final_operations else None
        assert validation is not None
        checks = {
            "fixture_valid": validation.ok,
            "api_restarted_new_pid": second_pid != first_pid,
            "api_new_process_group": second_pgid != first_pgid,
            "draft_persisted": bool(
                restored_current
                and str(restored_current.get("draft_id")) == pre["draft_id"]
                and restored_current.get("version") == pre["draft_version"]
            ),
            "approval_persisted": bool(
                restored_approval
                and restored_approval.get("status") == "pending"
                and str(restored_approval.get("approval_id")) == approval_id
            ),
            "cursor_persisted": (
                cursor_pre.get("cursor") == cursor_after_restart.get("cursor")
                and cursor_pre.get("high_watermark") == cursor_after_restart.get("high_watermark")
            ),
            "distinct_approver": bool(
                approved_approval
                and approved_approval.get("decided_by_staff_id")
                and approved_approval.get("decided_by_staff_id") != pre["requester_staff_id"]
            ),
            "operation_prepared": bool(prepared_operations),
            "worker_exit_zero": worker_returncode == 0,
            "case_completed": final_case.get("state") == "completed",
            "request_preserved": final_case.get("request_id") == pre["request_id"],
            "one_operation": len(final_operations) == 1,
            "operation_succeeded": bool(
                operation_final and operation_final.get("status") == "succeeded"
            ),
            "operation_attempt_one": bool(operation_final and operation_final.get("attempt") == 1),
            "receipt_present": bool(operation_final and operation_final.get("receipt_present")),
            "operation_key_preserved": bool(
                operation_final and str(operation_final.get("idempotency_key")) == prepared_key
            ),
            "only_original_approval": bool(
                len(final_approvals) == 1
                and approval_final
                and approval_final.get("approval_id") == approval_id
            ),
            "approval_consumed": bool(
                approval_final and approval_final.get("status") == "consumed"
            ),
            "draft_consumed": bool(draft_final and draft_final.get("status") == "consumed"),
        }
        data.update(
            {
                "fixture": validation.as_dict(),
                "material_count": material_count,
                "restart": {
                    "method": "uvicorn_subprocess_sigterm_restart",
                    "first_pid": first_pid,
                    "first_pgid": first_pgid,
                    "second_pid": second_pid,
                    "second_pgid": second_pgid,
                    "worker_pid": worker_pid,
                    "first_stop": first_stop,
                    "second_stop": second_stop,
                    "worker_stop": worker_stop,
                },
                "persisted": {
                    "before": pre,
                    "after_restart": {
                        "draft_id": restored_current.get("draft_id"),
                        "draft_version": restored_current.get("version"),
                        "approval_id": (
                            restored_approval.get("approval_id") if restored_approval else None
                        ),
                        "approval_status": (
                            restored_approval.get("status") if restored_approval else None
                        ),
                        "case_revision": restored.get("revision"),
                    },
                    "cursor_before_restart": cursor_pre,
                    "cursor_after_restart": cursor_after_restart,
                    "cursor_after_worker": cursor_final,
                },
                "approval": {
                    "approval_id": approval_id,
                    "requester_staff_id": pre["requester_staff_id"],
                    "decided_by_staff_id": (
                        approved_approval.get("decided_by_staff_id") if approved_approval else None
                    ),
                },
                "execution": {
                    "worker_returncode": worker_returncode,
                    "case": final_case,
                    "approvals": final_approvals,
                    "drafts": final_drafts,
                    "operations": final_operations,
                },
                "machine_checks": checks,
                "status": "guard_passed" if all(checks.values()) else "guard_failed",
            }
        )
        data["processes"] = [
            first_stop,
            second_stop,
            worker_stop,
        ]
    except Exception as exc:
        data.update(
            {
                "status": "guard_failed",
                "stage": getattr(exc, "stage", stage),
                "error_type": type(exc).__name__,
                "detail": getattr(exc, "detail", "guard_failed"),
                "machine_checks": {},
            }
        )
        if validation is not None:
            data["fixture"] = validation.as_dict()
    finally:
        cleanup_errors: list[str] = []
        stop_records: list[dict[str, Any]] = []
        for process in processes:
            try:
                stop_records.append(process.stop())
            except Exception as exc:
                cleanup_errors.append(f"{process.role}:{type(exc).__name__}")
        data["process_cleanup"] = {
            "errors": cleanup_errors,
            "all_stopped": not cleanup_errors,
            "stops": stop_records,
        }
        if cleanup_errors:
            data["status"] = "guard_failed"
    result.update(data)
    return result


# ------------------------------------------------------------------- execution


def _not_run_model_cases(rows: list[dict[str, Any]], reason: str) -> list[dict[str, Any]]:
    return [
        _not_run(
            row,
            reason,
            BOUNDARY_POLICY if row["partition"] == "policy_graph" else BOUNDARY_ASGI,
        )
        for row in rows
        if row["partition"] in MODEL_PARTITIONS
    ]


def _run_engineering_guards(rows: list[dict[str, Any]], work_root: Path) -> list[dict[str, Any]]:
    engineering = [row for row in rows if row["partition"] == "engineering_guard"]
    results = asyncio.run(_run_engineering_async(engineering, work_root))
    for row in engineering:
        if row["id"] == "H12":
            results.append(_run_h12(row, work_root))
    return results


async def _run_engineering_async(
    rows: list[dict[str, Any]], work_root: Path
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in rows:
        if row["id"] == "H09":
            results.append(await _run_h09(row, work_root))
        elif row["id"] == "H10":
            results.append(await _run_h10(row, work_root))
        elif row["id"] == "H11":
            results.append(await _run_h11(row, work_root))
    return results


def _aggregate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    engineering = [case for case in cases if case["partition"] == "engineering_guard"]
    model_cases = [case for case in cases if case["partition"] in MODEL_PARTITIONS]
    eng_counts = Counter(str(case.get("status")) for case in engineering)
    model_counts = Counter(str(case.get("status")) for case in model_cases)
    records: list[dict[str, Any]] = []
    for case in model_cases:
        model = case.get("model")
        if isinstance(model, dict):
            records.extend(
                record for record in (model.get("records") or []) if isinstance(record, dict)
            )
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    models: set[str] = set()
    provider_calls = 0
    successful_calls = 0
    provider_errors = 0
    budget_blocked = 0
    json_invalid = 0
    truncation = 0
    for record in records:
        if record.get("status") == "ok":
            successful_calls += 1
            raw_usage = record.get("usage") or {}
            for key in usage:
                usage[key] += int(raw_usage.get(key) or 0)
            if record.get("model"):
                models.add(str(record["model"]))
            if record.get("json_valid") is False:
                json_invalid += 1
            if record.get("completion_at_cap") is True:
                truncation += 1
        elif record.get("error_type") == "ModelBudgetExceeded":
            budget_blocked += 1
        else:
            provider_errors += 1
        if record.get("error_type") != "ModelBudgetExceeded":
            provider_calls += 1
    not_run_reasons = sorted(
        {
            str(case.get("reason"))
            for case in model_cases
            if case.get("status") == "not_run" and case.get("reason")
        }
    )
    stop_reason = next(
        (
            reason
            for reason in (
                "global_model_budget_exhausted",
                "provider_failure_stop",
                "model_lane_setup_failed",
                "fixture_invalid_stop",
            )
            if reason in not_run_reasons
            or any(case.get("status") == "budget_exhausted" for case in model_cases)
            or any(case.get("status") == "fixture_invalid" for case in model_cases)
            or any(
                case.get("error_type") in {"LLMProviderError", "ModelBudgetExceeded"}
                for case in model_cases
            )
        ),
        None,
    )
    return {
        "engineering_guards": {
            "total": len(engineering),
            "guard_passed": eng_counts.get("guard_passed", 0),
            "guard_failed": eng_counts.get("guard_failed", 0),
            "not_run": eng_counts.get("not_run", 0),
        },
        "model_cases": {
            "total": len(model_cases),
            "model_case_observed": model_counts.get("model_case_observed", 0),
            "model_case_guard_failed": model_counts.get("model_case_guard_failed", 0),
            "model_case_failed_closed": model_counts.get("model_case_failed_closed", 0),
            "model_case_failed": model_counts.get("model_case_failed", 0),
            "model_case_unexpected_status": model_counts.get("model_case_unexpected_status", 0),
            "model_case_no_calls": model_counts.get("model_case_no_calls", 0),
            "budget_exhausted": model_counts.get("budget_exhausted", 0),
            "fixture_invalid": model_counts.get("fixture_invalid", 0),
            "not_run": model_counts.get("not_run", 0),
        },
        "model_calls": {
            "actual_provider_calls": provider_calls,
            "successful_calls": successful_calls,
            "provider_error_calls": provider_errors,
            "budget_blocked_calls": budget_blocked,
            "max_calls": MAX_MODEL_CALLS,
            "usage": usage,
            "models_actual": sorted(models),
            "json_invalid_calls": json_invalid,
            "possible_truncation_calls": truncation,
            "not_run_reasons": not_run_reasons,
            "stop_reason": stop_reason,
        },
        "no_combined_llm_score": True,
        "manual_review": {
            "status": "pending_codex",
            "axes": list(_MANUAL_AXES),
            "scale": [0, 1, 2],
            "note": "Machine checks and same-provider faithfulness are not manual verdicts.",
        },
    }


def _base_report(
    mode: Literal["offline", "real"],
    artifacts: FrozenArtifacts,
    *,
    model_public: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runner": "eval.after_sales_acceptance",
        "status": "running",
        "mode": mode,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "duration_ms": None,
        "freeze_verification": artifacts.verification(),
        "provenance": artifacts.provenance(),
        "data": artifacts.data_meta(),
        "model": model_public,
        "limits": {
            "max_cases": MAX_DATASET_CASES,
            "max_calls": MAX_MODEL_CALLS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "cumulative_fee_cap": None,
            "note": "Call/token bounds are eval guards, not billing guarantees.",
        },
        "boundaries": {
            "policy_graph": (
                "in_process_chat_graph: real ChatService/LangGraph via direct asyncio; "
                "no ASGI or TCP"
            ),
            "staff_suggestion": (
                "asgi_in_process_http: real FastAPI routing through httpx ASGITransport; " "no TCP"
            ),
            "engineering_guards": {
                "H09_H10_H11": "asgi_in_process_http",
                "H11_execution": "in_process_execution_service",
                "H12": (
                    "tcp_os_restart: real uvicorn child on 127.0.0.1 over TCP, "
                    "SIGTERM stop/start with new PID/PGID, then a real worker child process"
                ),
            },
        },
        "cases": [],
        "summary": {},
        "cleanup": {},
        "notes": [
            "Synthetic-only fixtures; no real customer data is used.",
            "H01-H06 use the real ChatService/LangGraph with the actual BM25/lexical retriever and "
            "hash embeddings; this is not evidence of real embedding quality.",
            "Faithfulness is same-provider automatic evidence, not an independent manual verdict.",
            "Engineering guards are separate engineering evidence and are not merged into an LLM score.",
            "Model cases are observations; semantic 0/1/2 grading is pending Codex.",
            "Zero model calls are never counted as positive generation; not_run and failed cases "
            "are not passed.",
            "Real-mode credentials are read only from the dedicated 0600 .env.eval.local; values "
            "are never logged or reported.",
        ],
    }


def _write_report(report: dict[str, Any], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"after_sales_acceptance_{stamp}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path


def _cleanup_work_root(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        shutil.rmtree(path)
    except Exception as exc:
        errors.append(type(exc).__name__)
    return {
        "temp_work_dir_removed": not path.exists(),
        "errors": errors,
    }


def _exit_code(report: dict[str, Any]) -> int:
    if report.get("status") != "completed":
        return 2
    summary = report.get("summary") or {}
    engineering = summary.get("engineering_guards") or {}
    model_calls = summary.get("model_calls") or {}
    if engineering.get("guard_failed", 0) or engineering.get("not_run", 0):
        return 1
    if model_calls.get("budget_blocked_calls", 0):
        return 1
    if model_calls.get("stop_reason") in {
        "provider_failure_stop",
        "model_lane_setup_failed",
        "fixture_invalid_stop",
    }:
        return 1
    if (summary.get("model_cases") or {}).get("fixture_invalid", 0):
        return 1
    if (report.get("cleanup") or {}).get("errors"):
        return 1
    return 0


def run_acceptance(
    *,
    mode: Literal["offline", "real"],
    real_config: RealModelConfig | None = None,
    report_dir: Path = REPORT_DIR,
    artifacts: FrozenArtifacts | None = None,
) -> RunOutcome:
    """Run the frozen holdout and always write a scrubbed report.

    Freeze verification happens before any provider construction. The runner
    creates and removes its own temporary directory and never accepts a caller
    database URL.
    """
    if mode not in {"offline", "real"}:
        raise ConfigurationError("mode must be offline or real")
    if mode == "real" and real_config is None:
        raise ConfigurationError("real mode requires an explicit RealModelConfig")
    if mode == "offline" and real_config is not None:
        raise ConfigurationError("offline mode does not accept a model config")

    artifacts = artifacts or verify_frozen()
    if mode == "real":
        assert real_config is not None
        model_public: dict[str, Any] = {
            "lane": "real",
            "provider": real_config.provider,
            "requested_model": real_config.model,
            "thinking_mode": real_config.thinking_mode,
            "base_url": real_config.base_url,
            "max_cases": real_config.max_cases,
            "max_calls": real_config.max_calls,
            "max_output_tokens": real_config.max_output_tokens,
            "credential_source": "dedicated .env.eval.local (0600), DTC_EVAL_* keys only",
        }
    else:
        model_public = {
            "lane": "not_run",
            "reason": "offline_model_not_requested",
            "calls": 0,
            "credential_source": "not_read",
        }

    work_root = Path(tempfile.mkdtemp(prefix="dtc_after_sales_acceptance_"))
    started = time.perf_counter()
    report = _base_report(mode, artifacts, model_public=model_public)
    try:
        guard_results = _run_engineering_guards(artifacts.rows, work_root)
        if mode == "real":
            assert real_config is not None
            model_results = asyncio.run(
                _run_real_model_cases(artifacts.rows, real_config, work_root)
            )
        else:
            model_results = _not_run_model_cases(artifacts.rows, "offline_model_not_requested")
        results_by_id = {result["id"]: result for result in [*guard_results, *model_results]}
        report["cases"] = [
            results_by_id.get(row["id"], _not_run(row, "handler_missing", "unknown"))
            for row in artifacts.rows
        ]
        report["status"] = "completed"
        report["summary"] = _aggregate_cases(report["cases"])
        if mode == "real":
            calls = report["summary"]["model_calls"]
            report["model"]["actual"] = {
                "models": calls["models_actual"],
                "usage": calls["usage"],
                "actual_provider_calls": calls["actual_provider_calls"],
                "stop_reason": calls["stop_reason"],
            }
    except Exception as exc:
        report["status"] = "aborted"
        report["error_type"] = type(exc).__name__
        report["summary"] = {
            "engineering_guards": {},
            "model_cases": {},
            "model_calls": {},
            "no_combined_llm_score": True,
            "manual_review": {"status": "pending_codex", "axes": list(_MANUAL_AXES)},
        }
    finally:
        report["cleanup"] = _cleanup_work_root(work_root)
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        report_path = _write_report(report, report_dir)
    return RunOutcome(report=report, report_path=report_path, exit_code=_exit_code(report))


def _print_summary(outcome: RunOutcome) -> None:
    report = outcome.report
    summary = report.get("summary") or {}
    engineering = summary.get("engineering_guards") or {}
    model_cases = summary.get("model_cases") or {}
    calls = summary.get("model_calls") or {}
    print(f"after-sales acceptance: status={report.get('status')} mode={report.get('mode')}")
    print(f"report: {outcome.report_path}")
    if report.get("freeze_verification", {}).get("status") == "verified":
        freeze = report["freeze_verification"]
        print(
            "freeze: verified "
            f"{freeze.get('freeze_count')} files revision={freeze.get('freeze_revision')} "
            f"dataset={str(freeze.get('dataset_sha256'))[:12]}"
        )
    if engineering:
        print(
            "engineering_guards: "
            f"passed={engineering.get('guard_passed', 0)} "
            f"failed={engineering.get('guard_failed', 0)} "
            f"not_run={engineering.get('not_run', 0)}"
        )
    if model_cases:
        print(
            "model_cases: "
            f"observed={model_cases.get('model_case_observed', 0)} "
            f"failed_closed={model_cases.get('model_case_failed_closed', 0)} "
            f"failed={model_cases.get('model_case_failed', 0)} "
            f"guard_failed={model_cases.get('model_case_guard_failed', 0)} "
            f"fixture_invalid={model_cases.get('fixture_invalid', 0)} "
            f"not_run={model_cases.get('not_run', 0)}"
        )
    if calls:
        usage = calls.get("usage") or {}
        print(
            "model_calls: "
            f"actual={calls.get('actual_provider_calls', 0)}/"
            f"{calls.get('max_calls', MAX_MODEL_CALLS)} "
            f"prompt={usage.get('prompt_tokens', 0)} "
            f"completion={usage.get('completion_tokens', 0)} "
            f"total={usage.get('total_tokens', 0)} "
            f"stop={calls.get('stop_reason')}"
        )
    print("manual_review: pending Codex 0/1/2 per axis; no final acceptance claim")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.after_sales_acceptance",
        description="Sealed after-sales holdout acceptance runner",
    )
    parser.add_argument(
        "--allow-real-model",
        action="store_true",
        help="explicitly allow real provider calls; credentials come only from .env.eval.local",
    )
    parser.add_argument("--provider", default=None, help="real provider (deepseek)")
    parser.add_argument("--model", default=None, help="real model identifier")
    parser.add_argument(
        "--max-cases",
        type=int,
        default=None,
        help=f"must be {MAX_DATASET_CASES} for the sealed holdout",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help=f"global eval call guard (1..{MAX_MODEL_CALLS})",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=MAX_OUTPUT_TOKENS,
        help=f"per-call output token cap (1..{MAX_OUTPUT_TOKENS})",
    )
    parser.add_argument(
        "--thinking-mode",
        default="disabled",
        choices=["disabled"],
        help="real runs use thinking disabled",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=REPORT_DIR,
        help="ignored report directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        artifacts = verify_frozen()
    except FreezeVerificationError as exc:
        report = {
            "schema_version": 1,
            "runner": "eval.after_sales_acceptance",
            "status": "freeze_mismatch",
            "mode": "real" if args.allow_real_model else "offline",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "cases": [],
            "cleanup": {},
            "notes": ["No provider was constructed and no paid call was made."],
        }
        path = _write_report(report, args.report_dir)
        print(f"after-sales acceptance: status=freeze_mismatch report={path}")
        return 2

    if args.allow_real_model:
        try:
            config = real_config_from_explicit_options(
                allow_real_model=args.allow_real_model,
                provider=args.provider,
                model=args.model,
                max_cases=args.max_cases,
                max_calls=args.max_calls,
                max_output_tokens=args.max_output_tokens,
                thinking_mode=args.thinking_mode,
            )
        except ConfigurationError as exc:
            report = {
                "schema_version": 1,
                "runner": "eval.after_sales_acceptance",
                "status": "configuration_error",
                "mode": "real",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "cases": [],
                "cleanup": {},
                "notes": ["No provider was constructed and no paid call was made."],
            }
            path = _write_report(report, args.report_dir)
            print(f"after-sales acceptance: status=configuration_error report={path}")
            return 2
        outcome = run_acceptance(
            mode="real",
            real_config=config,
            report_dir=args.report_dir,
            artifacts=artifacts,
        )
    else:
        outcome = run_acceptance(
            mode="offline",
            report_dir=args.report_dir,
            artifacts=artifacts,
        )
    _print_summary(outcome)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
