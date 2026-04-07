from pathlib import Path

from vimgym.config import AppConfig, load_config, save_config


def test_defaults():
    cfg = AppConfig()
    assert cfg.server_port == 7337
    assert cfg.server_host == "127.0.0.1"
    assert cfg.debounce_secs == 5.0


def test_env_watch_path_override(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("VIMGYM_WATCH_PATH", str(data))
    cfg = load_config(vault_dir=tmp_path)
    assert cfg.watch_path == data
    assert len(cfg.sources) == 1
    assert cfg.sources[0].id == "env_override"


def test_env_port_override(tmp_path, monkeypatch):
    monkeypatch.setenv("VIMGYM_PORT", "8080")
    cfg = load_config(vault_dir=tmp_path)
    assert cfg.server_port == 8080


def test_save_load_roundtrip(tmp_path):
    cfg = AppConfig(vault_dir=tmp_path, server_port=9000)
    save_config(cfg)
    cfg2 = load_config(vault_dir=tmp_path)
    assert cfg2.server_port == 9000


def test_db_path_property():
    cfg = AppConfig(vault_dir=Path("/tmp/testvault"))
    assert cfg.db_path == Path("/tmp/testvault/vault.db")
