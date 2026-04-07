"""Vimgym configuration — schema v2 with sources[]."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_SCHEMA_VERSION = 1


# ── Source descriptors ──────────────────────────────────────────────────


@dataclass
class SourceConfig:
    id: str
    name: str
    type: str             # "claude_code" | "cursor" | "copilot" | "gemini" | "antigravity" | ...
    path: str             # raw, may contain ~
    enabled: bool = True
    auto_detected: bool = False

    @property
    def expanded_path(self) -> Path:
        return Path(self.path).expanduser()

    def exists(self) -> bool:
        return self.expanded_path.exists()


# Known AI tools — used by detect_sources(). Only `claude_code` has a parser in v1;
# everything else is detected and persisted as disabled, so users can see what's
# available and toggle once parsers ship.
KNOWN_SOURCES: list[dict[str, str]] = [
    {
        "id": "claude_code",
        "name": "Claude Code",
        "type": "claude_code",
        "check_path": "~/.claude",
        "watch_path": "~/.claude/projects",
        "note": "Anthropic's Claude Code CLI",
    },
    {
        "id": "cursor",
        "name": "Cursor",
        "type": "unknown",
        "check_path": "~/.cursor",
        "watch_path": "~/.cursor",
        "note": "Cursor IDE — parser not yet available",
    },
    {
        "id": "copilot",
        "name": "GitHub Copilot",
        "type": "unknown",
        "check_path": "~/.copilot",
        "watch_path": "~/.copilot",
        "note": "GitHub Copilot — parser not yet available",
    },
    {
        "id": "antigravity",
        "name": "Antigravity",
        "type": "unknown",
        "check_path": "~/.antigravity",
        "watch_path": "~/.antigravity",
        "note": "Antigravity — parser not yet available",
    },
    {
        "id": "gemini",
        "name": "Gemini CLI",
        "type": "unknown",
        "check_path": "~/.gemini",
        "watch_path": "~/.gemini",
        "note": "Google Gemini CLI — parser not yet available",
    },
]


def detect_sources(home_dir: Path | None = None) -> list[SourceConfig]:
    """Scan home dir for known AI tool dirs.

    Returns one SourceConfig per detected tool. Only sources with a v1 parser
    (currently just `claude_code`) are enabled by default; others are detected
    but disabled.
    """
    if home_dir is None:
        home_dir = Path.home()

    detected: list[SourceConfig] = []
    for entry in KNOWN_SOURCES:
        # Resolve check_path relative to the supplied home_dir for testability.
        rel = entry["check_path"].lstrip("~/").lstrip("/")
        check = (home_dir / rel) if entry["check_path"].startswith("~") else Path(entry["check_path"])
        if check.exists():
            watch_rel = entry["watch_path"]
            # Re-anchor watch_path on the synthetic home_dir if it was a ~ path
            # so unit tests with tmp_path don't accidentally watch the real $HOME.
            if watch_rel.startswith("~"):
                watch_anchored = str(home_dir / watch_rel.lstrip("~/").lstrip("/"))
            else:
                watch_anchored = watch_rel
            detected.append(
                SourceConfig(
                    id=entry["id"],
                    name=entry["name"],
                    type=entry["type"],
                    path=watch_anchored,
                    enabled=(entry["type"] == "claude_code"),
                    auto_detected=True,
                )
            )

    return detected


# ── App config ─────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    vault_dir: Path = field(default_factory=lambda: Path("~/.vimgym").expanduser())
    server_host: str = "127.0.0.1"
    server_port: int = 7337
    auto_open_browser: bool = True
    log_level: str = "INFO"
    debounce_secs: float = 5.0
    stability_polls: int = 2
    stability_poll_interval: float = 1.0
    sources: list[SourceConfig] = field(default_factory=list)

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled and s.exists()]

    @property
    def watch_paths(self) -> list[Path]:
        return [s.expanded_path for s in self.enabled_sources]

    @property
    def watch_path(self) -> Path:
        """Legacy compat: returns the first enabled source path, or the
        Claude Code default if none. Used by tests + a few code paths that
        haven't migrated to multi-source yet."""
        paths = self.watch_paths
        return paths[0] if paths else Path("~/.claude/projects").expanduser()

    @property
    def db_path(self) -> Path:
        return self.vault_dir / "vault.db"

    @property
    def pid_path(self) -> Path:
        return self.vault_dir / "vimgym.pid"

    @property
    def log_path(self) -> Path:
        return self.vault_dir / "logs" / "vimgym.log"

    @property
    def rules_path(self) -> Path:
        return self.vault_dir / "redaction-rules.json"


# ── Load / save ────────────────────────────────────────────────────────


_SCALAR_FIELDS = {
    "server_host", "server_port", "auto_open_browser", "log_level",
    "debounce_secs", "stability_polls", "stability_poll_interval",
}


def _deserialize_sources(raw_sources: list) -> list[SourceConfig]:
    out: list[SourceConfig] = []
    for s in raw_sources or []:
        if not isinstance(s, dict) or "id" not in s or "type" not in s or "path" not in s:
            continue
        out.append(
            SourceConfig(
                id=s["id"],
                name=s.get("name") or s["id"],
                type=s["type"],
                path=s["path"],
                enabled=bool(s.get("enabled", True)),
                auto_detected=bool(s.get("auto_detected", False)),
            )
        )
    return out


def load_config(vault_dir: Path | None = None) -> AppConfig:
    """Load config from $VIMGYM_PATH/config.json with env overrides.

    On disk, the config follows schema v2. v1 configs are migrated transparently
    on read but only persisted to disk by an explicit save_config() call.
    """
    base = Path(
        os.environ.get("VIMGYM_PATH", str(Path("~/.vimgym").expanduser()))
    ).expanduser()
    if vault_dir is not None:
        base = vault_dir

    config_file = base / "config.json"
    cfg = AppConfig(vault_dir=base)

    if config_file.exists():
        try:
            raw = json.loads(config_file.read_text())
        except json.JSONDecodeError:
            raw = {}

        if "vault_dir" in raw:
            cfg.vault_dir = Path(raw["vault_dir"]).expanduser()
        for key in _SCALAR_FIELDS:
            if key in raw:
                setattr(cfg, key, raw[key])
        cfg.sources = _deserialize_sources(raw.get("sources", []))

    # ── Environment overrides ──
    if "VIMGYM_PORT" in os.environ:
        try:
            cfg.server_port = int(os.environ["VIMGYM_PORT"])
        except ValueError:
            pass

    if "VIMGYM_WATCH_PATH" in os.environ:
        override = Path(os.environ["VIMGYM_WATCH_PATH"]).expanduser()
        cfg.sources = [
            SourceConfig(
                id="env_override",
                name="ENV Override",
                type="claude_code",
                path=str(override),
                enabled=True,
                auto_detected=False,
            )
        ]

    return cfg


def save_config(cfg: AppConfig) -> None:
    """Atomically write the config as schema v2."""
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    config_file = cfg.vault_dir / "config.json"
    tmp = config_file.with_suffix(".tmp")
    data = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "vault_dir": str(cfg.vault_dir),
        "server_host": cfg.server_host,
        "server_port": cfg.server_port,
        "auto_open_browser": cfg.auto_open_browser,
        "log_level": cfg.log_level,
        "debounce_secs": cfg.debounce_secs,
        "stability_polls": cfg.stability_polls,
        "stability_poll_interval": cfg.stability_poll_interval,
        "sources": [asdict(s) for s in cfg.sources],
    }
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(config_file)


# ── Vault initialization (called by `vg init` and lazily by `vg start`) ──


def init_vault(cfg: AppConfig | None = None) -> tuple[AppConfig, list[SourceConfig]]:
    """Create vault dir, re-run source detection, merge into config, persist.

    Always re-runs `detect_sources()` so newly-installed AI tools get picked up
    on subsequent `vg init` calls. User-set `enabled` flags on existing sources
    are preserved (merge by id). Returns (config, newly_added_sources).
    """
    if cfg is None:
        cfg = load_config()

    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    (cfg.vault_dir / "logs").mkdir(parents=True, exist_ok=True)

    detected = detect_sources()
    existing_by_id = {s.id: s for s in cfg.sources}
    newly_added: list[SourceConfig] = []

    merged: list[SourceConfig] = []
    for d in detected:
        if d.id in existing_by_id:
            # Source already known: keep the user's enabled flag, refresh the
            # path in case the tool moved, mark auto_detected for sources we
            # actually re-detected on this run.
            existing = existing_by_id[d.id]
            existing.path = d.path
            existing.type = d.type
            existing.name = d.name
            existing.auto_detected = True
            merged.append(existing)
        else:
            merged.append(d)
            newly_added.append(d)

    # Carry through any user-added sources that aren't in KNOWN_SOURCES.
    for s in cfg.sources:
        if s.id not in {d.id for d in detected}:
            merged.append(s)

    cfg.sources = merged
    save_config(cfg)
    return cfg, newly_added
