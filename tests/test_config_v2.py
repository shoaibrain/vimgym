"""Config v0.1: sources[], detection, init_vault re-detect + merge."""
from pathlib import Path


from vimgym.config import (
    AppConfig,
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


def test_detect_sources_disables_unknown_parsers(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".gemini").mkdir()
    sources = detect_sources(tmp_path)

    cursor = next((s for s in sources if s.id == "cursor"), None)
    gemini = next((s for s in sources if s.id == "gemini"), None)

    assert cursor is not None
    assert cursor.enabled is False
    assert gemini is not None
    assert gemini.enabled is False


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
            SourceConfig(id="real",  name="Real",  type="claude_code",
                         path=str(tmp_path), enabled=True),
            SourceConfig(id="missing", name="Missing", type="claude_code",
                         path="/no/such/dir", enabled=True),
            SourceConfig(id="off", name="Off", type="claude_code",
                         path=str(tmp_path), enabled=False),
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


def test_save_writes_schema_version_1(tmp_path):
    import json
    cfg = AppConfig(vault_dir=tmp_path)
    save_config(cfg)
    raw = json.loads((tmp_path / "config.json").read_text())
    assert raw["schema_version"] == 1


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
    assert len(cfg.sources) == 1  # only Claude Code so far

    # User installs Cursor after initial vg init
    (fake_home / ".cursor").mkdir()

    cfg2 = load_config(vault_dir=cfg.vault_dir)
    cfg2, newly = init_vault(cfg2)
    ids = {s.id for s in cfg2.sources}
    assert "cursor" in ids
    assert "claude_code" in ids
    assert any(s.id == "cursor" for s in newly)


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
