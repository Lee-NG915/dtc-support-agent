"""Offline-first evaluation harness.

Default tasks are deterministic and offline:

    python -m eval.runner --task all
    python -m eval.runner --task intent
    python -m eval.runner --task retrieval
    python -m eval.runner --task epig-policy
    python -m eval.runner --task generation
    python -m eval.runner --task e2e

They construct ``Settings(_env_file=None)`` with empty provider keys, the
offline LLM, hash embeddings, lexical reranking, no MCP/Redis/Qdrant/local
models, a fake business connector and a fresh temporary SQLite database for
graph tasks. A developer ``.env`` or production environment cannot turn a
default run into a paid or networked evaluation.

Real model calls are a separate, explicit path:

    --allow-real-model --provider deepseek --model <model> \
    --max-cases <n> --max-calls <n> [--max-output-tokens <n>]

Credentials are read only from ``DTC_EVAL_DEEPSEEK_API_KEY`` (and optionally
``DTC_EVAL_DEEPSEEK_BASE_URL``) on that path. The call/token limits are
enforced eval guards, not billing guarantees.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr

from app.config.settings import Settings
from eval.metrics import (
    accuracy,
    classification_report,
    hit_at_k,
    mrr,
    percentile,
    precision_at_k,
    recall_at_k,
)
from eval.tool_trace import ToolTrace, compare_tool_expectation

ROOT = Path(__file__).resolve().parents[1]
INTENT_PATH = ROOT / "eval/dataset/intent_100.jsonl"
RAG_PATH = ROOT / "eval/dataset/rag_eval.jsonl"
EPIG_POLICY_PATH = ROOT / "eval/dataset/epig_policy_eval.jsonl"
E2E_PATH = ROOT / "eval/dataset/e2e_eval.jsonl"
REPORT_DIR = ROOT / "eval/reports"
KNOWLEDGE_DIR = ROOT / "knowledge"
EPIG_DEMO_DISCLOSURE = "内部自编演示政策，非官方或生产批准"

RULES_VERSION = "intent_rules_v1"
OVERLAY_VERSION = "intent_overlay_v1"
ANSWER_PROMPT_VERSION = "answer_v1"
FAITHFULNESS_PROMPT_VERSION = "faithfulness_v1"
RETRIEVAL_VERSION = "offline_hash_bm25_lexical_v1"
EPIG_POLICY_VERSION = "epig-policy-v1"

Mode = Literal["offline", "fake", "real"]
_LLM_TASKS = {"all", "intent", "generation", "e2e", "epig-policy"}

_SECRET_URL_RE = re.compile(r"\b(?:https?|postgres(?:ql)?|mysql|redis|amqp)://[^\s\"']+", re.I)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_API_KEY_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|service[_-]?token|secret)\s*[=:]\s*[^\s,;\"']+"
)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _dataset_meta(path: Path, rows: list[dict], *, role: str) -> dict:
    return {
        "source": path.name,
        "n": len(rows),
        "sha256": _sha256_bytes(path.read_bytes()),
        "role": role,
    }


def _knowledge_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in KNOWLEDGE_DIR.rglob("*") if item.is_file()):
        digest.update(path.relative_to(KNOWLEDGE_DIR).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


_GIT_TIMEOUT_SECONDS = 5.0
_PROVENANCE_GIT_PATHS = ("app", "eval", "uv.lock", "pyproject.toml")


def _git(root: Path, *args: str):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _digest_paths(root: Path, paths: list[Path]) -> dict:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(paths):
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        count += 1
    return {"files": count, "sha256": digest.hexdigest() if count else None}


def _dirty_scopes(root: Path) -> list[str] | str:
    result = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *_PROVENANCE_GIT_PATHS,
    )
    if result is None or result.returncode != 0:
        return "unknown"
    scopes: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if path.startswith("app/") and path.endswith(".py"):
            scopes.add("app")
        elif path.startswith("eval/") and path.endswith(".py"):
            scopes.add("eval")
        elif path == "uv.lock":
            scopes.add("dependencies")
        elif path == "pyproject.toml":
            scopes.add("config")
    return sorted(scopes)


def _provenance(root: Path = ROOT) -> dict:
    """Summarise the candidate commit plus allowlisted source/config digests.

    Only app/eval Python sources, the lock file and key project config are
    read. No file contents or filenames reach the report, and no ``.env`` or
    credentials are inspected.
    """
    commit = _git(root, "rev-parse", "HEAD")
    candidate_commit = (
        commit.stdout.strip() if commit is not None and commit.returncode == 0 else ""
    ) or "unknown"
    dirty = _dirty_scopes(root)

    def dirty_state(scope: str) -> bool | str:
        return "unknown" if dirty == "unknown" else scope in dirty

    return {
        "candidate_commit": candidate_commit,
        "candidate_commit_source": "HEAD",
        "candidate_commit_note": ("Uncommitted working-tree changes are not part of this commit."),
        "worktree_dirty": dirty if dirty == "unknown" else bool(dirty),
        "dirty_scopes": dirty,
        "source": {
            "app": {
                "scope": "app/**/*.py",
                "dirty": dirty_state("app"),
                **_digest_paths(root, list((root / "app").rglob("*.py"))),
            },
            "eval": {
                "scope": "eval/**/*.py",
                "dirty": dirty_state("eval"),
                **_digest_paths(root, list((root / "eval").rglob("*.py"))),
            },
        },
        "dependency_lock": {
            "path": "uv.lock",
            "dirty": dirty_state("dependencies"),
            **_digest_paths(root, [root / "uv.lock"]),
        },
        "config": {
            "path": "pyproject.toml",
            "dirty": dirty_state("config"),
            **_digest_paths(root, [root / "pyproject.toml"]),
        },
    }


def _prompt_versions() -> dict[str, str]:
    from app.prompts.intent import PROMPT_VERSION as intent_prompt_version

    return {
        "intent": intent_prompt_version,
        "answer": ANSWER_PROMPT_VERSION,
        "faithfulness": FAITHFULNESS_PROMPT_VERSION,
        "epig_policy": EPIG_POLICY_VERSION,
    }


def _redact(value: Any, *, secrets: tuple[str, ...] = ()) -> str:
    """Remove credentials and URLs from text that may reach a report or log."""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SECRET_URL_RE.sub("[REDACTED_URL]", text)
    text = _API_KEY_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _SK_TOKEN_RE.sub("[REDACTED]", text)
    return text


def offline_settings(
    *,
    database_url: str = "sqlite+aiosqlite:///:memory:",
) -> Settings:
    """Settings that cannot inherit a real provider, model, DB or business target.

    Every security-relevant field is overridden explicitly; ``_env_file=None``
    prevents pydantic-settings from opening a developer ``.env``. Explicit init
    values also take priority over polluted parent environment variables.
    """
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_name="eval-offline",
        app_env="testing",
        log_level="WARNING",
        deepseek_api_key=SecretStr(""),
        deepseek_base_url="",
        deepseek_model="offline",
        deepseek_temperature=None,
        deepseek_reasoning_effort="",
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
        database_url=database_url,
        admin_api_token=SecretStr(""),
        knowledge_dir=str(KNOWLEDGE_DIR),
        rate_limit_per_minute=1000,
        after_sales_enabled=False,
        business_connector="fake",
        epig_base_url="",
        epig_service_token=SecretStr(""),
        epig_allowed_markets="",
        after_sales_sku_category_map="",
        after_sales_worker_markets="",
    )


class ModelBudgetExceeded(RuntimeError):
    """Raised before an eval call beyond the explicitly configured call cap."""


@dataclass(frozen=True)
class ModelRun:
    mode: Literal["fake", "real"]
    provider: str
    model: str
    max_cases: int
    max_calls: int
    max_output_tokens: int
    llm: Any = field(repr=False)


@dataclass(frozen=True)
class RealModelConfig:
    provider: str
    model: str
    max_cases: int
    max_calls: int
    max_output_tokens: int
    api_key: str = field(repr=False)
    base_url: str = ""


class _BoundedLLM:
    """Count calls and cap per-request output tokens on an injected provider."""

    def __init__(self, provider: Any, *, max_calls: int, max_output_tokens: int) -> None:
        self._provider = provider
        self._max_calls = max_calls
        self._max_output_tokens = max_output_tokens
        self.calls = 0

    def _reserve(self) -> None:
        self.calls += 1
        if self.calls > self._max_calls:
            raise ModelBudgetExceeded(
                f"eval model call budget exceeded ({self._max_calls}); "
                "raise --max-calls explicitly if more calls are intended"
            )

    def _bounded_tokens(self, requested: int) -> int:
        return max(1, min(int(requested), self._max_output_tokens))

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ):
        self._reserve()
        return await self._provider.generate(
            messages,
            temperature=temperature,
            max_tokens=self._bounded_tokens(max_tokens),
            response_format=response_format,
        )

    def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        self._reserve()
        return self._provider.stream(
            messages,
            temperature=temperature,
            max_tokens=self._bounded_tokens(max_tokens),
            response_format=response_format,
        )

    async def ping(self) -> bool:
        return await self._provider.ping()

    async def aclose(self) -> None:
        closer = getattr(self._provider, "aclose", None)
        if closer is not None:
            await closer()


def _model_calls(llm: Any) -> int:
    return int(getattr(llm, "calls", 0))


def fake_model_run(
    llm: Any,
    *,
    provider: str = "fake",
    model: str = "test-double",
    max_cases: int = 10_000,
    max_calls: int = 10_000,
    max_output_tokens: int = 512,
) -> ModelRun:
    """Wrap an injected fake provider for plumbing tests; never a real call."""
    return ModelRun(
        mode="fake",
        provider=provider,
        model=model,
        max_cases=max_cases,
        max_calls=max_calls,
        max_output_tokens=max_output_tokens,
        llm=_BoundedLLM(llm, max_calls=max_calls, max_output_tokens=max_output_tokens),
    )


def _coerce_model_run(value: ModelRun | Any | None) -> ModelRun | None:
    if value is None or isinstance(value, ModelRun):
        return value
    return fake_model_run(value)


def _resolve_injection(
    model_run: ModelRun | Any | None,
    llm: Any | None,
) -> ModelRun | None:
    """Accept either the new ``model_run`` seam or the old ``llm`` test seam."""
    if model_run is not None and llm is not None:
        raise TypeError("pass either model_run or llm, not both")
    return _coerce_model_run(model_run if model_run is not None else llm)


def _limited_rows(path: Path, max_cases: int | None) -> list[dict]:
    rows = load_jsonl(path)
    return rows if max_cases is None else rows[:max_cases]


def _model_meta(model_run: ModelRun | None) -> dict:
    if model_run is None:
        return {
            "mode": "offline",
            "provider": "none",
            "model": None,
            "quality_measured": False,
        }
    return {
        "mode": model_run.mode,
        "provider": model_run.provider,
        "model": model_run.model,
        # Configuration alone never proves measured quality; each eval sets
        # this from the calls and errors it actually observed.
        "quality_measured": False,
        "limits": {
            "max_cases_per_task": model_run.max_cases,
            "max_model_calls": model_run.max_calls,
            "max_output_tokens_per_call": model_run.max_output_tokens,
        },
    }


_EVAL_SECTIONS = (
    "intent_rules",
    "intent_overlay",
    "intent_v1",
    "intent_v2",
    "intent_v3",
    "retrieval",
    "epig_policy",
    "epig_policy_llm",
    "generation",
    "e2e",
)
_TASK_SECTIONS = {
    "intent": ("intent_rules", "intent_overlay", "intent_v1", "intent_v2", "intent_v3"),
    "retrieval": ("retrieval",),
    "epig-policy": ("epig_policy", "epig_policy_llm"),
    "generation": ("generation",),
    "e2e": ("e2e",),
}
_DETERMINISTIC_LABELS = {
    "intent_rules": "intent_rules",
    "intent_overlay": "intent_overlay",
    "retrieval": "offline_retrieval",
    "epig_policy": "epig_policy_deterministic",
}


def _quality_scope(
    *,
    mode: str,
    attempted_calls: int,
    errors: int,
    budget_exceeded: bool,
) -> str:
    """Classify how much real-model quality was actually observed."""
    if mode != "real" or attempted_calls <= 0:
        return "none"
    if errors or budget_exceeded:
        return "partial"
    return "full"


def _effective_quality_scope(section: dict) -> str:
    """Read the recorded scope, falling back to execution evidence."""
    scope = section.get("quality_scope")
    if scope in {"full", "partial", "none"}:
        return scope
    if section.get("mode") != "real":
        return "none"
    attempted = section.get("attempted_model_calls")
    if attempted is None:
        return "full" if section.get("quality_measured") is True else "none"
    if attempted <= 0:
        return "none"
    if section.get("errors") or section.get("budget_exceeded"):
        return "partial"
    return "full" if section.get("quality_measured") is True else "none"


def _section_measurement_label(key: str, section: dict) -> str | None:
    if key in _DETERMINISTIC_LABELS:
        return _DETERMINISTIC_LABELS[key]
    scope = _effective_quality_scope(section)
    if key in {"intent_v1", "intent_v2", "intent_v3"}:
        if scope == "full":
            return f"{key}_real_model_quality"
        if section.get("measurement") == "injected_fake_plumbing":
            return f"{key}_call_plumbing"
        return f"{key}_partial_model_run" if scope == "partial" else None
    if key == "generation":
        if scope == "full":
            return "generation_faithfulness_quality"
        if scope == "partial":
            return "generation_partial_model_run"
        if section.get("model_generation_tested"):
            return "generation_call_plumbing"
        return None
    if key == "epig_policy_llm":
        if scope == "full":
            return "epig_policy_generation_quality"
        if scope == "partial":
            return "epig_policy_generation_partial_model_run"
        if section.get("attempted_model_calls"):
            return "epig_policy_generation_call_plumbing"
        return "epig_policy_review_gating_only"
    if key == "e2e":
        if scope == "full":
            return "real_model_graph_quality"
        if section.get("measurement") == "offline_graph_plumbing":
            return "offline_graph_plumbing"
        if scope == "partial":
            return "real_model_graph_partial_model_run"
        return "real_model_graph_plumbing"
    return None


def _run_metadata(
    *,
    task: str,
    model_run: ModelRun | None,
    results: dict | None = None,
) -> dict:
    results = results or {}
    executed_sections = [key for key in _EVAL_SECTIONS if isinstance(results.get(key), dict)]
    measured: list[str] = []
    not_measured: list[str] = []
    for key in executed_sections:
        label = _section_measurement_label(key, results[key])
        if label is not None and label not in measured:
            measured.append(label)
        for item in results[key].get("not_measured") or []:
            if item not in not_measured:
                not_measured.append(item)
    real_quality_scopes = [
        _effective_quality_scope(results[key])
        for key in executed_sections
        if results[key].get("mode") == "real"
    ]
    if not any(scope in {"full", "partial"} for scope in real_quality_scopes):
        quality_scope = "none"
    elif all(scope == "full" for scope in real_quality_scopes):
        quality_scope = "full"
    else:
        quality_scope = "partial"
    quality_measured = quality_scope == "full"
    if not quality_measured:
        for item in ("real_model_output_quality", "human_quality_review"):
            if item not in not_measured:
                not_measured.append(item)
    executed_tasks = [
        name
        for name, sections in _TASK_SECTIONS.items()
        if any(section in executed_sections for section in sections)
    ]
    return {
        "task": task,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "offline" if model_run is None else model_run.mode,
        "provider": "none" if model_run is None else model_run.provider,
        "model": None if model_run is None else model_run.model,
        "quality_measured": quality_measured,
        "quality_scope": quality_scope,
        "budget": None if model_run is None else _model_meta(model_run)["limits"],
        "budget_note": (
            None
            if model_run is None
            else "Enforced eval call/token guards, not a provider billing cap."
        ),
        "task_coverage": {
            "requested": task,
            "executed_tasks": executed_tasks,
            "not_run_tasks": [name for name in _TASK_SECTIONS if name not in executed_tasks],
            "sections": executed_sections,
        },
        "data": {
            "intent": _dataset_meta(INTENT_PATH, load_jsonl(INTENT_PATH), role="internal_eval"),
            "rag": _dataset_meta(RAG_PATH, load_jsonl(RAG_PATH), role="internal_eval"),
            "epig_policy": _dataset_meta(
                EPIG_POLICY_PATH,
                load_jsonl(EPIG_POLICY_PATH),
                role="development_regression",
            ),
            "e2e": _dataset_meta(E2E_PATH, load_jsonl(E2E_PATH), role="upstream_demo_qa"),
        },
        "knowledge_sha256": _knowledge_digest(),
        "prompt_versions": _prompt_versions(),
        "provenance": _provenance(),
        "measured": measured,
        "not_measured": not_measured,
    }


def _doc_stem(document_id: str) -> str:
    return document_id.rsplit("_", 1)[0]


def _unique_stems(chunks) -> list[str]:
    stems: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        stem = _doc_stem(chunk.document_id)
        if stem not in seen:
            seen.add(stem)
            stems.append(stem)
    return stems


def _summarize_scores(values: list[float]) -> dict:
    if not values:
        return {"mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "mean": round(sum(values) / len(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def _keyword_hit(answer: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    return all(word in answer for word in keywords)


def _missing_answer_term_groups(row: dict, answer: str) -> list[str]:
    groups = [list(alternatives) for alternatives in row.get("required_answer_term_groups") or []]
    covered_terms = {term for alternatives in groups for term in alternatives}
    groups.extend([term] for term in row.get("required_terms") or [] if term not in covered_terms)
    return [
        " / ".join(alternatives)
        for alternatives in groups
        if not any(term in answer for term in alternatives)
    ]


def eval_intent_rules() -> dict:
    from app.agents.nodes.intent import rule_based_intent

    rows = load_jsonl(INTENT_PATH)
    pairs = [(row["intent"], rule_based_intent(row["query"]).label.value) for row in rows]
    return {
        **_model_meta(None),
        "measurement": "deterministic",
        "backend": "rules",
        "prompt_version": RULES_VERSION,
        "data": _dataset_meta(INTENT_PATH, rows, role="internal_eval"),
        "n": len(pairs),
        "accuracy": round(accuracy(pairs), 4),
        "per_label": {k: round(v, 4) for k, v in classification_report(pairs).items()},
        "not_measured": ["real_model_output_quality"],
    }


async def eval_intent_llm(
    *,
    model_run: ModelRun | Any | None = None,
    llm: Any | None = None,
    version: str,
    overlay: bool,
) -> dict:
    from app.agents.nodes.intent import IntentClassifier
    from app.prompts.intent import INTENT_SYSTEM, INTENT_SYSTEM_V1, INTENT_SYSTEM_V2

    run = _resolve_injection(model_run, llm)
    if run is None:
        raise SystemExit(
            "eval_intent_llm requires an explicit provider; the default offline path "
            "does not construct one. Use --allow-real-model with DTC_EVAL_* credentials "
            "or inject a fake ModelRun in tests."
        )
    if version == "intent_v1":
        system = INTENT_SYSTEM_V1
    elif version == "intent_v2":
        system = INTENT_SYSTEM_V2
    else:
        system = INTENT_SYSTEM
    classifier = IntentClassifier(
        run.llm,
        system_prompt=system,
        prompt_version=version,
        apply_overlay=overlay,
    )
    rows = _limited_rows(INTENT_PATH, run.max_cases)
    calls_before_all = _model_calls(run.llm)
    semaphore = asyncio.Semaphore(4)
    errors = 0
    budget_exceeded = False
    error_samples: list[str] = []

    async def _one(row: dict) -> tuple[str, str]:
        nonlocal errors, budget_exceeded
        async with semaphore:
            try:
                pred = (await classifier.classify(row["query"])).label.value
                return row["intent"], pred
            except ModelBudgetExceeded as exc:
                errors += 1
                budget_exceeded = True
                if len(error_samples) < 3:
                    error_samples.append(_redact(exc))
                return row["intent"], "ERROR"
            except Exception as exc:
                errors += 1
                if len(error_samples) < 3:
                    error_samples.append(_redact(exc))
                return row["intent"], "ERROR"

    pairs = list(await asyncio.gather(*[_one(row) for row in rows]))
    real = run.mode == "real"
    attempted_model_calls = _model_calls(run.llm) - calls_before_all
    quality_scope = _quality_scope(
        mode=run.mode,
        attempted_calls=attempted_model_calls,
        errors=errors,
        budget_exceeded=budget_exceeded,
    )
    quality_measured = quality_scope == "full"
    mismatches = (
        [
            {"query": rows[i]["query"], "gold": gold, "pred": pred}
            for i, (gold, pred) in enumerate(pairs)
            if gold != pred
        ]
        if real
        else []
    )
    return {
        **_model_meta(run),
        "quality_measured": quality_measured,
        "quality_scope": quality_scope,
        "measurement": "real_model" if real else "injected_fake_plumbing",
        "backend": run.provider,
        "prompt_version": version,
        "overlay": overlay,
        "data": _dataset_meta(INTENT_PATH, rows, role="internal_eval"),
        "n": len(pairs),
        "attempted_model_calls": attempted_model_calls,
        "errors": errors,
        "budget_exceeded": budget_exceeded,
        "error_samples": error_samples,
        "accuracy": round(accuracy(pairs), 4) if real else None,
        "per_label": (
            {k: round(v, 4) for k, v in classification_report(pairs).items()} if real else None
        ),
        "mismatches": mismatches,
        "not_measured": [] if quality_measured else ["intent_accuracy"],
        "quality_note": (
            "Real provider output measured on the fixed dataset."
            if quality_measured
            else (
                "Real provider output was partial or absent; intent quality is not "
                "reported as fully measured."
                if real
                else "Injected fake provider: call plumbing only, not model quality."
            )
        ),
    }


def eval_intent_overlay() -> dict:
    from app.agents.nodes.intent_overlay import high_precision_intent

    rows = load_jsonl(INTENT_PATH)
    pairs = []
    uncovered = 0
    for row in rows:
        pred = high_precision_intent(row["query"])
        if pred is None:
            uncovered += 1
            pairs.append((row["intent"], "NONE"))
        else:
            pairs.append((row["intent"], pred.value))
    return {
        **_model_meta(None),
        "measurement": "deterministic",
        "backend": "overlay",
        "prompt_version": OVERLAY_VERSION,
        "data": _dataset_meta(INTENT_PATH, rows, role="internal_eval"),
        "n": len(pairs),
        "uncovered": uncovered,
        "accuracy": round(accuracy(pairs), 4),
        "per_label": {k: round(v, 4) for k, v in classification_report(pairs).items()},
        "not_measured": ["real_model_output_quality"],
    }


async def eval_retrieval(*, rows: list[dict] | None = None) -> dict:
    """Deterministic BM25 + hash-vector + lexical rerank; no model is loaded."""
    from app.providers.embeddings import HashEmbeddingProvider
    from app.providers.reranker import LexicalRerankProvider
    from app.retrieval.hybrid import HybridRetriever
    from app.retrieval.ingest import ingest_to_memory
    from app.retrieval.reranker import DocumentReranker
    from app.retrieval.vector import VectorRetriever

    settings = offline_settings()
    embedder = HashEmbeddingProvider(dim=512)
    bm25, store, chunks = await ingest_to_memory(str(KNOWLEDGE_DIR), embedder, settings=settings)
    vector = VectorRetriever(store, embedder)
    hybrid = HybridRetriever(bm25, vector, rrf_k=60)
    data_rows = rows if rows is not None else load_jsonl(RAG_PATH)

    async def _run(name: str, retrieve) -> dict:
        precs1, recalls3, hits1, hits3, mrrs = [], [], [], [], []
        for row in data_rows:
            result = await retrieve(row["query"])
            stems = _unique_stems(result)
            relevant = set(row["relevant_doc_ids"])
            precs1.append(precision_at_k(relevant, stems, 1))
            recalls3.append(recall_at_k(relevant, stems, 3))
            hits1.append(hit_at_k(relevant, stems, 1))
            hits3.append(hit_at_k(relevant, stems, 3))
            mrrs.append(mrr(relevant, stems))
        return {
            "backend": name,
            "n": len(data_rows),
            "precision_at_1": _summarize_scores(precs1),
            "recall_at_3": _summarize_scores(recalls3),
            "hit_at_1": _summarize_scores(hits1),
            "hit_at_3": _summarize_scores(hits3),
            "mrr": _summarize_scores(mrrs),
        }

    async def _bm25(query: str):
        return bm25.search(query, top_k=5)

    async def _vector(query: str):
        return await vector.search(query, top_k=5)

    async def _hybrid(query: str):
        return await hybrid.retrieve(query, top_k=5)

    lexical = DocumentReranker(LexicalRerankProvider())

    async def _hybrid_lexical(query: str):
        fused = await hybrid.retrieve(query, top_k=8)
        return await lexical.rerank(query, fused, top_n=5)

    systems = [
        await _run("bm25", _bm25),
        await _run("hash-vector", _vector),
        await _run("bm25+hash-vector+rrf", _hybrid),
        await _run("hybrid+lexical-fallback", _hybrid_lexical),
    ]
    return {
        **_model_meta(None),
        "measurement": "deterministic",
        "version": RETRIEVAL_VERSION,
        "data": _dataset_meta(RAG_PATH, data_rows, role="internal_eval"),
        "knowledge_sha256": _knowledge_digest(),
        "dense_backend": "hash-vector",
        "local_embedding_configured": settings.local_embedding_configured,
        "cross_encoder_configured": settings.cross_encoder_configured,
        "cross_encoder_available": False,
        "note": (
            "Offline hash embeddings + BM25 + lexical rerank. Local embedding and "
            "Cross-Encoder models are disabled; the lexical fallback is not a Cross-Encoder."
        ),
        "n": len(data_rows),
        "systems": systems,
        "not_measured": ["local_embedding_quality", "cross_encoder_quality"],
    }


async def eval_epig_policy(*, rows: list[dict] | None = None) -> dict:
    """Deterministic scoped-retrieval eval; no paid model or live storefront calls."""
    from app.providers.embeddings import HashEmbeddingProvider
    from app.providers.reranker import LexicalRerankProvider
    from app.retrieval.cache import MemoryRetrievalCache
    from app.retrieval.hybrid import HybridRetriever
    from app.retrieval.ingest import ingest_to_memory
    from app.retrieval.policy import (
        chunk_matches_scope,
        corpus_fingerprint,
        merge_policy_review_chunks,
        policy_review_required,
    )
    from app.retrieval.reranker import DocumentReranker
    from app.retrieval.vector import VectorRetriever
    from app.schemas.retrieval import PolicyScope

    settings = offline_settings()
    embedder = HashEmbeddingProvider(dim=512)
    bm25, store, chunks = await ingest_to_memory(str(KNOWLEDGE_DIR), embedder, settings=settings)
    retriever = HybridRetriever(
        bm25,
        VectorRetriever(store, embedder),
        rrf_k=60,
        cache=MemoryRetrievalCache(ttl_seconds=60),
        corpus_revision=corpus_fingerprint(chunks),
    )
    data_rows = rows if rows is not None else load_jsonl(EPIG_POLICY_PATH)
    reranker = DocumentReranker(LexicalRerankProvider())
    cases = []
    for row in data_rows:
        scope = PolicyScope(
            brand=row["brand"],
            market=row["market"],
            as_of=date.fromisoformat(row["as_of"]),
            product_category=row.get("product_category"),
        )
        candidates = await retriever.retrieve(row["query"], top_k=8, scope=scope)
        found = await reranker.rerank(row["query"], candidates, top_n=6)
        review_chunks = retriever.matching_review_chunks(row["query"], scope=scope)
        found = merge_policy_review_chunks(found, review_chunks)
        stems = _unique_stems(found)
        scope_ok = all(chunk_matches_scope(chunk, scope) for chunk in found)
        source_ok = all(
            chunk.source_type == "internal_demo"
            and bool(chunk.source_label.strip())
            and bool(chunk.source_path.strip())
            and chunk.source_urls == []
            and chunk.policy_status == "demo"
            for chunk in found
        )
        expected_empty = bool(row.get("expected_empty"))
        expected_documents = set(row.get("expected_documents") or [])
        documents_ok = bool(found) if not expected_empty else not found
        if expected_documents:
            documents_ok = expected_documents.issubset(stems)
        expected_first = row.get("expected_first_document")
        first_document_ok = not expected_first or (bool(stems) and stems[0] == expected_first)
        terms = row.get("required_terms") or []
        text = "\n".join(chunk.content for chunk in found)
        terms_ok = all(term in text for term in terms)
        review_ok = True
        if "expect_review" in row and found:
            review_ok = policy_review_required(row["query"], found) is row["expect_review"]
        passed = (
            scope_ok and source_ok and documents_ok and first_document_ok and terms_ok and review_ok
        )
        cases.append(
            {
                "id": row["id"],
                "passed": passed,
                "documents": stems,
                "scope_ok": scope_ok,
                "source_ok": source_ok,
                "documents_ok": documents_ok,
                "first_document_ok": first_document_ok,
                "terms_ok": terms_ok,
                "review_ok": review_ok,
            }
        )
    return {
        **_model_meta(None),
        "measurement": "deterministic",
        "n": len(cases),
        "passed": sum(case["passed"] for case in cases),
        "scope_violations": sum(not case["scope_ok"] for case in cases),
        "source_failures": sum(not case["source_ok"] for case in cases),
        "document_failures": sum(not case["documents_ok"] for case in cases),
        "first_document_failures": sum(not case["first_document_ok"] for case in cases),
        "term_failures": sum(not case["terms_ok"] for case in cases),
        "review_failures": sum(not case["review_ok"] for case in cases),
        "backend": "bm25+hash-vector+rrf+lexical-rerank+scoped-cache",
        "data": _dataset_meta(EPIG_POLICY_PATH, data_rows, role="development_regression"),
        "knowledge_sha256": _knowledge_digest(),
        "corpus_revision": corpus_fingerprint(chunks),
        "dataset_note": ("34 synthetic policy cases are a fixed development regression set."),
        "model_generation_tested": False,
        "not_measured": ["model_generation", "faithfulness", "real_tool_call_accuracy"],
        "cases": cases,
    }


async def _generate_answer(llm, query: str, evidence: str) -> str:
    from app.agents.nodes.generate import AnswerPayload
    from app.prompts.answer import ANSWER_SYSTEM, ANSWER_USER
    from app.providers.json_parser import parse_model

    response = await llm.generate(
        [
            {"role": "system", "content": ANSWER_SYSTEM},
            {
                "role": "user",
                "content": ANSWER_USER.format(
                    query=query,
                    intent="faq_policy",
                    strategy="single",
                    evidence=evidence,
                    tool_results="(无)",
                ),
            },
        ],
        temperature=0.0,
        max_tokens=500,
        response_format={"type": "json_object"},
    )
    return parse_model(response.content, AnswerPayload).answer


async def eval_epig_policy_generation(
    model_run: ModelRun | Any | None = None,
    *,
    llm: Any | None = None,
) -> dict:
    """Evaluate the generation node separately from deterministic retrieval/guardrails."""
    from app.agents.nodes.generate import generate_node
    from app.core.enums import RetrievalStrategy
    from app.providers.embeddings import HashEmbeddingProvider
    from app.providers.reranker import LexicalRerankProvider
    from app.retrieval.cache import MemoryRetrievalCache
    from app.retrieval.hybrid import HybridRetriever
    from app.retrieval.ingest import ingest_to_memory
    from app.retrieval.policy import corpus_fingerprint, merge_policy_review_chunks
    from app.retrieval.reranker import DocumentReranker
    from app.retrieval.vector import VectorRetriever
    from app.schemas.retrieval import PolicyScope

    run = _resolve_injection(model_run, llm)
    if run is None:
        raise SystemExit(
            "eval_epig_policy_generation requires an explicit provider; the default "
            "offline path does not construct one."
        )

    settings = offline_settings()
    embedder = HashEmbeddingProvider(dim=512)
    bm25, store, chunks = await ingest_to_memory(str(KNOWLEDGE_DIR), embedder, settings=settings)
    retriever = HybridRetriever(
        bm25,
        VectorRetriever(store, embedder),
        rrf_k=60,
        cache=MemoryRetrievalCache(ttl_seconds=60),
        corpus_revision=corpus_fingerprint(chunks),
    )
    reranker = DocumentReranker(LexicalRerankProvider())
    data_rows = _limited_rows(EPIG_POLICY_PATH, run.max_cases)
    cases = []
    fallback_answers = 0
    errors = 0
    budget_exceeded = False
    error_samples: list[str] = []
    calls_before_all = _model_calls(run.llm)
    for row in data_rows:
        scope = PolicyScope(
            brand=row["brand"],
            market=row["market"],
            as_of=date.fromisoformat(row["as_of"]),
            product_category=row.get("product_category"),
        )
        candidates = await retriever.retrieve(row["query"], top_k=8, scope=scope)
        found = await reranker.rerank(row["query"], candidates, top_n=6)
        review_chunks = retriever.matching_review_chunks(row["query"], scope=scope)
        found = merge_policy_review_chunks(found, review_chunks)
        calls_before = _model_calls(run.llm)
        try:
            result = await generate_node(
                {
                    "user_query": row["query"],
                    "policy_scope": scope,
                    "strategy": RetrievalStrategy.SINGLE,
                    "retrieved_chunks": found,
                    "tool_results": [],
                },
                run.llm,
            )
        except ModelBudgetExceeded as exc:
            errors += 1
            budget_exceeded = True
            if len(error_samples) < 3:
                error_samples.append(_redact(exc))
            break
        except Exception as exc:
            errors += 1
            if len(error_samples) < 3:
                error_samples.append(_redact(exc))
            continue
        expected_refused = bool(row.get("expected_empty") or row.get("expect_review"))
        citations = result.get("citations") or []
        answer = result.get("final_answer") or ""
        refusal_ok = bool(result.get("refused")) is expected_refused
        missing_terms: list[str] = []
        if expected_refused:
            citations_ok = not citations if row.get("expected_empty") else bool(citations)
            terms_ok = True
        else:
            citations_ok = bool(citations)
            missing_terms = _missing_answer_term_groups(row, answer)
            terms_ok = not missing_terms
        citations_ok = citations_ok and all(
            citation.brand == scope.brand
            and citation.market == scope.market
            and citation.source_type == "internal_demo"
            and bool(citation.source_label.strip())
            and bool(citation.source_path.strip())
            and citation.source_urls == []
            for citation in citations
        )
        disclosure_ok = EPIG_DEMO_DISCLOSURE in answer
        expected_calls = 0 if expected_refused else 1
        model_call_ok = _model_calls(run.llm) - calls_before == expected_calls
        fallback = answer.startswith("暂时无法生成完整答复") or answer.startswith(
            "暂时无法基于现有信息"
        )
        fallback_answers += int(fallback)
        passed = all(
            (
                refusal_ok,
                citations_ok,
                terms_ok,
                disclosure_ok,
                model_call_ok,
                not fallback,
            )
        )
        cases.append(
            {
                "id": row["id"],
                "passed": passed,
                "refusal_ok": refusal_ok,
                "citations_ok": citations_ok,
                "terms_ok": terms_ok,
                "disclosure_ok": disclosure_ok,
                "missing_terms": missing_terms,
                "model_call_ok": model_call_ok,
                "fallback": fallback,
                "citation_chunk_ids": [citation.chunk_id for citation in citations],
            }
        )
    real = run.mode == "real"
    attempted_model_calls = _model_calls(run.llm) - calls_before_all
    quality_scope = _quality_scope(
        mode=run.mode,
        attempted_calls=attempted_model_calls,
        errors=errors,
        budget_exceeded=budget_exceeded,
    )
    quality_measured = quality_scope == "full"
    if real:
        not_measured = ["faithfulness", "full_graph", "real_tool_call_accuracy"]
        if not quality_measured:
            not_measured = ["model_output_quality", *not_measured]
    else:
        not_measured = ["model_output_quality", "faithfulness", "hallucination_rate"]
    return {
        **_model_meta(run),
        "quality_measured": quality_measured,
        "quality_scope": quality_scope,
        "measurement": "real_model" if real else "injected_fake_plumbing",
        "n": len(cases),
        "passed": sum(case["passed"] for case in cases),
        "errors": errors,
        "budget_exceeded": budget_exceeded,
        "error_samples": error_samples,
        "model_generation_tested": attempted_model_calls > 0,
        "attempted_model_calls": attempted_model_calls,
        "fallback_answers": fallback_answers,
        "disclosure_failures": sum(not case["disclosure_ok"] for case in cases),
        "full_graph_tested": False,
        "faithfulness_tested": False,
        "data": _dataset_meta(EPIG_POLICY_PATH, data_rows, role="development_regression"),
        "knowledge_sha256": _knowledge_digest(),
        "corpus_revision": corpus_fingerprint(chunks),
        "prompt_versions": _prompt_versions(),
        "dataset_note": ("34 synthetic policy cases are a fixed development regression set."),
        "quality_note": (
            "Real provider output on fixed scoped evidence."
            if quality_measured
            else (
                "Real provider output was partial or absent; generation quality is not "
                "reported as fully measured."
                if real
                else "Injected fake provider: pass/fail is call plumbing, not model quality."
            )
        ),
        "not_measured": not_measured,
        "note": "Direct generation-node eval with fixed scoped evidence.",
        "cases": cases,
    }


async def eval_generation(
    model_run: ModelRun | Any | None = None,
    *,
    llm: Any | None = None,
) -> dict:
    """Faithfulness/hallucination eval on a real provider, or not_measured offline."""
    from app.providers.embeddings import HashEmbeddingProvider
    from app.retrieval.hybrid import HybridRetriever
    from app.retrieval.ingest import ingest_to_memory
    from app.retrieval.vector import VectorRetriever

    run = _resolve_injection(model_run, llm)
    if run is None:
        rows = load_jsonl(RAG_PATH)
        return {
            **_model_meta(None),
            "quality_scope": "none",
            "model_generation_tested": False,
            "faithfulness_tested": False,
            "n": len(rows),
            "data": _dataset_meta(RAG_PATH, rows, role="internal_eval"),
            "knowledge_sha256": _knowledge_digest(),
            "prompt_versions": _prompt_versions(),
            "reason": (
                "No model provider configured. The default offline run retrieves with hash "
                "embeddings but does not call an LLM, so faithfulness and hallucination "
                "rates are not measured."
            ),
            "not_measured": [
                "faithfulness",
                "hallucination_rate",
                "llm_output_quality",
                "keyword_hit",
            ],
            "cases": [],
        }

    settings = offline_settings()
    embedder = HashEmbeddingProvider(dim=512)
    bm25, store, chunks = await ingest_to_memory(str(KNOWLEDGE_DIR), embedder, settings=settings)
    retriever = HybridRetriever(bm25, VectorRetriever(store, embedder), rrf_k=60)
    data_rows = _limited_rows(RAG_PATH, run.max_cases)
    calls_before_all = _model_calls(run.llm)

    if run.mode == "fake":
        plumbing_cases = []
        plumbing_errors = 0
        plumbing_budget_exceeded = False
        plumbing_error_samples: list[str] = []
        for row in data_rows:
            retrieved = await retriever.retrieve(row["query"], top_k=5)
            evidence = (
                "\n\n".join(f"[{c.chunk_id}] {c.content}" for c in retrieved[:6]) or "(无检索结果)"
            )
            try:
                await _generate_answer(run.llm, row["query"], evidence)
                plumbing_cases.append({"id": row["id"], "ok": True})
            except ModelBudgetExceeded as exc:
                plumbing_errors += 1
                plumbing_budget_exceeded = True
                if len(plumbing_error_samples) < 3:
                    plumbing_error_samples.append(_redact(exc))
                break
            except Exception as exc:
                plumbing_errors += 1
                if len(plumbing_error_samples) < 3:
                    plumbing_error_samples.append(_redact(exc))
                plumbing_cases.append({"id": row["id"], "ok": False, "error": _redact(exc)})
        attempted_model_calls = _model_calls(run.llm) - calls_before_all
        return {
            **_model_meta(run),
            "quality_scope": "none",
            "measurement": "injected_fake_plumbing",
            "model_generation_tested": attempted_model_calls > 0,
            "faithfulness_tested": False,
            "n": len(data_rows),
            "attempted_model_calls": attempted_model_calls,
            "errors": plumbing_errors,
            "budget_exceeded": plumbing_budget_exceeded,
            "error_samples": plumbing_error_samples,
            "data": _dataset_meta(RAG_PATH, data_rows, role="internal_eval"),
            "knowledge_sha256": _knowledge_digest(),
            "prompt_versions": _prompt_versions(),
            "quality_note": (
                "Injected fake provider: only call plumbing was exercised. Faithfulness, "
                "hallucination and keyword rates are intentionally absent."
            ),
            "not_measured": [
                "faithfulness",
                "hallucination_rate",
                "llm_output_quality",
                "keyword_hit",
            ],
            "cases": plumbing_cases,
        }

    from app.guardrails.hallucination import FaithfulnessChecker

    checker = FaithfulnessChecker(run.llm)
    semaphore = asyncio.Semaphore(3)
    counts = {
        "naive_supported": 0,
        "rag_supported": 0,
        "checked_supported": 0,
        "naive_keyword": 0,
        "rag_keyword": 0,
        "checked_keyword": 0,
        "errors": 0,
    }
    error_samples: list[str] = []
    budget_exceeded = False

    async def _one(row: dict) -> None:
        nonlocal budget_exceeded
        async with semaphore:
            try:
                retrieved = await retriever.retrieve(row["query"], top_k=5)
                evidence = (
                    "\n\n".join(f"[{c.chunk_id}] {c.content}" for c in retrieved[:6])
                    or "(无检索结果)"
                )
                naive = await _generate_answer(run.llm, row["query"], "(无检索结果)")
                rag = await _generate_answer(run.llm, row["query"], evidence)
                naive_judge = await checker.check(
                    query=row["query"],
                    answer=naive,
                    chunks=retrieved,
                    tool_results_text="",
                )
                rag_judge = await checker.check(
                    query=row["query"],
                    answer=rag,
                    chunks=retrieved,
                    tool_results_text="",
                )
                checked = rag
                checked_judge = rag_judge
                if not rag_judge.supported:
                    from app.agents.nodes.faithfulness import self_correct_answer

                    checked, checked_judge, _, _ = await self_correct_answer(
                        llm=run.llm,
                        checker=checker,
                        query=row["query"],
                        answer=rag,
                        chunks=retrieved,
                        tool_results_text="",
                        first_result=rag_judge,
                    )
                keywords = row.get("gold_answer_contains") or []
                counts["naive_supported"] += int(naive_judge.supported)
                counts["rag_supported"] += int(rag_judge.supported)
                counts["checked_supported"] += int(checked_judge.supported)
                counts["naive_keyword"] += int(_keyword_hit(naive, keywords))
                counts["rag_keyword"] += int(_keyword_hit(rag, keywords))
                counts["checked_keyword"] += int(_keyword_hit(checked, keywords))
            except ModelBudgetExceeded as exc:
                counts["errors"] += 1
                budget_exceeded = True
                if len(error_samples) < 3:
                    error_samples.append(_redact(exc))
            except Exception as exc:
                counts["errors"] += 1
                if len(error_samples) < 3:
                    error_samples.append(_redact(exc))

    await asyncio.gather(*[_one(row) for row in data_rows])
    n = len(data_rows)
    checked_h = (n - counts["checked_supported"]) / n if n else 0.0
    naive_h = (n - counts["naive_supported"]) / n if n else 0.0
    drop = ((naive_h - checked_h) / naive_h) if naive_h else 0.0
    attempted_model_calls = _model_calls(run.llm) - calls_before_all
    quality_scope = _quality_scope(
        mode=run.mode,
        attempted_calls=attempted_model_calls,
        errors=counts["errors"],
        budget_exceeded=budget_exceeded,
    )
    quality_measured = quality_scope == "full"
    not_measured = ["full_graph_faithfulness", "human_quality_review"]
    if not quality_measured:
        not_measured = ["faithfulness", "hallucination_rate", *not_measured]
    return {
        **_model_meta(run),
        "quality_measured": quality_measured,
        "quality_scope": quality_scope,
        "measurement": "real_model",
        "n": n,
        "judge": f"{run.provider}-faithfulness_v1",
        "judge_model": run.model,
        "errors": counts["errors"],
        "budget_exceeded": budget_exceeded,
        "error_samples": error_samples,
        "attempted_model_calls": attempted_model_calls,
        "naive_no_context": {
            "faithfulness": round(counts["naive_supported"] / n, 4) if n else 0.0,
            "hallucination_rate": round(naive_h, 4),
            "keyword_hit": round(counts["naive_keyword"] / n, 4) if n else 0.0,
        },
        "rag": {
            "faithfulness": round(counts["rag_supported"] / n, 4) if n else 0.0,
            "hallucination_rate": round((n - counts["rag_supported"]) / n, 4) if n else 0.0,
            "keyword_hit": round(counts["rag_keyword"] / n, 4) if n else 0.0,
        },
        "rag_plus_self_check": {
            "faithfulness": round(counts["checked_supported"] / n, 4) if n else 0.0,
            "hallucination_rate": round(checked_h, 4),
            "keyword_hit": round(counts["checked_keyword"] / n, 4) if n else 0.0,
        },
        "hallucination_relative_drop": round(drop, 4),
        "data": _dataset_meta(RAG_PATH, data_rows, role="internal_eval"),
        "knowledge_sha256": _knowledge_digest(),
        "prompt_versions": _prompt_versions(),
        "not_measured": not_measured,
        "note": (
            "Hallucination = LLM judge supported=false. Same-provider judge is optimistic; "
            "relative drop is naive vs RAG+self-check. Not a human quality verdict."
        ),
    }


def _chat_service_with_llm(container: Any, llm: Any) -> Any:
    """Rebuild ChatService with an explicitly injected, budgeted provider."""
    from app.services.chat_service import ChatService

    old = container.chat_service
    return ChatService(
        settings=container.settings,
        llm=llm,
        retriever=container.retriever,
        reranker=old._reranker,
        session_factory=container.session_factory,
        rate_limiter=old._rate_limiter,
        registry=old.registry,
        staff_mode=old._staff_mode,
    )


def _selection_quality_flags(
    *,
    real: bool,
    quality_measured: bool,
    agent_tool_total: int,
    positive_scorable_cases_with_calls: int,
    inhibition_scorable_cases: int,
) -> tuple[bool, bool]:
    """Separate positive tool-selection measurement from inhibition.

    Measurement only asks whether a scorable positive expectation was actually
    evaluated with real calls. It does not require correctness; unmatched
    expectations still count, with correctness reported as a numerator.
    Empty-expectation cases evidence inhibition separately.
    """
    positive = bool(
        real
        and quality_measured
        and agent_tool_total > 0
        and positive_scorable_cases_with_calls > 0
    )
    inhibition = bool(real and quality_measured and inhibition_scorable_cases > 0)
    return positive, inhibition


async def eval_e2e(
    model_run: ModelRun | Any | None = None,
    *,
    llm: Any | None = None,
    rows: list[dict] | None = None,
) -> dict:
    """Run the graph only against a fresh temp SQLite DB, with guaranteed cleanup."""
    from app.schemas.chat import ChatRequest
    from app.services.container import build_container

    run = _resolve_injection(model_run, llm)
    data_rows = (
        rows
        if rows is not None
        else _limited_rows(E2E_PATH, run.max_cases if run is not None else None)
    )
    calls_before_all = _model_calls(run.llm) if run is not None else 0
    errors = 0
    budget_exceeded = False
    error_samples: list[str] = []
    latencies: list[float] = []
    intent_ms: list[float] = []
    retrieval_ms: list[float] = []
    llm_ms: list[float] = []
    tool_ms: list[float] = []
    refuse_ok = 0
    refuse_n = 0
    keyword_ok = 0
    keyword_n = 0
    proxy_ok = 0
    proxy_n = 0
    details: list[dict] = []
    agent_tool_total = 0
    agent_tool_cases_with_calls = 0
    agent_tool_by_name: Counter[str] = Counter()
    agent_tool_by_status: Counter[str] = Counter()
    expectation_cases = 0
    scorable_cases = 0
    unscorable_cases = 0
    expectation_matches = 0
    positive_cases = 0
    positive_cases_scorable = 0
    positive_cases_scorable_with_calls = 0
    positive_cases_matched = 0
    inhibition_cases = 0
    inhibition_cases_scorable = 0
    inhibition_cases_matched = 0
    expectation_failures: list[str] = []
    unscorable_case_ids: list[str] = []
    trace_installed = False

    def _finish_case(
        row: dict,
        calls: list[dict],
        trace: ToolTrace,
        *,
        evaluated: bool = True,
    ) -> dict:
        nonlocal agent_tool_total, agent_tool_cases_with_calls
        nonlocal expectation_cases, scorable_cases, unscorable_cases, expectation_matches
        nonlocal positive_cases, positive_cases_scorable
        nonlocal positive_cases_scorable_with_calls, positive_cases_matched
        nonlocal inhibition_cases, inhibition_cases_scorable, inhibition_cases_matched
        detail: dict = {"id": row["id"], "agent_tool_calls": calls}
        if calls:
            agent_tool_cases_with_calls += 1
            agent_tool_total += len(calls)
            for call in calls:
                agent_tool_by_name[call["name"]] += 1
                agent_tool_by_status[call["status"]] += 1
        # Only an explicit dataset field is an assertion; other fields are ignored.
        if "expect_tools" in row:
            expectation_cases += 1
            configured = row["expect_tools"]
            is_inhibition = isinstance(configured, list) and not configured
            is_positive = isinstance(configured, list) and bool(configured)
            if is_inhibition:
                inhibition_cases += 1
            elif is_positive:
                positive_cases += 1
            if not evaluated:
                comparison: dict = {"matched": False, "error": "case_not_evaluated"}
                scorable = False
            else:
                try:
                    comparison = compare_tool_expectation(configured, calls, trace)
                except ValueError:
                    comparison = {"matched": False, "error": "invalid_expectation"}
                    scorable = False
                else:
                    # Truncated/sanitized arguments are never exact and never
                    # enter the scoring denominator.
                    scorable = "error" not in comparison and "reason" not in comparison
            comparison["scorable"] = scorable
            if scorable:
                scorable_cases += 1
                if is_inhibition:
                    inhibition_cases_scorable += 1
                elif is_positive:
                    positive_cases_scorable += 1
                    if calls:
                        positive_cases_scorable_with_calls += 1
                if comparison["matched"]:
                    expectation_matches += 1
                    if is_inhibition:
                        inhibition_cases_matched += 1
                    elif is_positive:
                        positive_cases_matched += 1
                else:
                    expectation_failures.append(str(row["id"]))
            else:
                unscorable_cases += 1
                unscorable_case_ids.append(str(row["id"]))
            detail["agent_tool_expectation"] = comparison
        return detail

    with tempfile.TemporaryDirectory(prefix="dtc-eval-e2e-") as temp_dir:
        database_url = f"sqlite+aiosqlite:///{Path(temp_dir, 'eval.sqlite3').as_posix()}"
        settings = offline_settings(database_url=database_url)
        container = None
        trace: ToolTrace | None = None
        try:
            try:
                container = await build_container(settings, seed=True)
            except Exception as exc:
                raise RuntimeError(f"e2e infrastructure failed: {_redact(exc)}") from None
            if run is not None:
                container.chat_service = _chat_service_with_llm(container, run.llm)
            # Install before graph/service execution. The compiled graph closes
            # over this same eval-only registry object, so it dispatches wrappers.
            trace = ToolTrace(sanitize=_redact)
            trace.install(container.chat_service.registry)
            trace_installed = True
            for row in data_rows:
                request = ChatRequest(message=row["query"], user_id=row.get("user_id") or "u_demo")
                started = time.perf_counter()
                trace.begin_case(str(row["id"]))
                try:
                    response = await container.chat_service.chat(
                        request, trace_id=f"eval_{row['id']}"
                    )
                except ModelBudgetExceeded as exc:
                    errors += 1
                    budget_exceeded = True
                    if len(error_samples) < 3:
                        error_samples.append(_redact(exc))
                    detail = _finish_case(row, trace.end_case(), trace, evaluated=False)
                    detail["error"] = "model_budget_exceeded"
                    details.append(detail)
                    break
                except Exception as exc:
                    errors += 1
                    redacted = _redact(exc)
                    if len(error_samples) < 3:
                        error_samples.append(redacted)
                    detail = _finish_case(row, trace.end_case(), trace, evaluated=False)
                    detail["error"] = redacted
                    details.append(detail)
                    continue
                case_calls = trace.end_case()
                total_ms = (time.perf_counter() - started) * 1000
                latencies.append(response.latency.total_ms or total_ms)
                intent_ms.append(response.latency.intent_ms)
                retrieval_ms.append(response.latency.retrieval_ms)
                llm_ms.append(response.latency.llm_ms)
                tool_ms.append(response.latency.tool_ms)
                if row.get("expect_refused"):
                    refuse_n += 1
                    if response.refused:
                        refuse_ok += 1
                keywords = row.get("expect_keywords") or []
                if keywords:
                    keyword_n += 1
                    if _keyword_hit(response.answer, keywords):
                        keyword_ok += 1
                if any(token in row["query"] for token in ("ORD", "库存", "取消")):
                    proxy_n += 1
                    if (
                        "ORD" in response.answer
                        or response.pending_action
                        or "库存" in response.answer
                    ):
                        proxy_ok += 1
                detail = _finish_case(row, case_calls, trace)
                detail.update(
                    {
                        "refused": response.refused,
                        "strategy": response.strategy.value if response.strategy else None,
                        "intent": response.intent.value if response.intent else None,
                        "latency_ms": round(latencies[-1], 1),
                    }
                )
                details.append(detail)
        finally:
            try:
                if trace is not None:
                    trace.uninstall()
            finally:
                if container is not None:
                    await container.aclose()

    mode: Mode = "offline" if run is None else run.mode
    real = mode == "real"
    attempted_model_calls = (run and _model_calls(run.llm) - calls_before_all) or 0
    quality_scope = _quality_scope(
        mode=mode,
        attempted_calls=attempted_model_calls,
        errors=errors,
        budget_exceeded=budget_exceeded,
    )
    quality_measured = quality_scope == "full"
    selection_quality_measured, inhibition_measured = _selection_quality_flags(
        real=real,
        quality_measured=quality_measured,
        agent_tool_total=agent_tool_total,
        positive_scorable_cases_with_calls=positive_cases_scorable_with_calls,
        inhibition_scorable_cases=inhibition_cases_scorable,
    )
    not_measured = ["human_quality_review"]
    if not selection_quality_measured:
        not_measured.insert(0, "real_tool_call_accuracy")
    if not quality_measured:
        not_measured = ["faithfulness", "llm_output_quality", *not_measured]
    return {
        "mode": mode,
        "provider": "offline" if run is None else run.provider,
        "model": "offline-template" if run is None else run.model,
        "quality_measured": quality_measured,
        "quality_scope": quality_scope,
        "measurement": "real_model_graph" if real else "offline_graph_plumbing",
        "n": len(data_rows),
        "errors": errors,
        "budget_exceeded": budget_exceeded,
        "error_samples": error_samples,
        "attempted_model_calls": attempted_model_calls,
        "data": _dataset_meta(E2E_PATH, data_rows, role="upstream_demo_qa"),
        "knowledge_sha256": _knowledge_digest(),
        "prompt_versions": _prompt_versions(),
        "database": "fresh-temporary-sqlite",
        "temporary_database_removed": True,
        "synthetic_seed": True,
        "refuse_accuracy": round(refuse_ok / refuse_n, 4) if refuse_n else None,
        "keyword_hit": round(keyword_ok / keyword_n, 4) if keyword_n else None,
        "keyword_hit_note": (
            "Answer-text keyword presence only; not a model-quality or tool-correctness score."
        ),
        # Historic field name was misleading: it only checks answer text, not tool execution.
        "legacy_answer_keyword_proxy": round(proxy_ok / proxy_n, 4) if proxy_n else None,
        "legacy_answer_keyword_proxy_note": (
            "Answer-text keyword/pending-action presence for order/inventory/cancel hints. "
            "This is NOT tool-call name/parameter/count accuracy."
        ),
        "agent_tool_trace_measured": trace_installed,
        "agent_tool_calls": {
            "total": agent_tool_total,
            "by_name": dict(sorted(agent_tool_by_name.items())),
            "by_status": dict(sorted(agent_tool_by_status.items())),
            "cases_with_calls": agent_tool_cases_with_calls,
            "instrumented": trace_installed,
            "scope": "eval_container_registry",
            "note": (
                "Actual agent Tool.run dispatches through this eval container's own registry. "
                "Zero is reported as zero; business-connector calls and answer keywords are "
                "not counted as agent tool calls."
            ),
        },
        "agent_tool_expectations": {
            "cases_with_expectations": expectation_cases,
            "cases_scorable": scorable_cases,
            "cases_unscorable": unscorable_cases,
            "unscorable": unscorable_case_ids,
            "cases_matched": expectation_matches,
            "accuracy": (
                round(expectation_matches / scorable_cases, 4) if scorable_cases else None
            ),
            "failures": expectation_failures,
            "positive_cases": positive_cases,
            "positive_cases_scorable": positive_cases_scorable,
            "positive_cases_scorable_with_calls": positive_cases_scorable_with_calls,
            "positive_cases_matched": positive_cases_matched,
            "inhibition_cases": inhibition_cases,
            "inhibition_cases_scorable": inhibition_cases_scorable,
            "inhibition_cases_matched": inhibition_cases_matched,
            "selection_quality_measured": selection_quality_measured,
            "inhibition_measured": inhibition_measured,
            "note": (
                "Exact tool name/validated-argument/count match. Truncated or sanitized "
                "arguments and errors are unscorable and excluded from the denominator. "
                "Measured positive selection needs a complete real run with actual calls and "
                "at least one scorable positive expectation, even if wrong; inhibition is "
                "measured separately from empty expectations."
            ),
        },
        "connector_calls": {
            "measured": False,
            "counted_as_agent_tool_calls": False,
            "note": (
                "Business connector invocations are not instrumented and are never counted "
                "as agent tool calls."
            ),
        },
        # True only for a complete real-model run; offline plumbing remains false.
        "real_tool_trace_measured": bool(real and trace_installed and quality_measured),
        "latency_scope": "real-model-pipeline" if real else "offline-pipeline",
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 1),
            "p95": round(percentile(latencies, 95), 1),
            "mean": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "intent_p50": round(percentile(intent_ms, 50), 1),
            "retrieval_p50": round(percentile(retrieval_ms, 50), 1),
            "llm_p50": round(percentile(llm_ms, 50), 1),
            "tool_p50": round(percentile(tool_ms, 50), 1),
        },
        "not_measured": not_measured,
        "cases": details,
    }


def _resume_bullets(report: dict) -> list[str]:
    bullets = []
    intent_rules = report.get("intent_rules")
    intent_v1 = report.get("intent_v1")
    intent_v2 = report.get("intent_v2")
    intent_v3 = report.get("intent_v3")
    overlay = report.get("intent_overlay")
    if intent_v3 and intent_v3.get("mode") == "real":
        parts = [f"intent_v3+overlay {intent_v3['accuracy']:.1%}"]
        if intent_v2 and intent_v2.get("mode") == "real":
            parts.insert(0, f"intent_v2 {intent_v2['accuracy']:.1%}")
        if intent_v1 and intent_v1.get("mode") == "real":
            parts.insert(0, f"intent_v1 {intent_v1['accuracy']:.1%}")
        extra = f"；规则 {intent_rules['accuracy']:.1%}" if intent_rules else ""
        if overlay:
            extra += f"；仅覆盖层 {overlay['accuracy']:.1%}"
        bullets.append(f"意图识别：{intent_v3['n']} 条 " + " → ".join(parts) + extra)
    retrieval = report.get("retrieval")
    if retrieval:
        bm25 = next((s for s in retrieval["systems"] if s["backend"] == "bm25"), None)
        dense = next(
            (
                s
                for s in retrieval["systems"]
                if s["backend"]
                not in {
                    "bm25",
                    "hybrid+cross-encoder",
                    "hybrid+lexical-fallback",
                }
                and not str(s["backend"]).endswith("+rrf")
            ),
            None,
        )
        hybrid_rrf = next((s for s in retrieval["systems"] if s["backend"].endswith("+rrf")), None)
        cross_encoder = next(
            (s for s in retrieval["systems"] if s["backend"] == "hybrid+cross-encoder"), None
        )
        if bm25:
            bullets.append(
                f"BM25 Precision@1 {bm25['precision_at_1']['mean']:.1%}，"
                f"Recall@3 {bm25['recall_at_3']['mean']:.1%}，MRR {bm25['mrr']['mean']:.2f}"
            )
        if dense:
            bullets.append(
                f"{dense['backend']} Precision@1 {dense['precision_at_1']['mean']:.1%}，"
                f"Recall@3 {dense['recall_at_3']['mean']:.1%}"
            )
        if hybrid_rrf:
            bullets.append(
                f"{hybrid_rrf['backend']} Precision@1 {hybrid_rrf['precision_at_1']['mean']:.1%}，"
                f"Recall@3 {hybrid_rrf['recall_at_3']['mean']:.1%}"
            )
        if cross_encoder:
            bullets.append(
                f"Hybrid+Cross-Encoder Precision@1 {cross_encoder['precision_at_1']['mean']:.1%}，"
                f"Recall@3 {cross_encoder['recall_at_3']['mean']:.1%}"
            )
    gen = report.get("generation")
    if gen and gen.get("mode") == "real" and gen.get("quality_measured"):
        naive = gen["naive_no_context"]
        rag = gen["rag"]
        checked = gen["rag_plus_self_check"]
        bullets.append(
            f"Faithfulness：无检索 {naive['faithfulness']:.0%} → "
            f"RAG {rag['faithfulness']:.0%} → RAG+自纠错 {checked['faithfulness']:.0%}"
        )
        bullets.append(
            f"幻觉率：无检索 {naive['hallucination_rate']:.0%} → "
            f"RAG+自纠错 {checked['hallucination_rate']:.0%}，"
            f"相对下降 {gen['hallucination_relative_drop']:.0%}"
        )
    e2e = report.get("e2e")
    if e2e and e2e.get("mode") == "real" and e2e.get("quality_measured"):
        lat = e2e["latency_ms"]
        bullets.append(
            f"E2E {e2e['n']} 条：P50 {lat['p50']}ms / P95 {lat['p95']}ms；"
            f"拒答准确率 {e2e['refuse_accuracy']}；关键词命中 {e2e['keyword_hit']}"
        )
    return bullets


async def run_all(
    *,
    model_run: ModelRun | Any | None = None,
    llm: Any | None = None,
) -> dict:
    if isinstance(llm, bool):
        if llm:
            raise SystemExit(
                "run_all(llm=True) no longer enables real calls; pass an explicit "
                "ModelRun built from --allow-real-model configuration."
            )
        run = _resolve_injection(model_run, None)
    else:
        run = _resolve_injection(model_run, llm)
    report: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "intent_rules": eval_intent_rules(),
        "intent_overlay": eval_intent_overlay(),
        "retrieval": await eval_retrieval(),
        "generation": await eval_generation(run),
        "e2e": await eval_e2e(model_run=run),
    }
    if run is not None:
        report["intent_v1"] = await eval_intent_llm(
            model_run=run, version="intent_v1", overlay=False
        )
        report["intent_v2"] = await eval_intent_llm(
            model_run=run, version="intent_v2", overlay=False
        )
        report["intent_v3"] = await eval_intent_llm(
            model_run=run, version="intent_v3", overlay=True
        )
    report["resume_bullets"] = _resume_bullets(report)
    return report


async def _aclose_llm(llm: Any) -> None:
    closer = getattr(llm, "aclose", None)
    if closer is not None:
        await closer()


def _real_model_config_from_args(args: argparse.Namespace) -> RealModelConfig | None:
    if args.llm and not args.allow_real_model:
        raise SystemExit(
            "--llm no longer enables real model calls; pass --allow-real-model with "
            "--provider, --model, --max-cases and --max-calls. Credentials are read only "
            "from DTC_EVAL_* variables."
        )
    if not args.allow_real_model:
        return None
    if args.task not in _LLM_TASKS:
        raise SystemExit(f"--task {args.task} is deterministic offline; omit --allow-real-model")
    if args.provider != "deepseek":
        raise SystemExit("--provider deepseek is required in real mode")
    if not args.model or not str(args.model).strip():
        raise SystemExit("--model is required in real mode")
    if args.max_cases is None or args.max_cases <= 0:
        raise SystemExit("--max-cases must be a positive integer in real mode")
    if args.max_calls is None or args.max_calls <= 0:
        raise SystemExit("--max-calls must be a positive integer in real mode")
    if not 1 <= args.max_output_tokens <= 2048:
        raise SystemExit("--max-output-tokens must be between 1 and 2048")

    api_key = os.environ.get("DTC_EVAL_DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "DTC_EVAL_DEEPSEEK_API_KEY is required in real mode; "
            "DEEPSEEK_API_KEY and .env files are intentionally not read"
        )
    base_url = (
        os.environ.get("DTC_EVAL_DEEPSEEK_BASE_URL", "").strip() or "https://api.deepseek.com"
    )
    return RealModelConfig(
        provider=args.provider,
        model=str(args.model).strip(),
        max_cases=args.max_cases,
        max_calls=args.max_calls,
        max_output_tokens=args.max_output_tokens,
        api_key=api_key,
        base_url=base_url,
    )


def _build_model_run(config: RealModelConfig) -> ModelRun:
    from app.providers.deepseek import DeepSeekProvider

    settings = offline_settings().model_copy(
        update={
            "deepseek_api_key": SecretStr(config.api_key),
            "deepseek_base_url": config.base_url,
            "deepseek_model": config.model,
            "llm_max_retries": 1,
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
        llm=_BoundedLLM(
            provider,
            max_calls=config.max_calls,
            max_output_tokens=config.max_output_tokens,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--task",
        choices=["all", "intent", "retrieval", "epig-policy", "generation", "e2e"],
        default="all",
    )
    parser.add_argument("--llm", action="store_true", help="deprecated; needs --allow-real-model")
    parser.add_argument(
        "--allow-real-model",
        action="store_true",
        help="explicitly allow real provider calls (credentials via DTC_EVAL_*)",
    )
    parser.add_argument("--provider", choices=["deepseek"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    args = parser.parse_args()

    config = _real_model_config_from_args(args)
    model_run = _build_model_run(config) if config is not None else None

    async def _go() -> dict:
        try:
            if args.task == "all":
                return await run_all(model_run=model_run)
            if args.task == "intent":
                result: dict[str, Any] = {
                    "intent_rules": eval_intent_rules(),
                    "intent_overlay": eval_intent_overlay(),
                }
                if model_run is not None:
                    result["intent_v1"] = await eval_intent_llm(
                        model_run=model_run, version="intent_v1", overlay=False
                    )
                    result["intent_v2"] = await eval_intent_llm(
                        model_run=model_run, version="intent_v2", overlay=False
                    )
                    result["intent_v3"] = await eval_intent_llm(
                        model_run=model_run, version="intent_v3", overlay=True
                    )
                result["resume_bullets"] = _resume_bullets(result)
                return result
            if args.task == "retrieval":
                return {"retrieval": await eval_retrieval()}
            if args.task == "epig-policy":
                policy_result: dict[str, Any] = {"epig_policy": await eval_epig_policy()}
                if model_run is not None:
                    policy_result["epig_policy_llm"] = await eval_epig_policy_generation(model_run)
                return policy_result
            if args.task == "generation":
                return {"generation": await eval_generation(model_run)}
            return {"e2e": await eval_e2e(model_run=model_run)}
        finally:
            if model_run is not None:
                await _aclose_llm(model_run.llm)

    report = asyncio.run(_go())
    report["run"] = _run_metadata(task=args.task, model_run=model_run, results=report)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"{args.task}_{stamp}.json"
    latest = REPORT_DIR / (
        "epig_policy_latest.json" if args.task == "epig-policy" else "latest.json"
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nWrote {path}")
    if report["run"]["mode"] == "offline":
        print(
            "\nOFFLINE: deterministic/plumbing checks only; model quality is not measured "
            "and no provider was called."
        )
    if args.task == "epig-policy":
        result = report["epig_policy"]
        if result["passed"] != result["n"]:
            raise SystemExit("E-pig policy evaluation failed; see the report for failing cases")
        generation = report.get("epig_policy_llm")
        if generation is not None and generation["passed"] != generation["n"]:
            raise SystemExit(
                "E-pig real-model generation evaluation failed; see the report for failing cases"
            )


if __name__ == "__main__":
    main()
