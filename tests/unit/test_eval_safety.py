"""Safety regressions for the offline-first eval harness.

These tests prove that default eval tasks cannot inherit a developer ``.env``,
real provider keys, an EPIG target or an existing database. Every real-model
path is exercised only with an injected fake provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.providers.base import LLMResponse
from app.schemas.common import TokenUsage
from eval import runner

CANARY = "synthetic-canary-7f3a"


def _pollute_parent_env(monkeypatch) -> None:
    values = {
        "APP_ENV": "production",
        "APP_NAME": f"{CANARY}-app",
        "DEEPSEEK_API_KEY": f"{CANARY}-deepseek",
        "DEEPSEEK_BASE_URL": f"https://{CANARY}-provider.invalid/v1",
        "DEEPSEEK_MODEL": f"{CANARY}-model",
        "DEEPSEEK_THINKING_MODE": "enabled",
        "EMBEDDING_API_KEY": f"{CANARY}-embed",
        "EMBEDDING_BASE_URL": f"https://{CANARY}-embed.invalid",
        "DATABASE_URL": f"postgresql+asyncpg://{CANARY}:{CANARY}@db.invalid/{CANARY}",
        "REDIS_URL": f"redis://{CANARY}.invalid/0",
        "QDRANT_URL": f"https://{CANARY}-qdrant.invalid",
        "MCP_ENABLED": "true",
        "MCP_REMOTE_URL": f"https://{CANARY}-mcp.invalid",
        "EPIG_BASE_URL": f"https://{CANARY}-epig.invalid",
        "EPIG_SERVICE_TOKEN": f"{CANARY}-epig-token",
        "EPIG_ALLOWED_MARKETS": "au",
        "BUSINESS_CONNECTOR": "epig",
        "AFTER_SALES_ENABLED": "true",
        "LOCAL_EMBEDDING_ENABLED": "true",
        "LOCAL_EMBEDDING_FORCE": "true",
        "CROSS_ENCODER_ENABLED": "true",
        "CROSS_ENCODER_FORCE": "true",
        "UNSTRUCTURED_ENABLED": "true",
        "UNSTRUCTURED_FORCE": "true",
        "RATE_LIMIT_PER_MINUTE": "1",
        "KNOWLEDGE_DIR": f"/tmp/{CANARY}-knowledge",
        "ADMIN_API_TOKEN": f"{CANARY}-admin",
        "AFTER_SALES_WORKER_MARKETS": "au",
        "AFTER_SALES_SKU_CATEGORY_MAP": f'{{"{CANARY}":"{CANARY}"}}',
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_offline_settings_ignore_polluted_env_and_decoy_dotenv(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"APP_NAME={CANARY}-dotenv-app",
                f"DEEPSEEK_API_KEY={CANARY}-dotenv-key",
                f"DATABASE_URL=postgresql+asyncpg://{CANARY}:{CANARY}@dotenv.invalid/db",
                "API_V1_PREFIX=/canary-prefix",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _pollute_parent_env(monkeypatch)

    settings = runner.offline_settings()

    assert settings.app_env == "testing"
    assert settings.app_name == "eval-offline"
    assert settings.api_v1_prefix == "/api/v1"  # decoy .env was not opened
    assert settings.deepseek_api_key.get_secret_value() == ""
    assert settings.deepseek_model == "offline"
    assert settings.deepseek_thinking_mode is None
    assert settings.embedding_api_key.get_secret_value() == ""
    assert settings.database_url == "sqlite+aiosqlite:///:memory:"
    assert settings.business_connector == "fake"
    assert settings.after_sales_enabled is False
    assert settings.epig_configured is False
    assert settings.epig_service_token.get_secret_value() == ""
    assert settings.redis_url == ""
    assert settings.qdrant_url == ""
    assert settings.mcp_enabled is False
    assert settings.local_embedding_configured is False
    assert settings.cross_encoder_configured is False
    assert settings.unstructured_configured is False
    assert settings.rate_limit_per_minute == 1000
    assert settings.admin_api_token.get_secret_value() == ""
    assert settings.knowledge_dir == str(runner.KNOWLEDGE_DIR)

    dumped = settings.model_dump_json()
    assert CANARY not in dumped
    assert "postgresql" not in dumped


def test_offline_settings_accept_only_an_explicit_temp_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'eval.sqlite3'}"
    settings = runner.offline_settings(database_url=database_url)
    assert settings.database_url == database_url
    assert "postgresql" not in settings.database_url


async def test_default_generation_is_not_measured_without_an_llm() -> None:
    result = await runner.eval_generation()

    assert result["mode"] == "offline"
    assert result["provider"] == "none"
    assert result["model"] is None
    assert result["quality_measured"] is False
    assert result["model_generation_tested"] is False
    assert result["faithfulness_tested"] is False
    assert result["not_measured"]
    assert "naive_no_context" not in result
    assert "rag_plus_self_check" not in result
    assert "hallucination_relative_drop" not in result
    assert result["data"]["role"] == "internal_eval"


def _real_model_run(
    llm,
    *,
    max_cases: int = 1,
    max_calls: int = 10,
    thinking_mode: str | None = None,
) -> runner.ModelRun:
    return runner.ModelRun(
        mode="real",
        provider="deepseek",
        model="test-model",
        max_cases=max_cases,
        max_calls=max_calls,
        max_output_tokens=64,
        thinking_mode=thinking_mode,
        llm=runner._BoundedLLM(llm, max_calls=max_calls, max_output_tokens=64),
    )


def test_generation_task_does_not_execute_or_claim_other_tasks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "REPORT_DIR", tmp_path / "reports")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("--task generation must not execute another task")

    for name in (
        "eval_intent_rules",
        "eval_intent_overlay",
        "eval_intent_llm",
        "eval_retrieval",
        "eval_epig_policy",
        "eval_e2e",
    ):
        monkeypatch.setattr(runner, name, forbidden)
    monkeypatch.setattr(sys, "argv", ["eval.runner", "--task", "generation"])

    runner.main()

    report = json.loads((tmp_path / "reports" / "latest.json").read_text(encoding="utf-8"))
    assert set(report) == {"generation", "run"}
    run_meta = report["run"]
    assert run_meta["task_coverage"] == {
        "requested": "generation",
        "executed_tasks": ["generation"],
        "not_run_tasks": ["intent", "retrieval", "epig-policy", "e2e"],
        "sections": ["generation"],
    }
    assert run_meta["measured"] == []
    assert run_meta["quality_measured"] is False
    assert "faithfulness" in run_meta["not_measured"]
    assert "real_model_output_quality" in run_meta["not_measured"]


async def test_real_mode_zero_execution_is_not_marked_quality() -> None:
    class _NeverLLM:
        calls = 0

        async def generate(self, *_args, **_kwargs):
            raise AssertionError("no model call should happen with zero cases")

    run = _real_model_run(_NeverLLM(), max_cases=0)
    result = await runner.eval_intent_llm(model_run=run, version="intent_v3", overlay=True)

    assert result["mode"] == "real"
    assert result["attempted_model_calls"] == 0
    assert result["quality_scope"] == "none"
    assert result["quality_measured"] is False
    assert "intent_accuracy" in result["not_measured"]

    run_meta = runner._run_metadata(task="intent", model_run=run, results={"intent_v3": result})
    assert run_meta["mode"] == "real"
    assert run_meta["quality_scope"] == "none"
    assert run_meta["quality_measured"] is False
    assert "intent_v3_real_model_quality" not in run_meta["measured"]


async def test_real_budget_truncation_is_partial_not_full_quality() -> None:
    from app.providers.fake import FakeLLMProvider

    run = _real_model_run(FakeLLMProvider(), max_cases=3, max_calls=1)
    result = await runner.eval_intent_llm(model_run=run, version="intent_v3", overlay=True)

    assert result["budget_exceeded"] is True
    assert result["attempted_model_calls"] >= 2
    assert result["errors"] >= 1
    assert result["quality_scope"] == "partial"
    assert result["quality_measured"] is False

    run_meta = runner._run_metadata(task="intent", model_run=run, results={"intent_v3": result})
    assert run_meta["measured"] == ["intent_v3_partial_model_run"]
    assert run_meta["quality_scope"] == "partial"
    assert run_meta["quality_measured"] is False


async def test_epig_review_only_gating_does_not_claim_model_quality(monkeypatch) -> None:
    from app.providers.fake import FakeLLMProvider

    review_rows = [
        row for row in runner.load_jsonl(runner.EPIG_POLICY_PATH) if row.get("expect_review")
    ]
    assert review_rows
    monkeypatch.setattr(runner, "_limited_rows", lambda path, max_cases: review_rows[:1])
    run = _real_model_run(FakeLLMProvider(), max_cases=1)

    result = await runner.eval_epig_policy_generation(run)

    assert result["mode"] == "real"
    assert result["attempted_model_calls"] == 0
    assert result["quality_scope"] == "none"
    assert result["quality_measured"] is False
    assert result["model_generation_tested"] is False
    assert "model_output_quality" in result["not_measured"]

    run_meta = runner._run_metadata(
        task="epig-policy",
        model_run=run,
        results={"epig_policy_llm": result},
    )
    assert run_meta["measured"] == ["epig_policy_review_gating_only"]
    assert run_meta["quality_scope"] == "none"
    assert run_meta["quality_measured"] is False


def test_run_quality_requires_every_real_section_to_be_full() -> None:
    run = _real_model_run(object())
    full = {
        "mode": "real",
        "quality_scope": "full",
        "quality_measured": True,
        "measurement": "real_model",
        "attempted_model_calls": 1,
        "not_measured": [],
    }
    partial = {
        "mode": "real",
        "quality_scope": "partial",
        "quality_measured": False,
        "measurement": "real_model",
        "attempted_model_calls": 1,
        "not_measured": ["faithfulness"],
    }

    only_full = runner._run_metadata(task="generation", model_run=run, results={"generation": full})
    assert only_full["quality_scope"] == "full"
    assert only_full["quality_measured"] is True

    mixed = runner._run_metadata(
        task="all",
        model_run=run,
        results={"generation": full, "e2e": partial},
    )
    assert mixed["quality_scope"] == "partial"
    assert mixed["quality_measured"] is False


async def test_default_offline_all_does_not_touch_deepseek_get_settings_or_network(
    monkeypatch,
) -> None:
    import app.config.settings as settings_mod
    import app.providers.deepseek as deepseek_mod
    import app.providers.embeddings as embeddings_mod
    import app.retrieval.ingest as ingest_mod

    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline eval must not construct a provider or read get_settings")

    monkeypatch.setattr(deepseek_mod.DeepSeekProvider, "__init__", forbidden)
    monkeypatch.setattr(settings_mod, "get_settings", forbidden)
    monkeypatch.setattr(ingest_mod, "get_settings", forbidden)
    monkeypatch.setattr(
        embeddings_mod.BgeM3EmbeddingProvider,
        "_load_sentence_transformer",
        forbidden,
    )
    monkeypatch.setattr(
        embeddings_mod.OpenAICompatibleEmbeddingProvider,
        "__init__",
        forbidden,
    )
    _pollute_parent_env(monkeypatch)

    report = await runner.run_all()

    assert report["retrieval"]["dense_backend"] == "hash-vector"
    assert report["retrieval"]["cross_encoder_available"] is False
    assert report["generation"]["mode"] == "offline"
    assert report["generation"]["quality_measured"] is False
    assert report["e2e"]["mode"] == "offline"
    assert report["e2e"]["quality_measured"] is False
    assert report["e2e"]["real_tool_trace_measured"] is False

    run_meta = runner._run_metadata(task="all", model_run=None, results=report)
    assert run_meta["quality_measured"] is False
    assert set(run_meta["measured"]) == {
        "intent_rules",
        "intent_overlay",
        "offline_retrieval",
        "offline_graph_plumbing",
    }
    assert "epig_policy_deterministic" not in run_meta["measured"]
    assert run_meta["task_coverage"]["executed_tasks"] == [
        "intent",
        "retrieval",
        "generation",
        "e2e",
    ]
    assert run_meta["task_coverage"]["not_run_tasks"] == ["epig-policy"]
    assert run_meta["task_coverage"]["sections"] == [
        key for key in runner._EVAL_SECTIONS if key in report
    ]

    text = json.dumps(report, ensure_ascii=False)
    assert CANARY not in text
    assert "postgresql" not in text
    assert "redis://" not in text
    assert "https://" not in text
    assert "http://" not in text


async def test_e2e_uses_fresh_temp_sqlite_and_cleans_it_up(monkeypatch) -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    disposed: list[object] = []
    real_dispose = AsyncEngine.dispose

    async def tracked_dispose(self, close: bool = True):
        disposed.append(self)
        return await real_dispose(self, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", tracked_dispose)
    captured: dict[str, str] = {}
    real_settings = runner.offline_settings

    def settings_spy(*, database_url: str = "sqlite+aiosqlite:///:memory:"):
        settings = real_settings(database_url=database_url)
        if "eval.sqlite3" in database_url:
            captured["database_url"] = database_url
        return settings

    monkeypatch.setattr(runner, "offline_settings", settings_spy)
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://{CANARY}:{CANARY}@db.invalid/{CANARY}",
    )

    rows = runner.load_jsonl(runner.E2E_PATH)[:1]
    result = await runner.eval_e2e(rows=rows)

    database_url = captured["database_url"]
    database_path = Path(database_url.removeprefix("sqlite+aiosqlite:///"))
    assert database_path.name == "eval.sqlite3"
    assert database_path.parent.name.startswith("dtc-eval-e2e-")
    assert not database_path.exists()
    assert not database_path.parent.exists()
    assert disposed, "container engine must be disposed"

    assert result["mode"] == "offline"
    assert result["database"] == "fresh-temporary-sqlite"
    assert result["temporary_database_removed"] is True
    assert "tool_path_hit" not in result
    assert result["legacy_answer_keyword_proxy_note"].find("NOT tool-call") >= 0
    assert "postgresql" not in json.dumps(result)


async def test_e2e_cleanup_and_redaction_on_case_exception(monkeypatch) -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    import app.services.chat_service as chat_service_mod

    disposed: list[object] = []
    real_dispose = AsyncEngine.dispose

    async def tracked_dispose(self, close: bool = True):
        disposed.append(self)
        return await real_dispose(self, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", tracked_dispose)
    captured: dict[str, str] = {}
    real_settings = runner.offline_settings

    def settings_spy(*, database_url: str = "sqlite+aiosqlite:///:memory:"):
        settings = real_settings(database_url=database_url)
        if "eval.sqlite3" in database_url:
            captured["database_url"] = database_url
        return settings

    monkeypatch.setattr(runner, "offline_settings", settings_spy)
    secret = f"{CANARY}-secret"
    leaky_error = (
        f"backend failed postgresql://{CANARY}:{secret}@db.invalid/x " "sk-synthetic123456"
    )

    async def failing_chat(self, request, *, trace_id, staff=None):
        raise RuntimeError(leaky_error)

    monkeypatch.setattr(chat_service_mod.ChatService, "chat", failing_chat)

    result = await runner.eval_e2e(rows=runner.load_jsonl(runner.E2E_PATH)[:1])

    assert result["errors"] == 1
    assert result["cases"][0]["error"]
    text = json.dumps(result, ensure_ascii=False)
    assert CANARY not in text
    assert secret not in text
    assert "postgresql://" not in text
    assert "sk-synthetic123456" not in text
    assert "[REDACTED_URL]" in text
    assert disposed, "exception path must still dispose the engine"
    database_path = Path(captured["database_url"].removeprefix("sqlite+aiosqlite:///"))
    assert not database_path.exists()
    assert not database_path.parent.exists()


def _capture_e2e_database(monkeypatch) -> dict[str, str]:
    captured: dict[str, str] = {}
    real_settings = runner.offline_settings

    def settings_spy(*, database_url: str = "sqlite+aiosqlite:///:memory:"):
        settings = real_settings(database_url=database_url)
        if "eval.sqlite3" in database_url:
            captured["database_url"] = database_url
        return settings

    monkeypatch.setattr(runner, "offline_settings", settings_spy)
    return captured


def _track_engine_disposal(monkeypatch) -> list[object]:
    from sqlalchemy.ext.asyncio import AsyncEngine

    disposed: list[object] = []
    real_dispose = AsyncEngine.dispose

    async def tracked_dispose(self, close: bool = True):
        disposed.append(self)
        return await real_dispose(self, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", tracked_dispose)
    return disposed


async def test_e2e_build_failure_disposes_engine_and_removes_tempdir(monkeypatch) -> None:
    import app.services.container as container_mod

    disposed = _track_engine_disposal(monkeypatch)
    captured = _capture_e2e_database(monkeypatch)

    async def fail_retrieval(settings, redis):
        raise RuntimeError("retrieval boom")

    monkeypatch.setattr(container_mod, "build_retrieval", fail_retrieval)

    with pytest.raises(RuntimeError, match="e2e infrastructure failed"):
        await runner.eval_e2e(rows=runner.load_jsonl(runner.E2E_PATH)[:1])

    assert disposed, "engine created before a retrieval failure must be disposed"
    database_path = Path(captured["database_url"].removeprefix("sqlite+aiosqlite:///"))
    assert not database_path.exists()
    assert not database_path.parent.exists()


async def test_e2e_build_cancellation_disposes_engine_and_removes_tempdir(monkeypatch) -> None:
    import app.services.container as container_mod

    disposed = _track_engine_disposal(monkeypatch)
    captured = _capture_e2e_database(monkeypatch)

    async def cancel_retrieval(settings, redis):
        raise asyncio.CancelledError()

    monkeypatch.setattr(container_mod, "build_retrieval", cancel_retrieval)

    with pytest.raises(asyncio.CancelledError):
        await runner.eval_e2e(rows=runner.load_jsonl(runner.E2E_PATH)[:1])

    assert disposed, "engine created before cancellation must be disposed"
    database_path = Path(captured["database_url"].removeprefix("sqlite+aiosqlite:///"))
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_provenance_hashes_allowlisted_sources_without_leaking_content(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runner, "_git", lambda root, *args: None)
    (tmp_path / "app").mkdir()
    (tmp_path / "eval").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "uv.lock").write_text("lock-v1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    source = tmp_path / "app" / "service.py"
    source.write_text(f"TOKEN = '{CANARY}'\n", encoding="utf-8")
    (tmp_path / "eval" / "runner.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "frontend" / "unrelated.ts").write_text(CANARY, encoding="utf-8")

    first = runner._provenance(tmp_path)
    assert first["candidate_commit"] == "unknown"
    assert first["worktree_dirty"] == "unknown"
    assert first["dirty_scopes"] == "unknown"
    assert first["source"]["app"]["files"] == 1
    assert first["source"]["app"]["dirty"] == "unknown"
    assert first["source"]["eval"]["files"] == 1
    assert first["dependency_lock"]["sha256"]
    assert first["dependency_lock"]["dirty"] == "unknown"

    source.write_text("TOKEN = 'changed'\n", encoding="utf-8")
    second = runner._provenance(tmp_path)

    assert second["source"]["app"]["sha256"] != first["source"]["app"]["sha256"]
    rendered = json.dumps(second, ensure_ascii=False)
    assert CANARY not in rendered
    assert "service.py" not in rendered
    assert "frontend" not in rendered
    assert "unrelated.ts" not in rendered


def test_dirty_scopes_ignore_unrelated_frontend_changes(tmp_path: Path, monkeypatch) -> None:
    class _GitResult:
        returncode = 0
        stdout = (
            " M eval/runner.py\n"
            " M frontend/src/unrelated.ts\n"
            " M uv.lock\n"
            " M pyproject.toml\n"
        )

    monkeypatch.setattr(runner, "_git", lambda root, *args: _GitResult())

    assert runner._dirty_scopes(tmp_path) == ["config", "dependencies", "eval"]


def test_llm_alone_fails_before_provider_or_db(monkeypatch) -> None:
    import app.providers.deepseek as deepseek_mod

    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider or temp DB must not be created")

    monkeypatch.setattr(sys, "argv", ["eval.runner", "--task", "e2e", "--llm"])
    monkeypatch.setenv("DTC_EVAL_DEEPSEEK_API_KEY", f"{CANARY}-would-be-key")
    monkeypatch.setattr(deepseek_mod.DeepSeekProvider, "__init__", forbidden)
    monkeypatch.setattr(runner.tempfile, "TemporaryDirectory", forbidden)

    with pytest.raises(SystemExit) as excinfo:
        runner.main()

    assert "allow-real-model" in str(excinfo.value)


def test_real_mode_missing_dtc_key_fails_before_provider_or_db(monkeypatch) -> None:
    import app.providers.deepseek as deepseek_mod

    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider or temp DB must not be created")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval.runner",
            "--task",
            "e2e",
            "--allow-real-model",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-flash",
            "--max-cases",
            "1",
            "--max-calls",
            "1",
        ],
    )
    monkeypatch.delenv("DTC_EVAL_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", f"{CANARY}-inherited-key")
    monkeypatch.setattr(deepseek_mod.DeepSeekProvider, "__init__", forbidden)
    monkeypatch.setattr(runner.tempfile, "TemporaryDirectory", forbidden)

    with pytest.raises(SystemExit) as excinfo:
        runner.main()

    message = str(excinfo.value)
    assert "DTC_EVAL_DEEPSEEK_API_KEY" in message
    assert CANARY not in message


def test_real_mode_requires_explicit_limits(monkeypatch) -> None:
    import app.providers.deepseek as deepseek_mod

    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider must not be created before limits are valid")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval.runner",
            "--task",
            "generation",
            "--allow-real-model",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-flash",
            "--max-calls",
            "1",
        ],
    )
    monkeypatch.setattr(deepseek_mod.DeepSeekProvider, "__init__", forbidden)

    with pytest.raises(SystemExit) as excinfo:
        runner.main()

    assert "--max-cases" in str(excinfo.value)


class _AnswerLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, **kwargs):
        self.calls += 1
        return LLMResponse(
            content='{"answer":"ok","citation_chunk_ids":[]}',
            usage=TokenUsage(total_tokens=4),
            model="fake",
        )


async def test_injected_fake_generation_reports_plumbing_not_quality() -> None:
    llm = _AnswerLLM()
    run = runner.fake_model_run(llm, max_cases=2)
    result = await runner.eval_generation(run)

    assert result["mode"] == "fake"
    assert result["quality_measured"] is False
    assert result["model_generation_tested"] is True
    assert result["faithfulness_tested"] is False
    assert result["attempted_model_calls"] == 2
    assert "naive_no_context" not in result
    assert "hallucination_relative_drop" not in result
    assert "faithfulness" in result["not_measured"]


async def test_policy_34_stays_a_development_regression_set() -> None:
    result = await runner.eval_epig_policy()
    rows = runner.load_jsonl(runner.EPIG_POLICY_PATH)

    assert len(rows) == 34
    assert result["n"] == 34
    assert result["passed"] == 34
    assert [case["id"] for case in result["cases"]] == [row["id"] for row in rows]
    assert result["data"]["role"] == "development_regression"
    assert result["model_generation_tested"] is False
    assert "holdout" not in json.dumps(result, ensure_ascii=False)


def _real_args(**overrides) -> argparse.Namespace:
    values = {
        "llm": False,
        "allow_real_model": True,
        "task": "generation",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "max_cases": 1,
        "max_calls": 1,
        "max_output_tokens": 128,
        "thinking_mode": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    ("cli_mode", "expected"),
    [
        (None, "disabled"),
        ("disabled", "disabled"),
        ("enabled", "enabled"),
        ("unspecified", None),
    ],
)
def test_real_mode_thinking_mode_defaults_to_disabled_and_ignores_env(
    monkeypatch, cli_mode: str | None, expected: str | None
) -> None:
    monkeypatch.setenv("DTC_EVAL_DEEPSEEK_API_KEY", f"{CANARY}-eval-key")
    monkeypatch.setenv("DEEPSEEK_THINKING_MODE", "enabled")

    config = runner._real_model_config_from_args(_real_args(thinking_mode=cli_mode))

    assert config is not None
    assert config.thinking_mode == expected


def test_thinking_mode_flag_requires_the_explicit_real_path(monkeypatch) -> None:
    monkeypatch.setenv("DTC_EVAL_DEEPSEEK_API_KEY", f"{CANARY}-eval-key")

    with pytest.raises(SystemExit) as excinfo:
        runner._real_model_config_from_args(
            _real_args(allow_real_model=False, task="retrieval", thinking_mode="enabled")
        )

    assert "--allow-real-model" in str(excinfo.value)


def test_run_metadata_declares_the_actual_thinking_mode() -> None:
    disabled = _real_model_run(_AnswerLLM(), thinking_mode="disabled")
    provider_default = _real_model_run(_AnswerLLM())

    assert runner._thinking_mode_meta(disabled) == "disabled"
    assert runner._model_meta(disabled)["thinking_mode"] == "disabled"
    assert (
        runner._run_metadata(task="generation", model_run=disabled, results={})["thinking_mode"]
        == "disabled"
    )
    assert runner._thinking_mode_meta(provider_default) == "provider_default"
    assert runner._thinking_mode_meta(None) == "not_applicable"
    assert runner._model_meta(None)["thinking_mode"] == "not_applicable"


async def test_build_model_run_applies_cli_mode_without_reading_global_env(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_THINKING_MODE", "enabled")
    config = runner.RealModelConfig(
        provider="deepseek",
        model="deepseek-flash",
        max_cases=1,
        max_calls=1,
        max_output_tokens=16,
        thinking_mode="disabled",
        api_key=f"{CANARY}-eval-key",
        base_url="https://api.deepseek.com",
    )

    run = runner._build_model_run(config)
    try:
        assert run.thinking_mode == "disabled"
        assert run.llm._provider._settings.deepseek_thinking_mode == "disabled"
    finally:
        await run.llm.aclose()
