"""Integration tests for the full daemon: spawns a real subprocess."""

import shutil
import socket
import time
from pathlib import Path

import httpx
import pytest

from vimgym.config import AppConfig, SourceConfig, save_config
from vimgym.daemon import _server_responding, is_running, start_daemon, stop_daemon

DATA_DIR = Path(__file__).parent / "fixtures" / "sessions" / "-Users-example-edforge"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def daemon_cfg(tmp_path):
    watch = tmp_path / "watch"
    proj = watch / "-Users-example-edforge"
    proj.mkdir(parents=True)
    # Seed one small session so backfill has work to do.
    src = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    shutil.copy(src, proj / src.name)

    cfg = AppConfig(
        vault_dir=tmp_path / "vault",
        sources=[
            SourceConfig(
                id="claude_code",
                name="Claude Code",
                type="claude_code",
                path=str(watch),
                enabled=True,
            )
        ],
        server_port=_free_port(),
        debounce_secs=0.5,
        stability_polls=1,
        stability_poll_interval=0.05,
        auto_open_browser=False,
    )
    cfg.vault_dir.mkdir(parents=True)
    save_config(cfg)
    yield cfg
    # Cleanup: ensure no orphan process.
    if is_running(cfg):
        stop_daemon(cfg)


def test_pid_lifecycle(daemon_cfg):
    cfg = daemon_cfg
    assert is_running(cfg) is False
    pid = start_daemon(cfg)
    assert pid > 0
    assert is_running(cfg) is True

    # Wait until the HTTP server is actually serving.
    deadline = time.monotonic() + 10.0
    health = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{cfg.server_port}/health", timeout=0.5)
            if r.status_code == 200:
                health = r.json()
                break
        except Exception:
            pass
        time.sleep(0.1)
    assert health is not None, "daemon never responded on /health"
    assert health["status"] == "ok"
    assert health["pid"] == pid
    # Backfilled the seeded session.
    assert health["sessions"] >= 1

    # Search through the API.
    r = httpx.get(
        f"http://127.0.0.1:{cfg.server_port}/api/search",
        params={"q": "CloudFormation"},
        timeout=2.0,
    )
    assert r.status_code == 200
    assert r.json()["total"] >= 1

    # Stop.
    assert stop_daemon(cfg) is True
    assert is_running(cfg) is False


def test_double_start_raises(daemon_cfg):
    cfg = daemon_cfg
    start_daemon(cfg)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            start_daemon(cfg)
    finally:
        stop_daemon(cfg)


def test_stale_pid_cleared(daemon_cfg):
    cfg = daemon_cfg
    cfg.pid_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_path.write_text("999999")  # very unlikely to exist
    assert is_running(cfg) is False
    assert not cfg.pid_path.exists()


def test_start_daemon_does_not_forward_legacy_watch_override(tmp_path, monkeypatch):
    """The detached child must reload the persisted multi-source v2 config."""
    cfg = AppConfig(vault_dir=tmp_path / "vault", server_port=7338)
    cfg.vault_dir.mkdir(parents=True)
    captured: dict[str, object] = {}

    class Process:
        pid = 43210
        returncode = None

        @staticmethod
        def poll():
            return None

    def popen(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setenv("VIMGYM_WATCH_PATH", str(tmp_path / "legacy-override"))
    monkeypatch.setattr("vimgym.daemon.is_running", lambda _config: False)
    monkeypatch.setattr("vimgym.daemon._server_responding", lambda _config: True)
    monkeypatch.setattr("vimgym.daemon.subprocess.Popen", popen)

    assert start_daemon(cfg) == Process.pid
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "VIMGYM_WATCH_PATH" not in child_env
    assert child_env["VIMGYM_PATH"] == str(cfg.vault_dir)


def test_start_daemon_fails_when_exact_child_never_becomes_ready(tmp_path, monkeypatch):
    cfg = AppConfig(vault_dir=tmp_path / "vault", server_port=7338)
    cfg.vault_dir.mkdir(parents=True)

    class Process:
        pid = 43210
        returncode = None
        terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0

        def kill(self):
            self.terminated = True

    process = Process()
    monkeypatch.setattr("vimgym.daemon.is_running", lambda _config: False)
    monkeypatch.setattr("vimgym.daemon._server_responding", lambda _config: False)
    monkeypatch.setattr("vimgym.daemon._STARTUP_TIMEOUT_SECS", 0.0)
    monkeypatch.setattr("vimgym.daemon.subprocess.Popen", lambda *args, **kwargs: process)

    with pytest.raises(RuntimeError, match="did not become ready"):
        start_daemon(cfg)

    assert process.terminated is True
    assert not cfg.pid_path.exists()


def test_server_readiness_requires_matching_child_pid(tmp_path, monkeypatch):
    cfg = AppConfig(vault_dir=tmp_path, server_port=7338)
    cfg.pid_path.write_text("43210")

    class Response:
        status_code = 200
        pid = 99999

        def json(self):
            return {"status": "ok", "pid": self.pid}

    response = Response()
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: response)
    assert _server_responding(cfg) is False

    response.pid = 43210
    assert _server_responding(cfg) is True


def test_no_duplicate_log_lines(daemon_cfg):
    """Regression test for the daemon double-write logging bug.

    The parent's start_daemon() spawns the child with stdout/stderr redirected
    to the log file. The child's logger must therefore use ONLY a FileHandler;
    a StreamHandler(sys.stderr) would cause every record to appear twice.
    """
    cfg = daemon_cfg
    start_daemon(cfg)
    try:
        # Wait for the server to come up and the backfill log line to appear.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"http://127.0.0.1:{cfg.server_port}/health", timeout=0.5)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        # Give the logger a moment to flush.
        time.sleep(0.5)

        assert cfg.log_path.exists(), "log file was not created"
        content = cfg.log_path.read_text(encoding="utf-8", errors="replace")
        backfill_lines = [ln for ln in content.splitlines() if "backfill processed" in ln]
        assert len(backfill_lines) == 1, (
            f"Expected exactly 1 'backfill processed' line, got "
            f"{len(backfill_lines)}:\n{backfill_lines}"
        )
    finally:
        stop_daemon(cfg)
