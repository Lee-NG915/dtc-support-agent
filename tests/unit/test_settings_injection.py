from __future__ import annotations

from pathlib import Path

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
