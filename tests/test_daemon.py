"""Integration tests for the full daemon: spawns a real subprocess."""
import shutil
import socket
import time
from pathlib import Path

import httpx
import pytest

from vimgym.config import AppConfig, SourceConfig, save_config
from vimgym.daemon import is_running, start_daemon, stop_daemon

DATA_DIR = Path(__file__).parent.parent / "data" / "-Users-shoaibrain-edforge"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def daemon_cfg(tmp_path):
    watch = tmp_path / "watch"
    proj = watch / "-Users-shoaibrain-edforge"
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
