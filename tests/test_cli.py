import subprocess
import sys


def run(args):
    return subprocess.run(
        [sys.executable, "-m", "vimgym.cli"] + args,
        capture_output=True, text=True
    )


def test_version():
    from vimgym import __version__
    r = run(["--version"])
    assert r.returncode == 0
    assert __version__ in r.stdout


def test_help():
    r = run(["--help"])
    assert r.returncode == 0
    assert "start" in r.stdout
    assert "search" in r.stdout


def test_status_runs(tmp_path, monkeypatch):
    # Point at an isolated vault so we don't touch the user's real one.
    monkeypatch.setenv("VIMGYM_PATH", str(tmp_path))
    r = run(["status"])
    assert r.returncode == 0
    assert "stopped" in r.stdout or "running" in r.stdout


def test_stop_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("VIMGYM_PATH", str(tmp_path))
    r = run(["stop"])
    assert r.returncode == 0
    assert "not running" in r.stdout or "stopped" in r.stdout


def test_open_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("VIMGYM_PATH", str(tmp_path))
    r = run(["open"])
    assert r.returncode == 1
    assert "not running" in r.stdout


def test_search_requires_query(tmp_path, monkeypatch):
    monkeypatch.setenv("VIMGYM_PATH", str(tmp_path))
    r = run(["search"])
    assert r.returncode == 2


def test_vg_init(tmp_path, monkeypatch):
    monkeypatch.setenv("VIMGYM_PATH", str(tmp_path))
    r = run(["init"])
    assert r.returncode == 0
    assert "vault initialized" in r.stdout
    assert (tmp_path / "config.json").exists()


def test_vg_config_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("VIMGYM_PATH", str(tmp_path))
    r = run(["config"])
    assert r.returncode == 0
    assert "vault" in r.stdout
    assert "server" in r.stdout


def test_vg_config_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("VIMGYM_PATH", str(tmp_path))
    run(["init"])  # populate sources first
    r = run(["config", "sources"])
    assert r.returncode == 0
    # Will print at least the table header even if no sources detected on this machine
    assert "id" in r.stdout or "no sources" in r.stdout
