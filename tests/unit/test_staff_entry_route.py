"""Built frontend: the confirmed staff entry is served without an SPA fallback."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main_module
from app.config.settings import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]
SHELL = "<!doctype html><html><body>synthetic staff shell</body></html>\n"
ASSET = "console.log('asset');\n"


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    """Offline settings: no developer .env, real DB, model, or connector."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="testing",
        log_level="CRITICAL",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        after_sales_enabled=enabled,
        business_connector="fake",
        deepseek_api_key=SecretStr(""),
        embedding_api_key=SecretStr(""),
        local_embedding_enabled=False,
        cross_encoder_enabled=False,
        cross_encoder_force=False,
        unstructured_enabled=False,
        mcp_enabled=False,
        redis_url="",
        qdrant_url="",
        knowledge_dir=str(ROOT / "knowledge"),
    )


def _write_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(SHELL, encoding="utf-8")
    (dist / "assets" / "app.js").write_text(ASSET, encoding="utf-8")
    return dist


def _assert_json_404(response) -> None:
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_staff_entry_serves_shell_and_leaves_other_paths_alone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "FRONTEND_DIST", _write_dist(tmp_path))
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        staff = client.get("/staff")
        staff_slash = client.get("/staff/")
        root = client.get("/")
        asset = client.get("/assets/app.js")
        unknown = client.get("/not-a-route")
        unknown_api = client.get("/api/v1/not-a-route")
        me = client.get("/api/v1/after-sales/me")
        order = client.get("/api/v1/after-sales/au/orders/ORD1234567")

    for response in (staff, staff_slash, root):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.text == SHELL
    assert asset.status_code == 200
    assert asset.text == ASSET

    # Only the confirmed entry falls back to the shell.
    _assert_json_404(unknown)
    _assert_json_404(unknown_api)

    # The shell does not open staff or business APIs without a credential.
    assert me.status_code == 401
    assert me.json()["error_code"] == "UNAUTHORIZED"
    assert order.status_code == 401
    assert order.json()["error_code"] == "UNAUTHORIZED"


def test_staff_entry_stays_public_but_feature_stays_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "FRONTEND_DIST", _write_dist(tmp_path))
    app = create_app(_settings(tmp_path, enabled=False))
    with TestClient(app) as client:
        shell = client.get("/staff")
        me = client.get("/api/v1/after-sales/me")

    assert shell.status_code == 200
    assert shell.text == SHELL
    assert me.status_code == 404
    assert me.json()["error_code"] == "AFTER_SALES_DISABLED"


def test_staff_entry_is_absent_without_built_dist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "FRONTEND_DIST", tmp_path / "missing-dist")
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        staff = client.get("/staff")
        staff_slash = client.get("/staff/")
        root = client.get("/")
        health = client.get("/api/v1/health")

    # API-only mode must not pretend the UI is available.
    _assert_json_404(staff)
    _assert_json_404(staff_slash)
    _assert_json_404(root)
    assert health.status_code == 200
