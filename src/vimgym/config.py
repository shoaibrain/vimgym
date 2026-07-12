"""Vimgym configuration — schema v2 with sources[]."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_SCHEMA_VERSION = 2
SUPPORTED_SOURCE_TYPES = frozenset({"claude_code", "codex"})


class FutureConfigError(RuntimeError):
    """A newer config schema must not be overwritten by this Vimgym."""


# ── Source descriptors ──────────────────────────────────────────────────


@dataclass
class SourceConfig:
    id: str
    name: str
    type: str  # "claude_code" | "codex"
    path: str  # raw, may contain ~
    enabled: bool = True
    auto_detected: bool = False

    def __post_init__(self) -> None:
        # Unknown v1-era entries remain inspectable metadata, never active roots.
        if self.type not in SUPPORTED_SOURCE_TYPES:
            self.enabled = False

    @property
    def expanded_path(self) -> Path:
        return Path(self.path).expanduser()

    def exists(self) -> bool:
        return self.expanded_path.exists()

    @property
    def supported(self) -> bool:
        """Whether v0.2 has a built-in parser for this metadata record."""
        return self.type in SUPPORTED_SOURCE_TYPES


# v0.2 intentionally supports exactly these built-in provider roots. Other
# application state (including Codex databases, logs, memories, goals, and
# import maps) is never discovered or read.
KNOWN_SOURCES: list[dict[str, str]] = [
    {
        "id": "claude_code",
        "name": "Claude Code",
        "type": "claude_code",
        "check_path": "~/.claude",
        "watch_path": "~/.claude/projects",
        "note": "Anthropic's Claude Code CLI",
    },
]


def detect_sources(home_dir: Path | None = None) -> list[SourceConfig]:
    """Scan home dir for known AI tool dirs.

    Returns the Claude projects root and the two locked Codex session roots.
    ``home_dir`` re-anchors discovery for deterministic tests; otherwise
    ``CODEX_HOME`` is honored and defaults to ``~/.codex``.
    """
    if home_dir is None:
        home_dir = Path.home()

    detected: list[SourceConfig] = []
    for entry in KNOWN_SOURCES:
        # Resolve check_path relative to the supplied home_dir for testability.
        rel = entry["check_path"].lstrip("~/").lstrip("/")
        check = (
            (home_dir / rel) if entry["check_path"].startswith("~") else Path(entry["check_path"])
        )
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
                    enabled=True,
                    auto_detected=True,
                )
            )

    codex_home = (
        Path(os.environ["CODEX_HOME"]).expanduser()
        if home_dir == Path.home() and "CODEX_HOME" in os.environ
        else home_dir / ".codex"
    )
    for source_id, name, child in (
        ("codex_active", "Codex active sessions", "sessions"),
        ("codex_archived", "Codex archived sessions", "archived_sessions"),
    ):
        path = codex_home / child
        if path.exists():
            detected.append(
                SourceConfig(
                    id=source_id,
                    name=name,
                    type="codex",
                    path=str(path),
                    enabled=True,
                    auto_detected=True,
                )
            )

    return detected


def validate_loopback_host(host: str) -> str:
    """Reject public bind configuration; v0.2 has no authentication layer."""
    normalized = host.strip().lower().strip("[]")
    if normalized not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("vimgym may only bind to 127.0.0.1, localhost, or ::1")
    return host


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
        return [s for s in self.sources if s.supported and s.enabled and s.exists()]

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
    "server_host",
    "server_port",
    "auto_open_browser",
    "log_level",
    "debounce_secs",
    "stability_polls",
    "stability_poll_interval",
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
                enabled=bool(s.get("enabled", True)) and s["type"] in SUPPORTED_SOURCE_TYPES,
                auto_detected=bool(s.get("auto_detected", False)),
            )
        )
    return out


def stored_config_schema(vault_dir: Path) -> int | None:
    """Return the persisted config schema without modifying the vault."""

    path = Path(vault_dir) / "config.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    version = value.get("schema_version")
    if isinstance(version, int):
        return version
    if "watch_path" in value and "sources" not in value:
        return 1
    if "sources" in value:
        return 2
    return None


def load_config(vault_dir: Path | None = None) -> AppConfig:
    """Load config from $VIMGYM_PATH/config.json with env overrides.

    On disk, the config follows schema v2. v1 configs are migrated transparently
    on read but only persisted to disk by an explicit save_config() call.
    """
    base = Path(os.environ.get("VIMGYM_PATH", str(Path("~/.vimgym").expanduser()))).expanduser()
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
        legacy_watch_path = raw.get("watch_path")
        if not cfg.sources and isinstance(legacy_watch_path, str) and legacy_watch_path:
            cfg.sources = [
                SourceConfig(
                    id="claude_code",
                    name="Claude Code",
                    type="claude_code",
                    path=legacy_watch_path,
                    enabled=True,
                    auto_detected=False,
                )
            ]

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

    validate_loopback_host(cfg.server_host)

    return cfg


def save_config(cfg: AppConfig) -> None:
    """Atomically write the config as schema v2."""
    validate_loopback_host(cfg.server_host)
    existing_schema = stored_config_schema(cfg.vault_dir)
    if existing_schema is not None and existing_schema > CONFIG_SCHEMA_VERSION:
        raise FutureConfigError(
            f"config schema v{existing_schema} is newer than supported v{CONFIG_SCHEMA_VERSION}"
        )
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.vault_dir, 0o700)
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
        "sources": [{**asdict(s), "enabled": bool(s.enabled and s.supported)} for s in cfg.sources],
    }
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(config_file)


# ── Vault initialization (called by `vg init` and lazily by `vg start`) ──


def init_vault(cfg: AppConfig | None = None) -> tuple[AppConfig, list[SourceConfig]]:
    """Create vault dir, re-run source detection, merge into config, persist.

    Always re-runs `detect_sources()` so newly-installed supported providers are picked up
    on subsequent `vg init` calls. User-set `enabled` flags on existing sources
    are preserved (merge by id). Returns (config, newly_added_sources).
    """
    if cfg is None:
        cfg = load_config()
    existing_schema = stored_config_schema(cfg.vault_dir)
    if existing_schema is not None and existing_schema > CONFIG_SCHEMA_VERSION:
        raise FutureConfigError(
            f"config schema v{existing_schema} is newer than supported v{CONFIG_SCHEMA_VERSION}"
        )

    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.vault_dir, 0o700)
    logs_dir = cfg.vault_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(logs_dir, 0o700)

    # The legacy development override is explicitly a single-root mode; never
    # broaden it by auto-detecting the operator's real provider homes.
    detected = [] if "VIMGYM_WATCH_PATH" in os.environ else detect_sources()
    existing_by_id = {s.id: s for s in cfg.sources}
    newly_added: list[SourceConfig] = []

    merged: list[SourceConfig] = []
    for d in detected:
        if d.id in existing_by_id:
            # Source already known: keep the user's enabled flag, refresh the
            # path in case the tool moved, mark auto_detected for sources we
            # actually re-detected on this run.
            existing = existing_by_id[d.id]
            if existing.auto_detected:
                existing.path = d.path
                existing.type = d.type
                existing.name = d.name
            merged.append(existing)
        else:
            merged.append(d)
            newly_added.append(d)

    # Carry through manually configured roots that were not auto-detected.
    for s in cfg.sources:
        if s.id not in {d.id for d in detected}:
            merged.append(s)

    cfg.sources = merged
    save_config(cfg)
    return cfg, newly_added
