"""Narrow guards for the isolated after-sales browser acceptance runner.

Ordinary backend unit/CI runs start only harmless owned child/grandchild groups.
The real Playwright browser fault case is explicitly opt-in with
``DTC_RUN_BROWSER_FAULT_ACCEPTANCE=1`` and is skipped with a named reason
otherwise. With the flag set, a missing Node/Playwright engine fails the test
instead of skipping. No Docker, dev server, app process or user profile starts.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scripts.run_after_sales_browser import (
    DTC_MIGRATION_HEAD,
    ENGINE_CACHE,
    LOST_RESPONSE_MARKER,
    BrowserSuiteError,
    _cleanup_browser_group,
    _remove_owned_tree,
    _run_owned_process_group,
    _start_logged_process,
    _stop_logged_process,
    browser_close_status,
    build_child_env,
    build_expected,
    consumer_subject_mapping,
    pick_free_loopback_port,
    redact_text,
    validate_api_origin,
    validate_epig_manifest,
)
from scripts.run_after_sales_pg import SignalState


def _manifest(tmp_path: Path) -> dict[str, object]:
    png = tmp_path / "synthetic-upload.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    os.chmod(png, 0o600)
    return {
        "version": 1,
        "market": "au",
        "browserOrigin": "http://epig-qa-0123456789abcdef.localhost:4321",
        "internalOrigin": "http://127.0.0.1:4321",
        "runnerPid": os.getpid(),
        "subjects": {
            "one": {
                "bootstrapUrl": "http://epig-qa-0123456789abcdef.localhost:4321/api/sandbox/bootstrap?subject=one",
                "orderId": "ord_sandbox_0123456789abcdef_one",
            },
            "two": {
                "bootstrapUrl": "http://epig-qa-0123456789abcdef.localhost:4321/api/sandbox/bootstrap?subject=two",
                "orderId": "ord_sandbox_0123456789abcdef_two",
            },
        },
        "service": {"token": "synthetic-service-token-1234567890"},
        "database": {"url": "postgres://synthetic:secret@127.0.0.1:5432/dt"},
        "fixtures": {"uploadPng": str(png)},
        "boundaries": {
            "lostResponse": {
                "descriptionMarker": LOST_RESPONSE_MARKER,
                "orderId": "ord_sandbox_0123456789abcdef_one",
            }
        },
    }


def test_child_env_is_a_fixed_allowlist_without_parent_secrets(tmp_path: Path) -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/home",
        "LANG": "en_AU.UTF-8",
        "DEEPSEEK_API_KEY": "real-looking-key",
        "OPENAI_API_KEY": "other-key",
        "DATABASE_URL": "postgresql+asyncpg://real:secret@db.example/prod",
        "PGPASSWORD": "parent-password",
        "EPIG_SERVICE_TOKEN": "parent-epig-token",
        "NODE_OPTIONS": "--require=evil.js",
        "SOME_UNRELATED_SECRET": "must-not-inherit",
    }
    env = build_child_env(
        tmp_path,
        "postgresql+asyncpg://run:runpass@127.0.0.1:5432/run",
        "http://127.0.0.1:4999",
        "run-service-token",
        parent=parent,
    )
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/tmp/home"
    assert env["APP_ENV"] == "testing"
    assert env["DATABASE_URL"] == "postgresql+asyncpg://run:runpass@127.0.0.1:5432/run"
    assert env["EPIG_SERVICE_TOKEN"] == "run-service-token"
    assert env["KNOWLEDGE_DIR"] == str(tmp_path / "knowledge")
    assert env["DEEPSEEK_API_KEY"] == ""
    assert env["OPENAI_API_KEY"] == ""
    forbidden = {"PGPASSWORD", "NODE_OPTIONS", "SOME_UNRELATED_SECRET"}
    assert not (forbidden & set(env))
    assert "real-looking-key" not in json.dumps(env)
    assert "parent-password" not in json.dumps(env)
    assert "parent-epig-token" not in json.dumps(env)


def test_redaction_removes_tokens_urls_and_bearer_values() -> None:
    token = "dtc_staff_synthetic_secret_token"
    text = f"token={token} url=postgresql+asyncpg://u:p@127.0.0.1:5432/db " f"header=Bearer {token}"
    redacted = redact_text(text, [token])
    assert token not in redacted
    assert "127.0.0.1:5432" not in redacted
    assert "Bearer [redacted]" in redacted


def test_consumer_mapping_puts_the_fault_on_consumer_two(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    validated = validate_epig_manifest(
        manifest,
        runner_pid=os.getpid(),
        upload_path=Path(str(manifest["fixtures"]["uploadPng"])),  # type: ignore[index]
    )
    mapping = consumer_subject_mapping(validated["subjects"])
    assert mapping["consumerOne"]["subject"] == "two"
    assert mapping["consumerTwo"]["subject"] == "one"
    assert mapping["consumerTwo"]["lostResponse"] is True
    assert mapping["lostResponseOrderId"] == manifest["subjects"]["one"]["orderId"]  # type: ignore[index]


def test_manifest_rejects_non_loopback_browser_origin(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["browserOrigin"] = "https://public.example"
    with pytest.raises(BrowserSuiteError, match="dedicated loopback host"):
        validate_epig_manifest(
            manifest,
            runner_pid=os.getpid(),
            upload_path=Path(str(manifest["fixtures"]["uploadPng"])),  # type: ignore[index]
        )


def test_manifest_rejects_foreign_runner_and_missing_fault(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["runnerPid"] = os.getpid() + 1
    with pytest.raises(BrowserSuiteError, match="runner process"):
        validate_epig_manifest(manifest, runner_pid=os.getpid())

    manifest = _manifest(tmp_path)
    manifest["boundaries"]["lostResponse"]["descriptionMarker"] = "changed"  # type: ignore[index]
    with pytest.raises(BrowserSuiteError, match="committed-response-loss"):
        validate_epig_manifest(
            manifest,
            runner_pid=os.getpid(),
            upload_path=Path(str(manifest["fixtures"]["uploadPng"])),  # type: ignore[index]
        )


def test_api_origin_and_port_are_loopback_only() -> None:
    assert validate_api_origin("http://127.0.0.1:1234") == "http://127.0.0.1:1234"
    with pytest.raises(BrowserSuiteError):
        validate_api_origin("http://0.0.0.0:1234")
    with pytest.raises(BrowserSuiteError):
        validate_api_origin("https://127.0.0.1:1234")
    port = pick_free_loopback_port()
    assert 1 <= port <= 65535


def test_owned_tree_removal_refuses_outside_paths(tmp_path: Path) -> None:
    owned = tmp_path / "run"
    owned.mkdir()
    child = owned / "source"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(BrowserSuiteError, match="outside this run directory"):
        _remove_owned_tree(outside, owned)
    assert _remove_owned_tree(child, owned) is True
    assert not child.exists()
    assert outside.exists()


def test_expected_text_is_synthetic_and_carries_the_marker() -> None:
    expected = build_expected("0123456789abcdef")
    assert LOST_RESPONSE_MARKER in expected.consumer_two_description
    assert expected.reply_v1 != expected.reply_v2
    assert expected.consumer_one_description != expected.consumer_two_description
    assert "0123456789abcdef" in expected.reply_v2


def test_migration_head_is_the_expected_0005() -> None:
    assert DTC_MIGRATION_HEAD == "0005"


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _fault_command(*, leader_exits: bool = True) -> list[str]:
    tail = "time.sleep(0.2)\n" if leader_exits else "time.sleep(20)\n"
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n" + tail
    )
    return [sys.executable, "-c", code]


def test_stop_logged_process_requires_the_whole_group_even_after_leader_exit(
    tmp_path: Path,
) -> None:
    from scripts.run_after_sales_pg import _terminate_group, _wait_group_gone

    owned = _start_logged_process(
        "fault-leader-exit",
        _fault_command(),
        tmp_path,
        {"PATH": os.environ.get("PATH", "")},
        tmp_path / "fault.log",
    )
    pgid = owned.process.pid
    try:
        assert _wait_until(lambda: owned.process.poll() is not None), "leader did not exit"
        assert not _wait_group_gone(pgid, 0.2), "the child should still own the group"
        result = _stop_logged_process(owned, grace=3.0)
        assert result["leader_exited"] is True
        assert result["group_gone"] is True
        assert result["clean"] is True
        assert _wait_group_gone(pgid, 1.0)
    finally:
        _terminate_group(pgid, grace=2.0)


def test_owned_process_group_timeout_kills_leader_and_child(tmp_path: Path) -> None:
    from scripts.run_after_sales_pg import _wait_group_gone

    result = _run_owned_process_group(
        _fault_command(leader_exits=False),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "timeout.log",
        timeout=1.0,
        state=SignalState(),
        grace=2.0,
    )
    assert result["timed_out"] is True
    assert result["leader_exited"] is True
    assert result["group_gone"] is True
    assert result["clean"] is True
    assert _wait_group_gone(result["pid"], 1.0)


def test_owned_process_group_cancellation_kills_leader_and_child(tmp_path: Path) -> None:
    from scripts.run_after_sales_pg import _wait_group_gone

    state = SignalState()

    def request_later() -> None:
        time.sleep(0.4)
        state.request(signal.SIGTERM)

    thread = threading.Thread(target=request_later, daemon=True)
    thread.start()
    result = _run_owned_process_group(
        _fault_command(leader_exits=False),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "cancel.log",
        timeout=30.0,
        state=state,
        grace=2.0,
    )
    thread.join(timeout=2.0)
    assert result["cancelled"] is True
    assert result["leader_exited"] is True
    assert result["group_gone"] is True
    assert result["clean"] is True
    assert _wait_group_gone(result["pid"], 1.0)


def test_browser_close_requires_an_explicit_successful_observation() -> None:
    assert browser_close_status(None)["clean"] is False
    assert browser_close_status({"status": "passed"})["clean"] is False
    assert (
        browser_close_status(
            {
                "browserLifecycle": {
                    "closeAttempted": True,
                    "closeOk": False,
                    "connectedAfterClose": None,
                }
            }
        )["clean"]
        is False
    )
    assert (
        browser_close_status(
            {
                "browserLifecycle": {
                    "closeAttempted": True,
                    "closeOk": True,
                    "connectedAfterClose": True,
                }
            }
        )["clean"]
        is False
    )
    assert (
        browser_close_status(
            {
                "browserLifecycle": {
                    "closeAttempted": True,
                    "closeOk": True,
                    "connectedAfterClose": False,
                }
            }
        )["clean"]
        is True
    )


@pytest.mark.skipif(
    os.environ.get("DTC_RUN_BROWSER_FAULT_ACCEPTANCE") != "1",
    reason=(
        "set DTC_RUN_BROWSER_FAULT_ACCEPTANCE=1 to run the real Playwright "
        "browser fault acceptance"
    ),
)
def test_actual_detached_browser_group_is_cleaned_after_node_kill(tmp_path: Path) -> None:
    """A real Playwright browser survives Node death and is killed by its sidecar group."""
    from scripts.run_after_sales_pg import _terminate_group, _wait_group_gone

    root = Path(__file__).resolve().parents[2]
    playwright_module = root / "frontend" / "node_modules" / "playwright"
    engine_dir = ENGINE_CACHE / "chromium_headless_shell-1254"
    if not engine_dir.is_dir():
        pytest.fail(
            "DTC_RUN_BROWSER_FAULT_ACCEPTANCE=1 requires the dedicated Playwright "
            "Chromium engine in the marked cache"
        )
    if not playwright_module.is_dir():
        pytest.fail("DTC_RUN_BROWSER_FAULT_ACCEPTANCE=1 requires the pinned Playwright package")
    if shutil.which("node") is None:
        pytest.fail("DTC_RUN_BROWSER_FAULT_ACCEPTANCE=1 requires Node.js")
    profile_root = tmp_path / "browser-profile-root"
    profile_root.mkdir(mode=0o700)
    sidecar = tmp_path / "browser-lifecycle.json"
    node_script = """
const { chromium } = require(process.argv[1]);
(async () => {
  const server = await chromium.launchServer({
    headless: true,
    host: "127.0.0.1",
    port: 0,
    handleSIGINT: false,
    handleSIGHUP: false,
    handleSIGTERM: false,
  });
  const child = server.process();
  const fs = require("fs");
  const payload = {
    version: 1,
    pid: child.pid,
    pgid: child.pid,
    platform: process.platform,
    executablePath: child.spawnfile || chromium.executablePath(),
    startedAt: new Date().toISOString(),
  };
  fs.writeFileSync(process.argv[2], JSON.stringify(payload) + "\\n", { mode: 0o600 });
  fs.chmodSync(process.argv[2], 0o600);
  console.log(JSON.stringify({ pid: child.pid }));
  setInterval(() => {}, 1000);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "TMPDIR": str(profile_root),
        "PLAYWRIGHT_BROWSERS_PATH": str(ENGINE_CACHE),
    }
    node = subprocess.Popen(
        ["node", "-e", node_script, str(playwright_module), str(sidecar)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    browser_pgid = 0
    try:
        assert _wait_until(sidecar.is_file, 45.0), "the real browser did not publish its sidecar"
        assert stat_mode(sidecar) == 0o600
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        browser_pgid = int(payload["pid"])
        assert int(payload["pgid"]) == browser_pgid
        assert os.getpgid(browser_pgid) == browser_pgid

        # Freeze the real owned browser group so Node's death cannot let it
        # exit before Python observes and cleans the exact sidecar group.
        os.killpg(browser_pgid, signal.SIGSTOP)
        os.killpg(node.pid, signal.SIGKILL)
        node.wait(timeout=10.0)
        assert not _wait_group_gone(
            browser_pgid, 0.5
        ), "the stopped browser group unexpectedly exited"

        result = _cleanup_browser_group(sidecar, grace=2.0)
        assert result["clean"] is True
        assert result["group_gone"] is True
        assert result["forced"] is True
        assert _wait_group_gone(browser_pgid, 2.0)
    finally:
        if browser_pgid:
            _terminate_group(browser_pgid, grace=2.0)
        try:
            os.killpg(node.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            node.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(profile_root, ignore_errors=True)
        sidecar.unlink(missing_ok=True)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
