"""Config v0.2: built-in sources, detection, and idempotent vault initialization."""

import json
from importlib.resources import files
from pathlib import Path

import pytest


from vimgym.config import (
    AppConfig,
    FutureConfigError,
    SourceConfig,
    detect_sources,
    init_vault,
    load_config,
    save_config,
)


def test_detect_sources_finds_claude(tmp_path):
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    sources = detect_sources(tmp_path)
    claude = next((s for s in sources if s.id == "claude_code"), None)
    assert claude is not None
    assert claude.enabled is True
    assert claude.type == "claude_code"
    assert claude.auto_detected is True


def test_detect_sources_ignores_unsupported_providers(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".gemini").mkdir()
    sources = detect_sources(tmp_path)

    assert sources == []


def test_detect_sources_returns_empty_when_nothing_present(tmp_path):
    sources = detect_sources(tmp_path)
    assert sources == []


def test_source_config_watch_path_compat(tmp_path):
    cfg = AppConfig(
        vault_dir=tmp_path,
        sources=[
            SourceConfig(
                id="claude_code",
                name="Claude Code",
                type="claude_code",
                path=str(tmp_path),
                enabled=True,
            )
        ],
    )
    assert cfg.watch_path == tmp_path
    assert cfg.watch_paths == [tmp_path]
    assert len(cfg.enabled_sources) == 1


def test_enabled_sources_filters_missing_paths(tmp_path):
    cfg = AppConfig(
        vault_dir=tmp_path,
        sources=[
            SourceConfig(
                id="real", name="Real", type="claude_code", path=str(tmp_path), enabled=True
            ),
            SourceConfig(
                id="missing", name="Missing", type="claude_code", path="/no/such/dir", enabled=True
            ),
            SourceConfig(
                id="off", name="Off", type="claude_code", path=str(tmp_path), enabled=False
            ),
        ],
    )
    enabled = cfg.enabled_sources
    assert len(enabled) == 1
    assert enabled[0].id == "real"


def test_env_override_replaces_sources(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("VIMGYM_WATCH_PATH", str(data))
    cfg = load_config(vault_dir=tmp_path)
    assert cfg.watch_path == data
    assert len(cfg.sources) == 1
    assert cfg.sources[0].id == "env_override"


def test_v1_watch_path_is_loaded_as_a_supported_source(tmp_path):
    custom = tmp_path / "custom-claude"
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vault_dir": str(tmp_path),
                "watch_path": str(custom),
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(vault_dir=tmp_path)

    assert len(cfg.sources) == 1
    assert cfg.sources[0].id == "claude_code"
    assert cfg.sources[0].path == str(custom)
    assert cfg.sources[0].supported is True


def test_env_override_init_does_not_broaden_to_real_provider_homes(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    (fake_home / ".codex" / "sessions").mkdir(parents=True)
    override = tmp_path / "fixture-source"
    override.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("VIMGYM_WATCH_PATH", str(override))

    cfg, _ = init_vault(load_config(vault_dir=tmp_path / "vault"))

    assert [(source.id, source.path) for source in cfg.sources] == [("env_override", str(override))]


def test_init_preserves_manually_configured_claude_root(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    custom = tmp_path / "custom-claude"
    custom.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    cfg = AppConfig(
        vault_dir=tmp_path / "vault",
        sources=[
            SourceConfig(
                "claude_code",
                "Custom Claude root",
                "claude_code",
                str(custom),
                auto_detected=False,
            )
        ],
    )

    initialized, _ = init_vault(cfg)

    claude = next(source for source in initialized.sources if source.id == "claude_code")
    assert claude.path == str(custom)
    assert claude.auto_detected is False


def test_future_config_is_rejected_without_mutation(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"schema_version": 99, "vault_dir": str(tmp_path), "sources": []}),
        encoding="utf-8",
    )
    before = config_path.read_bytes()
    cfg = load_config(vault_dir=tmp_path)

    with pytest.raises(FutureConfigError):
        init_vault(cfg)
    with pytest.raises(FutureConfigError):
        save_config(cfg)

    assert config_path.read_bytes() == before


def test_save_load_sources_roundtrip(tmp_path):
    cfg = AppConfig(
        vault_dir=tmp_path,
        sources=[
            SourceConfig(
                id="claude_code",
                name="Claude Code",
                type="claude_code",
                path="~/.claude/projects",
                enabled=True,
                auto_detected=True,
            )
        ],
    )
    save_config(cfg)
    cfg2 = load_config(vault_dir=tmp_path)
    assert len(cfg2.sources) == 1
    assert cfg2.sources[0].id == "claude_code"
    assert cfg2.sources[0].enabled is True
    assert cfg2.sources[0].auto_detected is True


def test_save_writes_schema_version_2(tmp_path):
    cfg = AppConfig(vault_dir=tmp_path)
    save_config(cfg)
    raw = json.loads((tmp_path / "config.json").read_text())
    assert raw["schema_version"] == 2
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600


def test_shipped_default_configs_use_the_v2_source_shape():
    package_default = json.loads(
        files("vimgym.defaults").joinpath("config.json").read_text(encoding="utf-8")
    )
    repository_default = json.loads(
        (Path(__file__).parents[1] / "defaults" / "config.json").read_text(encoding="utf-8")
    )

    assert package_default == repository_default
    assert package_default["schema_version"] == 2
    assert package_default["sources"] == []
    assert "watch_path" not in package_default


def test_unknown_source_type_is_retained_as_disabled_metadata(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sources": [
                    {
                        "id": "legacy-provider",
                        "name": "Legacy provider",
                        "type": "unknown",
                        "path": str(tmp_path / "legacy"),
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(vault_dir=tmp_path)

    assert len(cfg.sources) == 1
    assert cfg.sources[0].id == "legacy-provider"
    assert cfg.sources[0].supported is False
    assert cfg.sources[0].enabled is False
    assert cfg.enabled_sources == []

    constructed = SourceConfig(
        "future-provider", "Future provider", "future", str(tmp_path), enabled=True
    )
    assert constructed.supported is False
    assert constructed.enabled is False


def test_init_vault_creates_dirs_and_detects(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cfg = AppConfig(vault_dir=tmp_path / "vault")
    cfg, newly = init_vault(cfg)

    assert cfg.vault_dir.exists()
    assert (cfg.vault_dir / "logs").exists()
    assert (cfg.vault_dir / "config.json").exists()
    assert any(s.id == "claude_code" and s.enabled for s in cfg.sources)
    assert len(newly) >= 1


def test_init_vault_re_detects_on_subsequent_runs(tmp_path, monkeypatch):
    """Bug fix: vg init must always re-run detect_sources(), not just first time."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cfg = AppConfig(vault_dir=tmp_path / "vault")
    cfg, _ = init_vault(cfg)
    assert len(cfg.sources) == 1  # Claude is the only provider root present initially.

    # User installs Codex after initial vg init.
    (fake_home / ".codex" / "sessions").mkdir(parents=True)
    (fake_home / ".codex" / "archived_sessions").mkdir(parents=True)

    cfg2 = load_config(vault_dir=cfg.vault_dir)
    cfg2, newly = init_vault(cfg2)
    ids = {s.id for s in cfg2.sources}
    assert {"codex_active", "codex_archived"}.issubset(ids)
    assert "claude_code" in ids
    assert any(s.id == "codex_active" for s in newly)


def test_init_vault_preserves_user_disable(tmp_path, monkeypatch):
    """If user disabled a source, re-running init must NOT silently re-enable it."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cfg = AppConfig(vault_dir=tmp_path / "vault")
    cfg, _ = init_vault(cfg)

    # User disables claude_code via settings
    for s in cfg.sources:
        if s.id == "claude_code":
            s.enabled = False
    save_config(cfg)

    # Re-run init
    cfg2 = load_config(vault_dir=cfg.vault_dir)
    cfg2, _ = init_vault(cfg2)
    claude = next(s for s in cfg2.sources if s.id == "claude_code")
    assert claude.enabled is False, "user disable should survive re-init"
