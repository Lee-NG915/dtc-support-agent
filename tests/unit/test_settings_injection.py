from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.settings import Settings, clear_settings_cache, get_settings


def test_testing_settings_never_read_a_developer_env_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "APP_NAME=must-not-be-loaded\nDEEPSEEK_API_KEY=must-not-be-loaded\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "testing")
    clear_settings_cache()
    try:
        settings = get_settings()
        assert settings.app_name == "adaptive-rag-cs"
        assert settings.deepseek_api_key.get_secret_value() == ""
    finally:
        clear_settings_cache()


def test_explicit_settings_are_kept_for_the_application() -> None:
    settings = Settings(
        _env_file=None,
        app_env="testing",
        after_sales_enabled=True,
        business_connector="fake",
    )
    assert settings.after_sales_enabled is True
    assert settings.business_connector == "fake"


def test_default_deepseek_model_is_the_current_official_name(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_THINKING_MODE", raising=False)

    settings = Settings(_env_file=None)

    assert settings.deepseek_model == "deepseek-flash"
    assert settings.deepseek_thinking_mode is None


def test_env_example_overrides_model_and_explicitly_disables_thinking(monkeypatch) -> None:
    for name in ("DEEPSEEK_MODEL", "DEEPSEEK_THINKING_MODE", "DEEPSEEK_TEMPERATURE"):
        monkeypatch.delenv(name, raising=False)
    sample = Path(__file__).resolve().parents[2] / ".env.example"

    from_sample = Settings(_env_file=sample)
    unspecified_default = Settings(_env_file=None)

    assert from_sample.deepseek_model == "deepseek-flash"
    assert from_sample.deepseek_thinking_mode == "disabled"
    assert from_sample.deepseek_temperature is None
    assert unspecified_default.deepseek_thinking_mode is None


def test_deepseek_thinking_mode_is_narrow_and_optional() -> None:
    assert Settings(_env_file=None, deepseek_thinking_mode="disabled").deepseek_thinking_mode == (
        "disabled"
    )
    assert Settings(_env_file=None, deepseek_thinking_mode="enabled").deepseek_thinking_mode == (
        "enabled"
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, deepseek_thinking_mode="auto")
