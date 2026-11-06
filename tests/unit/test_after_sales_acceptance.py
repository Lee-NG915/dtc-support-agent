"""Focused tests for the sealed after-sales acceptance runner.

These tests are offline/Fake-only. They never read ``.env.eval.local``, never
reach the network and never start a paid model. The real 12-case run is a
separate, explicitly authorized operator action.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

import eval.after_sales_acceptance as acceptance
from app.providers.base import LLMResponse
from app.providers.fake import FakeLLMProvider
from app.schemas.common import TokenUsage
from eval.runner import ModelBudgetExceeded, ModelRun

DEMO_LOGIN_FILES = {
    "app/api/dependencies.py",
    "app/api/routes/after_sales.py",
    "app/config/settings.py",
    "app/core/security.py",
    "app/repositories/staff_repository.py",
    "app/schemas/after_sales.py",
}

QUALITY_REFREEZE_FILES = {
    "app/agents/nodes/generate.py",
    "app/agents/nodes/retrieval.py",
    "app/retrieval/bm25.py",
    "app/retrieval/hybrid.py",
    "app/retrieval/policy.py",
    "app/services/after_sales/service.py",
}


class _EchoProvider:
    def __init__(self) -> None:
        self.max_tokens: list[int] = []

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResponse:
        self.max_tokens.append(max_tokens)
        return LLMResponse(
            content='{"ok": true}',
            usage=TokenUsage(total_tokens=3),
            model="echo",
        )

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _fake_model_run(provider: object, *, max_calls: int = 10) -> ModelRun:
    return ModelRun(
        mode="fake",
        provider="fake",
        model="fake",
        max_cases=12,
        max_calls=max_calls,
        max_output_tokens=100,
        llm=acceptance.RecordingBoundedLLM(provider, max_calls=max_calls, max_output_tokens=100),
    )


class _ValidSuggestionProvider:
    """Return a schema-valid suggestion citing one supplied trusted chunk."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResponse:
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        chunk_id = payload["policy"]["chunks"][0]["chunk_id"]
        total_display = payload["order_amounts"]["total_display"]
        return LLMResponse(
            content=json.dumps(
                {
                    "action": "other",
                    "internal_summary": f"Synthetic fixture-valid suggestion; {total_display}.",
                    "consumer_reply": "Synthetic fixture-valid reply.",
                    "evidence_chunk_ids": [chunk_id],
                },
                ensure_ascii=False,
            ),
            usage=TokenUsage(total_tokens=3),
            model="fake-valid",
        )

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------- freeze


def test_verify_frozen_current_and_tamper_detection(tmp_path: Path) -> None:
    artifacts = acceptance.verify_frozen()
    assert artifacts.freeze_count == 203
    assert artifacts.freeze_revision == 5
    assert len(artifacts.rows) == 12

    dataset_copy = tmp_path / "after_sales_holdout_v1.jsonl"
    dataset_copy.write_bytes(acceptance.DATASET_PATH.read_bytes() + b"\n")
    with pytest.raises(acceptance.FreezeVerificationError, match="dataset SHA-256"):
        acceptance.verify_frozen(
            dataset_path=dataset_copy,
            manifest_path=acceptance.MANIFEST_PATH,
        )

    manifest = json.loads(acceptance.MANIFEST_PATH.read_text(encoding="utf-8"))
    first_path = sorted(manifest["freeze_hashes"])[0]
    manifest["freeze_hashes"][first_path] = "0" * 64
    tampered_manifest = tmp_path / "tampered_manifest.json"
    tampered_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(acceptance.FreezeVerificationError, match="frozen source changed"):
        acceptance.verify_frozen(
            dataset_path=acceptance.DATASET_PATH,
            manifest_path=tampered_manifest,
        )


def test_revision_3_engineering_refreeze_metadata_is_exact(tmp_path: Path) -> None:
    artifacts = acceptance.verify_frozen()
    manifest = artifacts.manifest

    assert artifacts.freeze_revision == 5
    assert manifest["freeze_revision"] == 5
    assert manifest["state"] == "frozen_prepared_not_run"
    assert artifacts.freeze_count == 203
    assert len(manifest["freeze_hashes"]) == 203

    # The engineering re-freeze must not touch the frozen dataset.
    assert manifest["dataset_sha256"] == (
        "ae13159484b31dde3bc6c63db9060169b2b6971b002d8b03e569c62707c256da"
    )
    assert artifacts.dataset_sha256 == manifest["dataset_sha256"]

    refreeze = manifest["engineering_refreeze"]
    assert refreeze["parent_revision"] == 2
    assert refreeze["parent_source_commit"] == "19f1d4550f532a73f56da9e4a4b830a759acc768"
    assert refreeze["parent_manifest_sha256"] == (
        "a02646d5b2ec4c4b7c381012e96d618ab87d832b49bc780cbd17b81b1e627ebc"
    )
    assert refreeze["model_evaluation_status"] == "not_run_for_revision_3"
    assert set(refreeze["files"]) == {"app/connectors/epig.py", "app/connectors/fake.py"}

    expected = {
        "app/connectors/epig.py": (
            "55619e44c0cc860630638aaac7a7888e3f18b60984372c79c7e7cecf538c1bb3",
            "6d33d802618f0dcf33a5540968704495b9c1b17ae19cd865a8d13e18a99ae3ce",
        ),
        "app/connectors/fake.py": (
            "4705e6a8088ec29c44d40ca8e1fa039968311067f5d354458fc591c111f8765f",
            "dac7a90ef1882dde32ec3d1715bab05c455f193ab8d46fca3dc21cb31d826f2c",
        ),
    }
    for relative, (old_sha256, new_sha256) in expected.items():
        assert refreeze["files"][relative] == {
            "old_sha256": old_sha256,
            "new_sha256": new_sha256,
        }
        assert manifest["freeze_hashes"][relative] == new_sha256

    # Every frozen entry outside the later re-freezes stays byte-identical to
    # revision 2. This keeps each later re-freeze from widening the revision-3
    # change set.
    touched = set(expected) | DEMO_LOGIN_FILES | QUALITY_REFREEZE_FILES
    unchanged = sorted(
        (path, digest) for path, digest in manifest["freeze_hashes"].items() if path not in touched
    )
    unchanged_digest = hashlib.sha256(
        json.dumps(unchanged, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert unchanged_digest == ("fd7f1991584e2722d703015f2895c1fcbd4dd69ee1f6bd472bf22c38491235b8")

    # Prior re-freeze history remains available rather than being rewritten.
    assert manifest["protocol_refreeze"]["files"]
    assert manifest["fixture_protocol_refreeze"]["prior_fixture_invalid_cases"] == [
        "H07",
        "H08",
    ]

    # The stricter guard is not weakened: revision 2 is now rejected.
    stale = json.loads(json.dumps(manifest))
    stale["freeze_revision"] = 2
    stale_manifest = tmp_path / "stale_revision_manifest.json"
    stale_manifest.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(acceptance.FreezeVerificationError, match="freeze revision"):
        acceptance.verify_frozen(
            dataset_path=acceptance.DATASET_PATH,
            manifest_path=stale_manifest,
        )


def test_revision_4_demo_login_refreeze_metadata_is_exact(tmp_path: Path) -> None:
    artifacts = acceptance.verify_frozen()
    manifest = artifacts.manifest

    assert artifacts.freeze_revision == 5
    assert manifest["freeze_revision"] == 5
    assert manifest["state"] == "frozen_prepared_not_run"
    assert artifacts.freeze_count == 203
    assert len(manifest["freeze_hashes"]) == 203
    assert manifest["dataset_sha256"] == (
        "ae13159484b31dde3bc6c63db9060169b2b6971b002d8b03e569c62707c256da"
    )
    assert artifacts.dataset_sha256 == manifest["dataset_sha256"]

    refreeze = manifest["demo_login_refreeze"]
    assert refreeze["parent_revision"] == 3
    assert refreeze["parent_source_commit"] == "c2ba24ed92c68de4d5f53dbc23da12be1144bafb"
    assert refreeze["parent_manifest_sha256"] == (
        "fe94dbdc60c9e7e59008b997015dd14458110e1bdbad5d2ec74c3273752f66f9"
    )
    assert refreeze["model_evaluation_status"] == "not_run_for_revision_4"
    assert set(refreeze["files"]) == DEMO_LOGIN_FILES

    expected = {
        "app/api/dependencies.py": (
            "b710222a8b1afdf4bc1995125f5963ea482809e17a2f5b7b28d0187664be73ee",
            "deefba49a3f41d7d40917cd63f231daecf8287d0a8ffc5cad56f20929352f144",
        ),
        "app/api/routes/after_sales.py": (
            "db4afd5f2fb198bbaf963ec3b20065aaf7b230ace45cda5f9a648fadee0f9c94",
            "82473b996db436f4d02539cb91286c2adfc3188f1fd987fdfd01dfe9e7f5db40",
        ),
        "app/config/settings.py": (
            "c2575d68f5a4b684af189e39a11411e0dd770e6d7708e0cf799017e7cb41f822",
            "3ff56181248ec599e7cd631aa384ac4c3d8ee358fa91c850e664403c62d630d7",
        ),
        "app/core/security.py": (
            "1439f66f1efc75e208e1281c2071c4c8487923b99e8db1c00072a8a623f0efbe",
            "24860c4375ca7507485ebdf0926e3f6913985358d1fd3520874267c3557aeb9d",
        ),
        "app/repositories/staff_repository.py": (
            "dcb0d6e52f251a127edbd2e8ad0ae0a93e4fa0e6a15a0178a2dc065f53a1a411",
            "7b312d77e2409f5a6a2dc46ee53e719e82b77561377026b5156234b9bf36afcf",
        ),
        "app/schemas/after_sales.py": (
            "b17ab2502133c9fe697024a81d0e149286cc9e2c94f3b184a59fae0ad909e991",
            "ca61dea1661a0d2ea5aadb5b30740d0d7f9134b6e82d6fae45d6ee818319ae2b",
        ),
    }
    for relative, (old_sha256, new_sha256) in expected.items():
        assert refreeze["files"][relative] == {
            "old_sha256": old_sha256,
            "new_sha256": new_sha256,
        }
        assert manifest["freeze_hashes"][relative] == new_sha256

    # Every other entry stays byte-identical to revision 3.
    unchanged = sorted(
        (path, digest)
        for path, digest in manifest["freeze_hashes"].items()
        if path not in DEMO_LOGIN_FILES | QUALITY_REFREEZE_FILES
    )
    unchanged_digest = hashlib.sha256(
        json.dumps(unchanged, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert unchanged_digest == "dd6f8c903334144c09fffb330cb6e0d2ada1584f63bccd1cf3b2e125a3dc668c"

    # Earlier refreeze history remains available rather than being rewritten.
    assert manifest["engineering_refreeze"]["files"]
    assert manifest["protocol_refreeze"]["files"]
    assert manifest["fixture_protocol_refreeze"]["prior_fixture_invalid_cases"] == [
        "H07",
        "H08",
    ]

    # Revision 3 is now rejected as stale.
    stale = json.loads(json.dumps(manifest))
    stale["freeze_revision"] = 3
    stale_manifest = tmp_path / "stale_revision_manifest.json"
    stale_manifest.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(acceptance.FreezeVerificationError, match="freeze revision"):
        acceptance.verify_frozen(
            dataset_path=acceptance.DATASET_PATH,
            manifest_path=stale_manifest,
        )


def test_revision_5_h05_h08_quality_refreeze_metadata_is_exact(tmp_path: Path) -> None:
    artifacts = acceptance.verify_frozen()
    manifest = artifacts.manifest

    assert artifacts.freeze_revision == 5
    assert manifest["freeze_revision"] == 5
    assert manifest["state"] == "frozen_prepared_not_run"
    assert artifacts.freeze_count == 203
    assert len(manifest["freeze_hashes"]) == 203
    assert manifest["dataset_sha256"] == (
        "ae13159484b31dde3bc6c63db9060169b2b6971b002d8b03e569c62707c256da"
    )
    assert artifacts.dataset_sha256 == manifest["dataset_sha256"]

    refreeze = manifest["quality_refreeze"]
    assert refreeze["parent_revision"] == 4
    assert refreeze["parent_source_commit"] == "4c218ac14a5ca4cc260d0691e9e615e77cae1e30"
    assert refreeze["parent_manifest_sha256"] == (
        "281bcbafc076b2917a732a308f7b9e6cd80efd7d6722724f7c50d646e5b7e51e"
    )
    assert refreeze["model_evaluation_status"] == "not_run_for_revision_5"
    assert set(refreeze["files"]) == QUALITY_REFREEZE_FILES

    expected = {
        "app/agents/nodes/generate.py": (
            "07888a1ef43b0d4440bb41f55c263aefd3f29259696571ff80620ba5e7d1d201",
            "7a782bbc292b59b93c869eafaada30058e7f60d353546e3d149e312f7b959fc6",
        ),
        "app/agents/nodes/retrieval.py": (
            "324842136174e2fff8d8cc9e94006931337e4e571adb4f6730a8ed6088f3e81b",
            "9e0fd9cfd0e40da8526c75e54e98e30772fa4c53a845ef2948ec7ce74341ca16",
        ),
        "app/retrieval/bm25.py": (
            "1f51eebaa5aca38997db7d8f163d74987a9ce5ce306dd90277e06eff0a94a87a",
            "412b08d1fe9db910655cc97f417dae0e9e8acac7ebef38e865d79598bbcab7ad",
        ),
        "app/retrieval/hybrid.py": (
            "d8caa412aa6bb337e58ab4902968c1b4f640bf4105585ff68c003a822f35badb",
            "29633a41ab16555e391bebf53933d6bb68a2402b32b1014ca53cbe4636d340d2",
        ),
        "app/retrieval/policy.py": (
            "4c05b14262b67a289a4c7673ce19d5b1586f2ef76c83a658d6ad95884caf2a4b",
            "7c2a202dc53cd71726623e151f12d9a0f9a0ef4db3177c10aba1e789efe58f39",
        ),
        "app/services/after_sales/service.py": (
            "5a72aa4534b29284e43ddeb06d1ba0706ec17413d96100350137bb25bf4a2cdc",
            "c3f200c61c1fef5d6e99adafb65ff28ba2284ba35b6091cfd04a28f1d7d035e2",
        ),
    }
    for relative, (old_sha256, new_sha256) in expected.items():
        assert refreeze["files"][relative] == {
            "old_sha256": old_sha256,
            "new_sha256": new_sha256,
        }
        assert manifest["freeze_hashes"][relative] == new_sha256

    # Every other entry stays byte-identical to revision 4.
    unchanged = sorted(
        (path, digest)
        for path, digest in manifest["freeze_hashes"].items()
        if path not in QUALITY_REFREEZE_FILES
    )
    unchanged_digest = hashlib.sha256(
        json.dumps(unchanged, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert unchanged_digest == "135c08360e183a07936a99adaeb746d62ee28134fa3c79aca864f9bc3faa83ec"

    # Prior protocol, fixture, engineering and demo-login history is preserved.
    assert manifest["protocol_refreeze"]["files"]
    assert manifest["fixture_protocol_refreeze"]["prior_fixture_invalid_cases"] == [
        "H07",
        "H08",
    ]
    assert manifest["engineering_refreeze"]["files"]
    assert manifest["demo_login_refreeze"]["files"]

    # Revision 4 is now rejected as stale.
    stale = json.loads(json.dumps(manifest))
    stale["freeze_revision"] = 4
    stale_manifest = tmp_path / "stale_revision_manifest.json"
    stale_manifest.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(acceptance.FreezeVerificationError, match="freeze revision"):
        acceptance.verify_frozen(
            dataset_path=acceptance.DATASET_PATH,
            manifest_path=stale_manifest,
        )


def test_verify_frozen_rejects_unsafe_path(tmp_path: Path) -> None:
    manifest = json.loads(acceptance.MANIFEST_PATH.read_text(encoding="utf-8"))
    first_path = sorted(manifest["freeze_hashes"])[0]
    manifest["freeze_hashes"]["../escape.txt"] = manifest["freeze_hashes"].pop(first_path)
    tampered_manifest = tmp_path / "unsafe_manifest.json"
    tampered_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(acceptance.FreezeVerificationError, match="unsafe path"):
        acceptance.verify_frozen(
            dataset_path=acceptance.DATASET_PATH,
            manifest_path=tampered_manifest,
        )


def test_freeze_failure_happens_before_credentials_or_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail_verify(**_kwargs: object) -> object:
        raise acceptance.FreezeVerificationError("forced freeze mismatch")

    def _must_not_load() -> object:
        raise AssertionError("credentials must not be read after a freeze failure")

    monkeypatch.setattr(acceptance, "verify_frozen", _fail_verify)
    monkeypatch.setattr(acceptance, "load_dedicated_eval_env", _must_not_load)
    code = acceptance.main(
        [
            "--allow-real-model",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-flash",
            "--max-cases",
            "12",
            "--max-calls",
            "1",
            "--max-output-tokens",
            "1200",
            "--thinking-mode",
            "disabled",
            "--report-dir",
            str(tmp_path),
        ]
    )
    assert code == 2
    reports = list(tmp_path.glob("after_sales_acceptance_*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["status"] == "freeze_mismatch"
    assert report["cases"] == []


# ------------------------------------------------------------ options/budgets


def test_real_options_require_all_12_cases_and_bounded_limits() -> None:
    acceptance.validate_real_options(
        allow_real_model=True,
        provider="deepseek",
        model="deepseek-flash",
        max_cases=12,
        max_calls=64,
        max_output_tokens=1200,
        thinking_mode="disabled",
    )
    for kwargs in (
        {"max_cases": 11},
        {"max_calls": 65},
        {"max_output_tokens": 1201},
        {"thinking_mode": "enabled"},
        {"provider": "other"},
        {"allow_real_model": False},
    ):
        values = {
            "allow_real_model": True,
            "provider": "deepseek",
            "model": "deepseek-flash",
            "max_cases": 12,
            "max_calls": 64,
            "max_output_tokens": 1200,
            "thinking_mode": "disabled",
        }
        values.update(kwargs)
        with pytest.raises(acceptance.ConfigurationError):
            acceptance.validate_real_options(**values)


def test_bounded_llm_enforces_call_and_output_limits() -> None:
    provider = _EchoProvider()
    llm = acceptance.RecordingBoundedLLM(provider, max_calls=1, max_output_tokens=7)
    response = asyncio.run(llm.generate([], max_tokens=100))
    assert response.model == "echo"
    assert provider.max_tokens == [7]
    with pytest.raises(ModelBudgetExceeded):
        asyncio.run(llm.generate([], max_tokens=100))
    assert len(llm.records) == 2
    assert llm.records[-1]["error_type"] == "ModelBudgetExceeded"
    assert llm.records[-1]["stage"] == "other"


# --------------------------------------------------------------- credential env


def test_dedicated_env_loader_is_0600_and_dtc_eval_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / ".env.eval.local"
    env_path.write_text(
        "DEEPSEEK_API_KEY=ignored-parent-key\n"
        "DTC_EVAL_DEEPSEEK_API_KEY=fake-dedicated-key\n"
        "DATABASE_URL=postgresql+asyncpg://ignored/db\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o644)
    monkeypatch.setattr(acceptance, "DEDICATED_ENV_PATH", env_path)
    with pytest.raises(acceptance.ConfigurationError, match="0600"):
        acceptance.load_dedicated_eval_env(env_path)

    os.chmod(env_path, 0o600)
    loaded = acceptance.load_dedicated_eval_env(env_path)
    assert loaded.api_key == "fake-dedicated-key"
    assert loaded.base_url == acceptance.OFFICIAL_BASE_URL
    public = json.dumps(loaded.public())
    assert "fake-dedicated-key" not in public
    assert "ignored-parent-key" not in public

    env_path.write_text(
        "DTC_EVAL_DEEPSEEK_API_KEY=fake-dedicated-key\n"
        "DTC_EVAL_DEEPSEEK_BASE_URL=https://example.invalid\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)
    with pytest.raises(acceptance.ConfigurationError, match="official"):
        acceptance.load_dedicated_eval_env(env_path)


def test_child_environment_allowlist_excludes_parent_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DTC_EVAL_DEEPSEEK_API_KEY", "parent-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "parent-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://parent/secret")
    monkeypatch.setenv("EPIG_SERVICE_TOKEN", "parent-secret")
    env = acceptance._child_env(tmp_path / "app.db", "{}")
    assert "DTC_EVAL_DEEPSEEK_API_KEY" not in env
    assert "EPIG_SERVICE_TOKEN" not in env
    assert env["DEEPSEEK_API_KEY"] == ""
    assert env["DATABASE_URL"].startswith("sqlite+aiosqlite:///")
    assert env["APP_ENV"] == "testing"
    assert env["PYTHONPATH"] == str(acceptance.ROOT)
    assert "parent-secret" not in json.dumps(env)


# --------------------------------------------------------------- offline guards


def test_offline_run_executes_four_guards_and_marks_models_not_run(
    tmp_path: Path,
) -> None:
    outcome = acceptance.run_acceptance(mode="offline", report_dir=tmp_path)
    assert outcome.exit_code == 0
    assert outcome.report_path.is_file()
    report = outcome.report
    assert report["status"] == "completed"
    assert report["mode"] == "offline"
    assert report["model"]["lane"] == "not_run"
    assert report["model"]["credential_source"] == "not_read"
    assert report["cleanup"]["temp_work_dir_removed"] is True
    assert report["summary"]["no_combined_llm_score"] is True

    cases = {case["id"]: case for case in report["cases"]}
    assert list(cases) == acceptance.EXPECTED_CASE_IDS
    for case_id in acceptance.EXPECTED_CASE_IDS[:8]:
        assert cases[case_id]["status"] == "not_run"
        assert cases[case_id]["reason"] == "offline_model_not_requested"
        assert cases[case_id]["model"]["calls"] == 0
    for case_id in acceptance.EXPECTED_CASE_IDS[8:]:
        assert cases[case_id]["status"] == "guard_passed", cases[case_id]
        assert cases[case_id]["model"]["calls"] == 0
        assert cases[case_id]["score_scope"] == "engineering_only"

    engineering = report["summary"]["engineering_guards"]
    assert engineering == {
        "total": 4,
        "guard_passed": 4,
        "guard_failed": 0,
        "not_run": 0,
    }
    assert report["summary"]["model_cases"]["not_run"] == 8
    assert report["summary"]["model_calls"]["actual_provider_calls"] == 0

    h12 = cases["H12"]
    assert h12["restart"]["method"] == "uvicorn_subprocess_sigterm_restart"
    assert h12["restart"]["second_pid"] != h12["restart"]["first_pid"]
    assert h12["machine_checks"]["api_restarted_new_pid"] is True
    assert h12["process_cleanup"]["all_stopped"] is True
    assert h12["machine_checks"]["case_completed"] is True
    assert h12["machine_checks"]["one_operation"] is True
    assert h12["machine_checks"]["approval_consumed"] is True

    assert cases["H09"]["fixture"]["facts"]["category_known"] is False
    assert cases["H09"]["fixture"]["facts"]["sku"] == "SYNTH-UNSUPPORTED-009"
    assert cases["H09"]["fixture"]["ok"] is True
    assert cases["H10"]["fixture"]["facts"]["sku"] == "SYNTH-CUP-010"
    assert cases["H10"]["fixture"]["facts"]["category"] == "homewares"
    assert cases["H10"]["fixture"]["ok"] is True
    for case_id in ("H11", "H12"):
        fixture = cases[case_id]["fixture"]
        assert fixture["ok"] is True
        assert fixture["facts"]["material_revision"] >= 2
        assert len(fixture["facts"]["materials"]) >= 2
        assert fixture["facts"]["category_source"] == "server_config"
    assert h12["fixture"]["facts"]["sku"] == "SYNTH-MUG-001"
    assert h12["fixture"]["facts"]["category"] == "homewares"
    assert h12["fixture"]["facts"]["description_required_from_query"] is False

    serialized = json.dumps(report, ensure_ascii=False)
    assert "DTC_EVAL_DEEPSEEK_API_KEY" not in serialized
    assert "sk-" not in serialized
    assert "Bearer " not in serialized


# --------------------------------------------------------------- model plumbing


def test_policy_graph_makes_nonzero_calls_without_claiming_quality(
    tmp_path: Path,
) -> None:
    artifacts = acceptance.verify_frozen()
    row = next(row for row in artifacts.rows if row["id"] == "H01")
    model_run = _fake_model_run(FakeLLMProvider())
    try:
        results = asyncio.run(acceptance._run_model_cases([row], model_run, tmp_path))
    finally:
        asyncio.run(acceptance._close_model_run(model_run))
    assert len(results) == 1
    result = results[0]
    assert result["status"] == "model_case_observed"
    assert result["staff_mode"] is True
    assert result["model"]["actual_provider_calls"] > 0
    assert "intent" in result["model"]["stages"]
    assert "answer_generation" in result["model"]["stages"]
    assert result["generation_stage"] == "model_answer_generation_attempted"
    assert not (set(result["registry_tools"]) & acceptance.DEMO_WRITE_TOOLS)
    assert result["boundary"] == acceptance.BOUNDARY_POLICY
    assert result["status"] != "passed"
    assert result["manual_review"]["status"] == "pending_codex"


@pytest.mark.parametrize(
    "bad_output",
    [
        json.dumps(
            {
                "action": "reship",
                "internal_summary": "synthetic",
                "consumer_reply": "synthetic",
                "evidence_chunk_ids": ["citation-admin-override"],
            }
        ),
        "not-json",
    ],
)
def test_suggestion_bad_json_or_citation_fails_closed_without_retry(
    tmp_path: Path, bad_output: str
) -> None:
    artifacts = acceptance.verify_frozen()
    row = next(row for row in artifacts.rows if row["id"] == "H08")
    provider = FakeLLMProvider({"床垫": bad_output})
    model_run = _fake_model_run(provider, max_calls=5)
    try:
        batch = asyncio.run(acceptance._run_suggestion_cases([row], model_run, tmp_path))
    finally:
        asyncio.run(acceptance._close_model_run(model_run))
    assert batch.stop_reason is None
    result = batch.results[0]
    assert result["status"] == "model_case_failed_closed"
    assert result["http_status"] == 502
    assert result["error_code"] == "LLM_OUTPUT_INVALID"
    assert result["candidate"] is None
    assert result["deltas"] == {
        "drafts": 0,
        "approvals": 0,
        "operations": 0,
        "business_create_calls": 0,
    }
    assert result["model"]["actual_provider_calls"] == 1
    assert result["model"]["successful_calls"] == 1


# --------------------------------------------------------------- fixture contract


def _fixture_row(case_id: str) -> dict[str, object]:
    return next(row for row in acceptance.verify_frozen().rows if row["id"] == case_id)


def test_fixture_validation_rejects_legacy_mug_as_mattress() -> None:
    row = _fixture_row("H08")
    required = acceptance._required_fixture_for_row(row)
    legacy_detail = {
        "market": "sg",
        "order_id": "ord_sg_00000042",
        "request_id": "asr_000000000042",
        "request_type": "damaged",
        "description": row["query"],
        "material_revision": 0,
        "order": {
            "order_id": "ord_sg_00000042",
            "market": "sg",
            "order_version": "ov_000000000007",
            "items": [
                {
                    "sku": "SYNTH-MUG-001",
                    "name": "Synthetic ceramic mug",
                    "quantity": 1,
                }
            ],
        },
        "materials": [],
        "drafts": [],
        "approvals": [],
        "operations": [],
    }
    legacy_evidence = {
        "trusted_categories": [
            {"sku": "SYNTH-MUG-001", "category": "mattress", "source": "server_config"}
        ],
        "missing_reasons": [],
    }
    validation = acceptance._validate_fixture(legacy_detail, legacy_evidence, required)
    assert validation.ok is False
    assert "order_sku_mismatch" in validation.errors
    assert "order_name_mismatch" in validation.errors
    assert "material_missing:manual_verification" in validation.errors
    assert "manual_verification_missing" in validation.errors
    assert "material_revision_incomplete" in validation.errors


def test_fixture_validation_rejects_h07_incomplete_materials() -> None:
    row = _fixture_row("H07")
    required = acceptance._required_fixture_for_row(row)
    detail = {
        "market": "au",
        "order_id": "ord_au_00000042",
        "request_id": "asr_000000000042",
        "request_type": "damaged",
        "description": row["query"],
        "material_revision": 0,
        "order": {
            "order_id": "ord_au_00000042",
            "market": "au",
            "order_version": "ov_000000000007",
            "items": [
                {
                    "sku": "SYNTH-CUP-007",
                    "name": "Synthetic ceramic cup",
                    "quantity": 1,
                }
            ],
        },
        "materials": [],
        "drafts": [],
        "approvals": [],
        "operations": [],
    }
    evidence = {
        "trusted_categories": [
            {"sku": "SYNTH-CUP-007", "category": "homewares", "source": "server_config"}
        ],
        "missing_reasons": [],
    }
    validation = acceptance._validate_fixture(detail, evidence, required)
    assert validation.ok is False
    assert "order_sku_mismatch" not in validation.errors
    assert "trusted_category_mismatch" not in validation.errors
    assert "material_missing:manual_verification" in validation.errors
    assert "manual_verification_missing" in validation.errors


def test_legacy_fixture_is_rejected_before_any_suggestion_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _fixture_row("H08")
    legacy = acceptance._SyntheticFixture(
        market="sg",
        sku="SYNTH-MUG-001",
        name="Synthetic ceramic mug",
        category="mattress",
        request_type="damaged",
        description=str(row["query"]),
        materials=(),
        category_known=True,
        description_from_query=True,
        reset_attachments=True,
    )
    monkeypatch.setattr(acceptance, "_fixture_for_row", lambda _row: legacy)
    provider = _ValidSuggestionProvider()
    model_run = _fake_model_run(provider, max_calls=5)
    try:
        batch = asyncio.run(acceptance._run_suggestion_cases([row], model_run, tmp_path))
    finally:
        asyncio.run(acceptance._close_model_run(model_run))
    assert batch.stop_reason == "fixture_invalid_stop"
    result = batch.results[0]
    assert result["status"] == "fixture_invalid"
    assert result["fixture"]["ok"] is False
    assert result["fixture"]["facts"]["sku"] == "SYNTH-MUG-001"
    assert result["fixture"]["facts"]["category"] == "mattress"
    assert result["fixture"]["facts"]["material_revision"] == 0
    assert result["fixture"]["facts"]["pre_suggestion_counts"] == {
        "drafts": 0,
        "approvals": 0,
        "operations": 0,
    }
    assert result["model"]["actual_provider_calls"] == 0
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("case_id", "expected_sku", "expected_name", "expected_category"),
    [
        ("H07", "SYNTH-CUP-007", "Synthetic ceramic cup", "homewares"),
        ("H08", "SYNTH-MATTRESS-008", "Synthetic mattress", "mattress"),
    ],
)
def test_corrected_fixture_is_accepted_and_suggestion_runs(
    tmp_path: Path,
    case_id: str,
    expected_sku: str,
    expected_name: str,
    expected_category: str,
) -> None:
    row = _fixture_row(case_id)
    provider = _ValidSuggestionProvider()
    model_run = _fake_model_run(provider, max_calls=5)
    try:
        batch = asyncio.run(acceptance._run_suggestion_cases([row], model_run, tmp_path))
    finally:
        asyncio.run(acceptance._close_model_run(model_run))
    assert batch.stop_reason is None
    result = batch.results[0]
    assert result["status"] == "model_case_observed"
    assert result["fixture"]["ok"] is True
    facts = result["fixture"]["facts"]
    assert facts["sku"] == expected_sku
    assert facts["name"] == expected_name
    assert facts["category"] == expected_category
    assert facts["category_source"] == "server_config"
    assert facts["description"] == row["query"]
    assert facts["material_revision"] >= 2
    assert len(facts["materials"]) >= 2
    assert len(result["fixture"]["sha256"]) == 64
    assert result["http_status"] == 200
    assert result["draft_status"] == "editing"
    assert result["deltas"] == {
        "drafts": 1,
        "approvals": 0,
        "operations": 0,
        "business_create_calls": 0,
    }
    assert result["machine_checks"]["fixture_valid"] is True
    assert result["machine_checks"]["trusted_source_citations_only"] is True
    assert result["model"]["actual_provider_calls"] == 1
    assert provider.calls == 1


# ------------------------------------------------------------------- process


def test_owned_process_group_is_stopped_exactly(tmp_path: Path) -> None:
    env = acceptance._child_env(tmp_path / "unused.db", "{}")
    process = acceptance._OwnedProcess(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env,
        role="sleeper",
    )
    try:
        assert process.pgid == process.pid
        assert process.alive is True
        stopped = process.stop(timeout=5.0)
        assert stopped["group_alive"] is False
        assert process.alive is False
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process.alive:
            process.stop(timeout=5.0)
