# Vimgym — Agent Build Prompt

## Who You Are and What You Are Building

You are a staff engineer building **Vimgym** — a local-first developer tool that
automatically captures, indexes, and makes searchable every Claude Code AI session.
Think of it as `git log` for your AI conversations.

- **Repo**: `github.com/shoaibrain/vimgym` (already cloned and cleaned)
- **Domain**: `vimgym.xyz`
- **CLI binary**: `vg`
- **Python package**: `vimgym`
- **Runtime**: Python 3.11+, macOS

**The full technical specification is in `SPEC.md` at the root of this repo.**
Read it before writing any code. Every decision in this build is grounded in that spec.

---

## Your First Action: Read the Spec

Before writing a single line of code, read the full spec:

```bash
cat SPEC.md
```

The spec contains:
- Real Claude Code JSONL schema (inspected from actual session files)
- Exact database schema (SQLite, 5 tables)
- Exact module architecture (every file, every function signature)
- 37 atomic tasks across 6 sprints
- Edge cases that will bite you if ignored

Do not skip this step. Do not proceed from memory. Read it.

---

## Repository State Right Now

```
vimgym/
├── data/                          ← REAL Claude Code session files (your test data)
│   └── -Users-shoaibrain-edforge/
│       ├── 3438c55b-0df0-4bc0-811e-561afcf19350.jsonl   (4.8MB, 470 lines)
│       ├── 64778c29-ae52-4fc1-acda-0bcde0cfa08b.jsonl   (4.5MB, 707 lines)
│       ├── 64b0bec2-912d-45f8-8bd3-f804ea250cf5.jsonl   (0.3MB, 145 lines)
│       ├── 68568954-72e5-4038-afba-958ffec228eb.jsonl   (2.0MB, 488 lines)
│       ├── 1fb8b1b8-6cb3-4e34-8446-fa60ba5df626.jsonl   (293B, 1 line)
│       └── eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl   (1.5MB, 148 lines)
├── SPEC.md                        ← Full technical specification (READ THIS FIRST)
├── .gitignore
├── public/
└── src/                           (empty — you will create everything here)
```

The `data/` directory contains real (already redacted) Claude Code sessions from
the EdForge project. These are your test fixtures. All tests run against these files.
Do not use synthetic/mock session data. Use these real files.

---

## The data/ Directory — Critical Understanding

These session files ARE the test fixtures. The `data/` directory mirrors exactly
how Claude Code stores sessions on disk:

```
~/.claude/projects/
└── -Users-shoaibrain-edforge/     ← path-encoded: /Users/shoaibrain/edforge
    └── {UUID}.jsonl               ← one file per session
```

Path encoding rule: `/Users/shoaibrain/edforge` → `-Users-shoaibrain-edforge`
(forward slashes become dashes, leading slash becomes leading dash)

**In dev/test mode**: the watcher watches `./data/` instead of `~/.claude/projects/`
**In production**: the watcher watches `~/.claude/projects/`
**Switch**: `VIMGYM_WATCH_PATH=./data vg start` for dev mode

All pytest tests reference `data/` directly via:
```python
DATA_DIR = Path(__file__).parent.parent / "data" / "-Users-shoaibrain-edforge"
```

---

## The Actual JSONL Format (Ground Truth)

You have real files. Before building the parser, inspect the actual format:

```bash
# See all message types in a session
python3 -c "
import json
lines = open('data/-Users-shoaibrain-edforge/eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl').read().strip().split('\n')
types = {}
for l in lines:
    try:
        obj = json.loads(l)
        t = obj.get('type','?')
        types[t] = types.get(t,0) + 1
    except: pass
print(types)
"

# See one record of each type
python3 -c "
import json
lines = open('data/-Users-shoaibrain-edforge/3438c55b-0df0-4bc0-811e-561afcf19350.jsonl').read().strip().split('\n')
seen = set()
for l in lines:
    try:
        obj = json.loads(l)
        t = obj.get('type')
        if t not in seen:
            print(f'=== {t} ===')
            print(json.dumps(obj, indent=2)[:800])
            seen.add(t)
    except: pass
"
```

**The six message types you will encounter:**

| type | what it contains |
|---|---|
| `queue-operation` | `operation` (enqueue/dequeue), `timestamp`, `sessionId`, optional `content` (task XML) |
| `user` | `uuid`, `parentUuid`, `timestamp`, `cwd`, `gitBranch`, `entrypoint`, `version`, `slug`, `permissionMode`, `sessionId`, `isMeta`, `message.content[]` |
| `assistant` | `uuid`, `parentUuid`, `timestamp`, `requestId`, `message.model`, `message.content[]`, `message.usage{}` |
| `file-history-snapshot` | `messageId`, `snapshot.trackedFileBackups{}`, `isSnapshotUpdate` |
| `ai-title` | `sessionId`, `aiTitle` |
| `last-prompt` | `sessionId`, `lastPrompt` |

**Key fields on `user` messages:**
- `isMeta: true` → skip this message (it's internal tooling, not user content)
- `message.content[]` can contain: `{type:"text"}`, `{type:"image", source:{type:"base64", data:"..."}}`, `{type:"tool_result"}`
- `toolUseResult` at top level: result of tool invocation

**Key fields on `assistant` messages:**
- `message.content[]` can contain: `{type:"thinking"}`, `{type:"text"}`, `{type:"tool_use", name:"Bash", input:{...}}`
- `message.usage`: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`
- Tool names seen in real sessions: `Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Agent`, `TodoWrite`, `ToolSearch`, `ExitPlanMode`, `AskUserQuestion`
- `name == "Agent"` → subagent was spawned, companion directory exists

---

## Build Order — Follow This Exactly

Build sprint by sprint. Do not jump ahead. Each sprint must be fully working
with passing tests before starting the next.

```
Sprint 1 → Sprint 2 → Sprint 3 → Sprint 4 → Sprint 5 → Sprint 6
(parser)   (storage)  (daemon+   (web UI)   (distrib)   (harden)
                       REST API)
```

After completing each sprint, run the full test suite:
```bash
pytest tests/ -v --tb=short
```
All tests must pass before proceeding to the next sprint.

---

## Sprint 1: Build the Parser

**Goal**: `parse_session(path)` returns a fully populated `ParsedSession` dataclass.
Tested against all 5 real session files in `data/`.

**Sprint 1 demo** (must work before Sprint 2):
```bash
python3 -c "
from pathlib import Path
from vimgym.pipeline.parser import parse_session
import json

s = parse_session(Path('data/-Users-shoaibrain-edforge/eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl'))
print(json.dumps({
    'session_uuid': s.session_uuid,
    'ai_title': s.ai_title,
    'slug': s.slug,
    'cwd': s.cwd,
    'git_branch': s.git_branch,
    'tools_used': s.tools_used,
    'files_modified': s.files_modified[:3],
    'messages': len(s.messages),
    'has_subagents': s.has_subagents,
    'input_tokens': s.input_tokens,
    'output_tokens': s.output_tokens,
    'parse_errors': s.parse_errors,
}, indent=2))
"
```

### T1.1 — Scaffold: pyproject.toml, package structure, CLI stub

Create the complete project scaffold first. Nothing else works until this is done.

**Create `pyproject.toml`:**
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "vimgym"
version = "0.1.0"
description = "AI session memory for developers"
readme = "README.md"
requires-python = ">=3.11"
license = {text = "MIT"}
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "watchdog>=4.0",
    "httpx>=0.27",
    "rich>=13",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "httpx",
    "ruff",
    "mypy",
]

[project.scripts]
vg = "vimgym.cli:main"

[tool.setuptools.package-dir]
"" = "src"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
vimgym = ["ui/**/*", "ui/vendor/*"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

**Create directory structure:**
```bash
mkdir -p src/vimgym/pipeline
mkdir -p src/vimgym/storage
mkdir -p src/vimgym/ui/vendor
mkdir -p tests/integration tests/perf
mkdir -p defaults
```

**Create all `__init__.py` files and stub modules:**
```bash
touch src/vimgym/__init__.py
touch src/vimgym/pipeline/__init__.py
touch src/vimgym/storage/__init__.py
```

**`src/vimgym/__init__.py`:**
```python
__version__ = "0.1.0"
```

**`src/vimgym/cli.py`** (stub — expand in Sprint 3):
```python
"""Vimgym CLI — AI session memory for developers."""
import argparse
import sys

from vimgym import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vg",
        description="Vimgym — AI session memory for developers",
    )
    parser.add_argument("--version", action="version", version=f"vimgym {__version__}")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("start",  help="Start daemon (watcher + web server)")
    sub.add_parser("stop",   help="Stop daemon")
    sub.add_parser("status", help="Show daemon status and vault stats")
    sub.add_parser("open",   help="Open browser UI")

    search_p = sub.add_parser("search", help="Search sessions")
    search_p.add_argument("query", nargs="?", help="Search query")
    search_p.add_argument("--project", help="Filter by project name")
    search_p.add_argument("--branch", help="Filter by git branch")
    search_p.add_argument("--since", help="Filter by date (ISO or Nd: 7d, 30d)")
    search_p.add_argument("--limit", type=int, default=20)
    search_p.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Stubs — implemented in Sprint 3
    from rich.console import Console
    console = Console()
    console.print(f"[yellow][{args.command}]: not yet implemented[/yellow]")
    sys.exit(1)


if __name__ == "__main__":
    main()
```

**Install and verify:**
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
vg --version    # must print: vimgym 0.1.0
vg --help       # must list all commands
vg start        # must print: [start]: not yet implemented, exit 1
```

**Test** (`tests/test_cli.py`):
```python
import subprocess
import sys

def run(args):
    return subprocess.run(
        [sys.executable, "-m", "vimgym.cli"] + args,
        capture_output=True, text=True
    )

def test_version():
    r = run(["--version"])
    assert r.returncode == 0
    assert "0.1.0" in r.stdout

def test_help():
    r = run(["--help"])
    assert r.returncode == 0
    assert "start" in r.stdout
    assert "search" in r.stdout

def test_stub_commands():
    for cmd in ["start", "stop", "status", "open"]:
        r = run([cmd])
        assert r.returncode == 1
        assert "not yet implemented" in r.stdout or "not yet implemented" in r.stderr
```

---

### T1.2 — `defaults/` config files

```bash
cat > defaults/config.json << 'EOF'
{
  "vault_dir": "~/.vimgym",
  "watch_path": "~/.claude/projects",
  "server_host": "127.0.0.1",
  "server_port": 7337,
  "debounce_secs": 5.0,
  "stability_polls": 2,
  "stability_poll_interval": 1.0,
  "auto_open_browser": true,
  "log_level": "INFO"
}
EOF
```

```bash
cat > defaults/redaction-rules.json << 'EOF'
{
  "version": 1,
  "rules": [
    {"name": "anthropic_key",  "pattern": "sk-ant-[a-zA-Z0-9_\\-]{60,}",                       "replacement": "[REDACTED_ANTHROPIC_KEY]"},
    {"name": "openai_key",     "pattern": "sk-[a-zA-Z0-9_\\-]{40,}",                           "replacement": "[REDACTED_OPENAI_KEY]"},
    {"name": "aws_access",     "pattern": "AKIA[0-9A-Z]{16}",                                   "replacement": "[REDACTED_AWS_KEY]"},
    {"name": "aws_secret",     "pattern": "(?i)aws.secret.{0,20}[=:]\\s*[a-zA-Z0-9/+]{40}",   "replacement": "[REDACTED_AWS_SECRET]"},
    {"name": "bearer_token",   "pattern": "Bearer\\s+[a-zA-Z0-9._\\-]{20,}",                   "replacement": "Bearer [REDACTED]"},
    {"name": "github_token",   "pattern": "ghp_[a-zA-Z0-9_]{36,}",                             "replacement": "[REDACTED_GITHUB_TOKEN]"},
    {"name": "jwt",            "pattern": "eyJ[a-zA-Z0-9_\\-]+\\.[a-zA-Z0-9_\\-]+\\.[a-zA-Z0-9_\\-]+", "replacement": "[REDACTED_JWT]"},
    {"name": "database_url",   "pattern": "(mongodb|postgres|mysql|redis)://[^\\s]{8,}",        "replacement": "[REDACTED_DB_URL]"},
    {"name": "pem_block",      "pattern": "-----BEGIN [A-Z ]+-----[\\s\\S]+?-----END [A-Z ]+-----", "replacement": "[REDACTED_PEM_BLOCK]"},
    {"name": "env_secret",     "pattern": "(?i)(password|secret|api_key|private_key)\\s*=\\s*\\S{8,}", "replacement": "\\1=[REDACTED]"}
  ]
}
EOF
```

---

### T1.3 — `AppConfig` dataclass (`src/vimgym/config.py`)

```python
"""Vimgym configuration."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AppConfig:
    vault_dir: Path = field(default_factory=lambda: Path("~/.vimgym").expanduser())
    watch_path: Path = field(default_factory=lambda: Path("~/.claude/projects").expanduser())
    server_host: str = "127.0.0.1"
    server_port: int = 7337
    debounce_secs: float = 5.0
    stability_polls: int = 2
    stability_poll_interval: float = 1.0
    auto_open_browser: bool = True
    log_level: str = "INFO"

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


def load_config(vault_dir: Path | None = None) -> AppConfig:
    """Load config from file + environment variable overrides."""
    base = Path(
        os.environ.get("VIMGYM_PATH", str(Path("~/.vimgym").expanduser()))
    ).expanduser()
    if vault_dir:
        base = vault_dir

    config_file = base / "config.json"
    cfg = AppConfig(vault_dir=base)

    if config_file.exists():
        raw = json.loads(config_file.read_text())
        for key, value in raw.items():
            if hasattr(cfg, key):
                if key in ("vault_dir", "watch_path"):
                    setattr(cfg, key, Path(value).expanduser())
                else:
                    setattr(cfg, key, value)

    # Environment overrides
    if "VIMGYM_WATCH_PATH" in os.environ:
        cfg.watch_path = Path(os.environ["VIMGYM_WATCH_PATH"]).expanduser()
    if "VIMGYM_PORT" in os.environ:
        cfg.server_port = int(os.environ["VIMGYM_PORT"])

    return cfg


def save_config(cfg: AppConfig) -> None:
    """Write config atomically."""
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    config_file = cfg.vault_dir / "config.json"
    tmp = config_file.with_suffix(".tmp")
    data = {
        "vault_dir": str(cfg.vault_dir),
        "watch_path": str(cfg.watch_path),
        "server_host": cfg.server_host,
        "server_port": cfg.server_port,
        "debounce_secs": cfg.debounce_secs,
        "stability_polls": cfg.stability_polls,
        "stability_poll_interval": cfg.stability_poll_interval,
        "auto_open_browser": cfg.auto_open_browser,
        "log_level": cfg.log_level,
    }
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(config_file)
```

**Test** (`tests/test_config.py`):
```python
import os
from pathlib import Path
from vimgym.config import AppConfig, load_config, save_config

def test_defaults():
    cfg = AppConfig()
    assert cfg.server_port == 7337
    assert cfg.server_host == "127.0.0.1"
    assert cfg.debounce_secs == 5.0

def test_env_watch_path_override(tmp_path, monkeypatch):
    monkeypatch.setenv("VIMGYM_WATCH_PATH", str(tmp_path / "data"))
    cfg = load_config(vault_dir=tmp_path)
    assert cfg.watch_path == tmp_path / "data"

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
```

---

### T1.4 — `ParsedSession` and `ParsedMessage` dataclasses (`src/vimgym/pipeline/parser.py`)

Define the complete output types. These are the contracts that all other modules depend on.

```python
"""Claude Code JSONL parser — converts raw session files to structured data."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedMessage:
    uuid: str
    parent_uuid: str | None
    type: str                     # 'user' | 'assistant'
    role: str
    timestamp: str | None
    has_tool_use: bool = False
    has_thinking: bool = False
    has_image: bool = False
    tool_names: list[str] = field(default_factory=list)
    content_json: str = "[]"      # full content array as JSON, base64 images stripped


@dataclass
class ParsedSession:
    # Identity
    session_uuid: str
    slug: str | None
    ai_title: str | None
    last_prompt: str | None

    # Source
    source_path: str
    project_dir: str              # raw encoded: -Users-shoaibrain-edforge
    cwd: str | None               # /Users/shoaibrain/edforge
    git_branch: str | None
    entrypoint: str | None        # claude-vscode
    claude_version: str | None    # 2.1.89
    permission_mode: str | None   # default | plan

    # Time (ISO8601 strings)
    started_at: str | None
    ended_at: str | None

    # Messages
    messages: list[ParsedMessage] = field(default_factory=list)
    user_messages_text: str = ""  # concat for FTS indexing
    asst_messages_text: str = ""  # concat for FTS indexing

    # Derived
    tools_used: list[str] = field(default_factory=list)       # sorted unique
    files_modified: list[str] = field(default_factory=list)   # from Write/Edit/snapshot
    has_subagents: bool = False

    # Token accounting
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    # Storage
    raw_jsonl: str = ""           # full file, base64 images replaced with placeholder
    file_hash: str = ""           # SHA256 of original file (before any modification)
    parse_errors: list[str] = field(default_factory=list)
```

**Test** (`tests/test_parser.py`):
```python
from vimgym.pipeline.parser import ParsedSession, ParsedMessage

def test_parsed_message_defaults():
    msg = ParsedMessage(
        uuid="abc", parent_uuid=None, type="user",
        role="user", timestamp=None
    )
    assert msg.has_tool_use is False
    assert msg.tool_names == []
    assert msg.content_json == "[]"

def test_parsed_session_defaults():
    s = ParsedSession(
        session_uuid="abc", slug=None, ai_title=None, last_prompt=None,
        source_path="/tmp/x.jsonl", project_dir="-Users-test",
        cwd=None, git_branch=None, entrypoint=None,
        claude_version=None, permission_mode=None,
        started_at=None, ended_at=None,
    )
    assert s.tools_used == []
    assert s.has_subagents is False
    assert s.input_tokens == 0
```

---

### T1.5 — Core JSONL parser implementation (`src/vimgym/pipeline/parser.py`)

Implement `parse_session(filepath: Path) -> ParsedSession`.

**Critical implementation rules:**
1. Stream line by line — never `f.read()` on whole file (sessions up to 4.8MB)
2. `json.JSONDecodeError` → append to `parse_errors`, continue — never crash
3. `isMeta == True` on user messages → skip entirely
4. Image base64: replace `{"type":"image","source":{"type":"base64","data":"..."}}` with `{"type":"image","omitted":true}` in both `raw_jsonl` and `content_json`
5. Thinking blocks: keep in `content_json` but do NOT add to `asst_messages_text`
6. `file_hash` = SHA256 of raw file bytes BEFORE any modification

```python
def parse_session(filepath: Path) -> ParsedSession:
    """Parse a Claude Code JSONL session file into structured data."""
    raw_bytes = filepath.read_bytes()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    raw_lines = raw_bytes.decode("utf-8", errors="replace").splitlines()

    project_dir = filepath.parent.name  # e.g. -Users-shoaibrain-edforge

    session = ParsedSession(
        session_uuid="",
        slug=None,
        ai_title=None,
        last_prompt=None,
        source_path=str(filepath.resolve()),
        project_dir=project_dir,
        cwd=None,
        git_branch=None,
        entrypoint=None,
        claude_version=None,
        permission_mode=None,
        started_at=None,
        ended_at=None,
        file_hash=file_hash,
    )

    tools_used: set[str] = set()
    files_modified: set[str] = set()
    user_texts: list[str] = []
    asst_texts: list[str] = []
    redacted_lines: list[str] = []

    for line_num, raw_line in enumerate(raw_lines, 1):
        line = raw_line.strip()
        if not line:
            redacted_lines.append(raw_line)
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            session.parse_errors.append(f"line {line_num}: {e}")
            redacted_lines.append(raw_line)
            continue

        msg_type = obj.get("type", "")

        if msg_type == "queue-operation":
            if obj.get("operation") == "enqueue" and session.started_at is None:
                session.started_at = obj.get("timestamp")
            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]
            redacted_lines.append(line)

        elif msg_type == "user":
            if obj.get("isMeta"):
                redacted_lines.append(line)
                continue

            # Extract session-level fields (same on every message)
            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]
            if not session.cwd and obj.get("cwd"):
                session.cwd = obj["cwd"]
            if not session.git_branch and obj.get("gitBranch"):
                session.git_branch = obj["gitBranch"]
            if not session.entrypoint and obj.get("entrypoint"):
                session.entrypoint = obj["entrypoint"]
            if not session.claude_version and obj.get("version"):
                session.claude_version = obj["version"]
            if not session.slug and obj.get("slug"):
                session.slug = obj["slug"]
            if not session.permission_mode and obj.get("permissionMode"):
                session.permission_mode = obj["permissionMode"]

            timestamp = obj.get("timestamp")
            if timestamp:
                session.ended_at = timestamp  # update to last seen

            # Process content blocks
            content = obj.get("message", {}).get("content", [])
            cleaned_content, has_image, text_parts = _process_content_blocks(content, for_user=True)
            user_texts.extend(text_parts)

            msg = ParsedMessage(
                uuid=obj.get("uuid", f"_line_{line_num}"),
                parent_uuid=obj.get("parentUuid"),
                type="user",
                role="user",
                timestamp=timestamp,
                has_image=has_image,
                content_json=json.dumps(cleaned_content),
            )
            session.messages.append(msg)

            # Rebuild line with cleaned content
            obj_copy = dict(obj)
            if obj_copy.get("message", {}).get("content"):
                obj_copy["message"] = dict(obj_copy["message"])
                obj_copy["message"]["content"] = cleaned_content
            redacted_lines.append(json.dumps(obj_copy))

        elif msg_type == "assistant":
            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]

            timestamp = obj.get("timestamp")
            if timestamp:
                session.ended_at = timestamp

            # Token accounting
            usage = obj.get("message", {}).get("usage", {})
            session.input_tokens += usage.get("input_tokens", 0)
            session.output_tokens += usage.get("output_tokens", 0)
            session.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
            session.cache_write_tokens += usage.get("cache_creation_input_tokens", 0)

            content = obj.get("message", {}).get("content", [])
            cleaned_content, has_image, text_parts = _process_content_blocks(content, for_user=False)

            has_tool_use = False
            has_thinking = False
            msg_tools: list[str] = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    has_tool_use = True
                    tool_name = block.get("name", "")
                    tools_used.add(tool_name)
                    msg_tools.append(tool_name)
                    if tool_name in ("Write", "Edit"):
                        path = block.get("input", {}).get("path", "")
                        if path:
                            files_modified.add(path)
                    if tool_name == "Agent":
                        session.has_subagents = True
                elif btype == "thinking":
                    has_thinking = True
                elif btype == "text":
                    asst_texts.append(block.get("text", ""))

            msg = ParsedMessage(
                uuid=obj.get("uuid", f"_line_{line_num}"),
                parent_uuid=obj.get("parentUuid"),
                type="assistant",
                role="assistant",
                timestamp=timestamp,
                has_tool_use=has_tool_use,
                has_thinking=has_thinking,
                has_image=has_image,
                tool_names=msg_tools,
                content_json=json.dumps(cleaned_content),
            )
            session.messages.append(msg)

            obj_copy = dict(obj)
            if obj_copy.get("message", {}).get("content"):
                obj_copy["message"] = dict(obj_copy["message"])
                obj_copy["message"]["content"] = cleaned_content
            redacted_lines.append(json.dumps(obj_copy))

        elif msg_type == "file-history-snapshot":
            snapshot = obj.get("snapshot", {})
            for file_path in snapshot.get("trackedFileBackups", {}).keys():
                files_modified.add(file_path)
            redacted_lines.append(line)

        elif msg_type == "ai-title":
            session.ai_title = obj.get("aiTitle")
            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]
            redacted_lines.append(line)

        elif msg_type == "last-prompt":
            session.last_prompt = obj.get("lastPrompt")
            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]
            redacted_lines.append(line)

        else:
            # Unknown type — preserve as-is, log
            session.parse_errors.append(f"line {line_num}: unknown type '{msg_type}'")
            redacted_lines.append(line)

    # Finalize
    session.raw_jsonl = "\n".join(redacted_lines)
    session.user_messages_text = "\n\n".join(user_texts)
    session.asst_messages_text = "\n\n".join(asst_texts)
    session.tools_used = sorted(tools_used)
    session.files_modified = sorted(files_modified)[:50]

    return session


def _process_content_blocks(
    content: list, for_user: bool
) -> tuple[list, bool, list[str]]:
    """
    Process content blocks:
    - Strip base64 image data, replace with omission marker
    - Extract text strings for FTS indexing
    Returns: (cleaned_content, has_image, text_parts)
    """
    cleaned: list = []
    has_image = False
    text_parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            cleaned.append(block)
            continue

        btype = block.get("type")

        if btype == "image":
            has_image = True
            source = block.get("source", {})
            media_type = source.get("media_type", "image/unknown")
            cleaned.append({"type": "image", "omitted": True, "media_type": media_type})

        elif btype == "text":
            text = block.get("text", "")
            text_parts.append(text)
            cleaned.append(block)

        elif btype == "thinking":
            # Keep structure but omit from FTS text
            cleaned.append({"type": "thinking", "omitted": True})

        else:
            cleaned.append(block)

    return cleaned, has_image, text_parts
```

**Tests** (`tests/test_parser.py`) — add these tests:
```python
from pathlib import Path
from vimgym.pipeline.parser import parse_session

DATA_DIR = Path(__file__).parent.parent / "data" / "-Users-shoaibrain-edforge"

def test_parse_simple_session():
    path = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    s = parse_session(path)
    assert s.session_uuid == "eaa3009a-c5ab-4015-a3e5-af26622652f9"
    assert s.ai_title == "Resolve circular CloudFormation stack dependencies and delete"
    assert s.slug == "wise-purring-flute"
    assert s.cwd is not None
    assert s.git_branch is not None
    assert len(s.messages) > 0
    assert s.parse_errors == []  # no errors on clean file

def test_parse_agents_session():
    path = DATA_DIR / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl"
    s = parse_session(path)
    assert s.session_uuid == "3438c55b-0df0-4bc0-811e-561afcf19350"
    assert s.has_subagents is True
    assert "Agent" in s.tools_used
    assert "Bash" in s.tools_used
    assert "Edit" in s.tools_used
    assert len(s.tools_used) >= 8  # real session has 10 tools

def test_parse_minimal_session():
    path = DATA_DIR / "1fb8b1b8-6cb3-4e34-8446-fa60ba5df626.jsonl"
    s = parse_session(path)
    # 1-line file with only last-prompt record
    assert s.session_uuid != ""  # extracted from last-prompt
    assert s.parse_errors == []  # no errors, even on minimal file

def test_all_sessions_parse_without_crash():
    """All 5 real sessions must parse to completion without exceptions."""
    for path in DATA_DIR.glob("*.jsonl"):
        s = parse_session(path)
        assert s.session_uuid != "", f"No session UUID extracted from {path.name}"

def test_image_base64_not_in_raw_jsonl():
    """Base64 image data must be stripped from raw_jsonl storage."""
    for path in DATA_DIR.glob("*.jsonl"):
        s = parse_session(path)
        if s.messages and any(m.has_image for m in s.messages):
            # If images were present, they must be replaced
            assert '"omitted": true' in s.raw_jsonl or "omitted" in s.raw_jsonl
            # Raw base64 data should not appear
            # (base64 strings are long alphanumeric sequences)
            import re
            # A real base64 block would be >1000 chars
            assert not re.search(r'"data":\s*"[A-Za-z0-9+/]{1000,}', s.raw_jsonl)

def test_file_hash_is_stable():
    """Same file → same hash on multiple calls."""
    path = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    s1 = parse_session(path)
    s2 = parse_session(path)
    assert s1.file_hash == s2.file_hash

def test_token_counts_nonzero_for_large_session():
    path = DATA_DIR / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl"
    s = parse_session(path)
    # Real session has massive cache usage
    assert s.output_tokens > 0
    assert s.cache_read_tokens > 0
```

Run after T1.5:
```bash
pytest tests/test_parser.py -v
```
All tests must pass.

---

### T1.6 — Project name decoder and metadata extractor (`src/vimgym/pipeline/metadata.py`)

```python
"""Metadata extraction from parsed sessions."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SessionMetadata:
    session_uuid: str
    project_name: str
    duration_secs: int | None
    message_count: int
    user_message_count: int
    asst_message_count: int
    tool_use_count: int
    files_modified_display: list[str]  # paths relative to cwd


def decode_project_name(project_dir: str, cwd: str | None) -> str:
    """
    Derive human-readable project name.
    
    Primary: use cwd field (ground truth from JSONL).
      /Users/shoaibrain/edforge → 'edforge'
      /Users/shoaibrain/my-cool-api → 'my-cool-api'
    
    Fallback: parse encoded project_dir.
      -Users-shoaibrain-edforge → 'edforge'
    """
    if cwd:
        return Path(cwd).name or "unknown"

    # Fallback: encoded dir name. Leading dash = path separator.
    # Cannot safely reverse all dashes (original dashes vs separator dashes).
    # Best effort: take last component after last dash that follows a pattern.
    parts = project_dir.lstrip("-").split("-")
    return parts[-1] if parts else "unknown"


def extract_metadata(session: "ParsedSession") -> SessionMetadata:  # type: ignore[name-defined]
    """Extract structured metadata from a parsed session."""
    from vimgym.pipeline.parser import ParsedSession

    project_name = decode_project_name(session.project_dir, session.cwd)

    # Duration
    duration_secs: int | None = None
    if session.started_at and session.ended_at:
        try:
            start = datetime.fromisoformat(session.started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(session.ended_at.replace("Z", "+00:00"))
            duration_secs = max(0, int((end - start).total_seconds()))
        except (ValueError, TypeError):
            pass

    # Message counts
    user_count = sum(1 for m in session.messages if m.role == "user")
    asst_count = sum(1 for m in session.messages if m.role == "assistant")
    tool_count = sum(len(m.tool_names) for m in session.messages)

    # File paths relative to cwd
    cwd_prefix = (session.cwd or "").rstrip("/") + "/"
    files_display = [
        f.replace(cwd_prefix, "") if f.startswith(cwd_prefix) else f
        for f in session.files_modified
    ]

    return SessionMetadata(
        session_uuid=session.session_uuid,
        project_name=project_name,
        duration_secs=duration_secs,
        message_count=len(session.messages),
        user_message_count=user_count,
        asst_message_count=asst_count,
        tool_use_count=tool_count,
        files_modified_display=files_display,
    )
```

**Tests** (`tests/test_metadata.py`):
```python
from vimgym.pipeline.metadata import decode_project_name, extract_metadata
from vimgym.pipeline.parser import parse_session
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "-Users-shoaibrain-edforge"

def test_decode_project_name_from_cwd():
    assert decode_project_name("-Users-shoaibrain-edforge", "/Users/shoaibrain/edforge") == "edforge"

def test_decode_project_name_with_dashes_in_cwd():
    assert decode_project_name("-Users-x-my-cool-api", "/Users/x/my-cool-api") == "my-cool-api"

def test_decode_project_name_no_cwd():
    assert decode_project_name("-Users-shoaibrain-edforge", None) == "edforge"

def test_extract_metadata_real_session():
    path = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    s = parse_session(path)
    meta = extract_metadata(s)
    assert meta.project_name == "edforge"
    assert meta.duration_secs is not None
    assert meta.duration_secs > 0
    assert meta.message_count > 0
    assert meta.user_message_count > 0
    assert meta.asst_message_count > 0

def test_duration_computed_for_large_session():
    path = DATA_DIR / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl"
    s = parse_session(path)
    meta = extract_metadata(s)
    # 5h 20m session
    assert meta.duration_secs > 3600
```

---

### T1.7 — Heuristic summarizer (`src/vimgym/pipeline/summary.py`)

```python
"""Session summarization."""
from __future__ import annotations


def heuristic_summary(session: "ParsedSession") -> str:  # type: ignore[name-defined]
    """
    Generate a brief summary without calling any external API.
    
    Format: "{title}. {first_prompt[:120]}. Files: {top_3}. Tools: {tools}."
    Max 280 chars.
    """
    parts: list[str] = []

    title = session.ai_title or "Untitled session"
    parts.append(title)

    # First non-empty user prompt
    for msg in session.messages:
        if msg.role == "user" and msg.content_json != "[]":
            try:
                import json
                content = json.loads(msg.content_json)
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block["text"].strip()
                        if text:
                            truncated = text[:120] + ("..." if len(text) > 120 else "")
                            parts.append(truncated)
                            break
            except Exception:
                pass
            break

    if session.files_modified:
        top = session.files_modified[:3]
        suffix = "..." if len(session.files_modified) > 3 else ""
        from pathlib import Path
        names = [Path(f).name for f in top]
        parts.append(f"Files: {', '.join(names)}{suffix}")

    if session.tools_used:
        parts.append(f"Tools: {', '.join(session.tools_used[:5])}")

    summary = ". ".join(parts)
    if len(summary) > 280:
        summary = summary[:277] + "..."
    return summary
```

**Tests** (`tests/test_summary.py`):
```python
from vimgym.pipeline.summary import heuristic_summary
from vimgym.pipeline.parser import parse_session
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "-Users-shoaibrain-edforge"

def test_summary_length():
    for path in DATA_DIR.glob("*.jsonl"):
        s = parse_session(path)
        summary = heuristic_summary(s)
        assert len(summary) <= 280
        assert len(summary) > 0

def test_summary_contains_title():
    path = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    s = parse_session(path)
    summary = heuristic_summary(s)
    assert "CloudFormation" in summary or "Resolve" in summary
```

---

### T1.8 — `tests/conftest.py`

```python
"""Shared pytest fixtures for all tests."""
import pytest
from pathlib import Path
from vimgym.pipeline.parser import ParsedSession, parse_session
from vimgym.db import init_db

# The data/ directory at repo root is the single source of test fixtures.
# These are real Claude Code sessions (redacted before commit).
DATA_DIR = Path(__file__).parent.parent / "data" / "-Users-shoaibrain-edforge"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    """Root of real session test data."""
    assert DATA_DIR.exists(), f"data/ dir missing: {DATA_DIR}"
    return DATA_DIR


@pytest.fixture(scope="session")
def simple_session_path(data_dir) -> Path:
    """eaa3009a: 1.5MB, 148 lines, no subagents"""
    return data_dir / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"


@pytest.fixture(scope="session")
def agents_session_path(data_dir) -> Path:
    """3438c55b: 4.8MB, 470 lines, 6 subagents, 10 tools"""
    return data_dir / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl"


@pytest.fixture(scope="session")
def minimal_session_path(data_dir) -> Path:
    """1fb8b1b8: 293B, 1 line, last-prompt only"""
    return data_dir / "1fb8b1b8-6cb3-4e34-8446-fa60ba5df626.jsonl"


@pytest.fixture(scope="session")
def all_session_paths(data_dir) -> list[Path]:
    """All 5 real sessions — for batch/integration tests."""
    paths = sorted(data_dir.glob("*.jsonl"))
    assert len(paths) >= 5, f"Expected 5 session files, found {len(paths)}"
    return paths


@pytest.fixture(scope="session")
def parsed_simple(simple_session_path) -> ParsedSession:
    return parse_session(simple_session_path)


@pytest.fixture(scope="session")
def parsed_agents(agents_session_path) -> ParsedSession:
    return parse_session(agents_session_path)


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    """Fresh initialized database in tmp directory."""
    db_path = tmp_path / "vault.db"
    init_db(db_path)
    return db_path
```

**Sprint 1 done. Verify:**
```bash
pytest tests/ -v --tb=short
# Expected: all tests in test_cli.py, test_config.py, test_parser.py,
#           test_metadata.py, test_summary.py pass
```

---

## Sprint 2: Storage — SQLite + Redaction + Search

**Goal**: Full pipeline: parsed session → redacted → inserted to SQLite →
searchable via FTS5.

**Sprint 2 demo** (must work before Sprint 3):
```bash
python3 -c "
import sys
from pathlib import Path
from vimgym.config import AppConfig
from vimgym.db import init_db, get_connection
from vimgym.pipeline.orchestrator import process_session

cfg = AppConfig(vault_dir=Path('/tmp/vimgym-test'))
init_db(cfg.db_path)

result = process_session(
    Path('data/-Users-shoaibrain-edforge/eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl'),
    cfg
)
print(f'Backed up: {result.session_uuid}')
print(f'Skipped: {result.skipped}')
print(f'Error: {result.error}')

import sqlite3
conn = sqlite3.connect(cfg.db_path)
row = conn.execute('SELECT ai_title, project_name, duration_secs FROM sessions LIMIT 1').fetchone()
print(f'DB row: {row}')
fts = conn.execute(\"SELECT session_uuid FROM sessions_fts WHERE sessions_fts MATCH 'CloudFormation'\").fetchall()
print(f'FTS search result: {fts}')
"
```

### T2.1 — Database init (`src/vimgym/db.py`)

See full implementation in SPEC.md section "T2.1". Key points:
- WAL mode + `synchronous=NORMAL`
- FTS5 availability check at init
- Full schema from spec (5 tables + indexes)
- `chmod 600` on DB file after creation
- Thread-local connections via `threading.local()`

Run full tests:
```bash
pytest tests/test_db.py -v
```

### T2.2 — Redaction engine (`src/vimgym/pipeline/redact.py`)

See full implementation in SPEC.md section "T2.2". Key points:
- Compile all regex at `__init__`, not per call
- `redact_text(text)` — applies to plain strings
- `redact_session_raw(raw_jsonl)` — applies line by line, skips malformed JSON lines

```bash
pytest tests/test_redact.py -v
```

### T2.3 — Session writer (`src/vimgym/storage/writer.py`)

See SPEC.md section "T2.3". Key points:
- Single transaction covering all 4 tables
- FTS: DELETE existing row then INSERT (update not supported in FTS5)
- `projects` table: upsert with running aggregates

```bash
pytest tests/test_writer.py -v
```

### T2.4 — Pipeline orchestrator (`src/vimgym/pipeline/orchestrator.py`)

```python
"""Pipeline orchestrator: coordinates parse → redact → metadata → write."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vimgym.config import AppConfig
from vimgym.db import get_connection
from vimgym.pipeline.metadata import extract_metadata
from vimgym.pipeline.parser import parse_session
from vimgym.pipeline.redact import RedactionEngine
from vimgym.pipeline.summary import heuristic_summary
from vimgym.storage.writer import session_exists_by_hash, session_exists_by_uuid, upsert_session

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    session_uuid: str = ""
    project_name: str = ""
    skipped: bool = False
    skip_reason: str = ""
    error: str | None = None
    duration_secs: int | None = None
    message_count: int = 0


def process_session(filepath: Path, config: AppConfig) -> ProcessResult:
    """
    Full pipeline: JSONL file → parsed → redacted → stored in SQLite.
    
    Never raises. All errors captured in ProcessResult.error.
    Safe to call from watcher thread.
    """
    try:
        return _process(filepath, config)
    except Exception as e:
        logger.exception(f"Unhandled error processing {filepath}: {e}")
        return ProcessResult(error=str(e))


def _process(filepath: Path, config: AppConfig) -> ProcessResult:
    conn = get_connection(config.db_path)
    engine = RedactionEngine(config.rules_path)

    # Step 1: Parse (also computes file_hash)
    session = parse_session(filepath)

    if not session.session_uuid:
        return ProcessResult(
            error=f"Could not extract session UUID from {filepath.name}"
        )

    # Step 2: Dedup checks
    if session_exists_by_hash(conn, session.file_hash):
        return ProcessResult(
            session_uuid=session.session_uuid,
            skipped=True,
            skip_reason="file_hash already indexed",
        )
    if session_exists_by_uuid(conn, session.session_uuid):
        return ProcessResult(
            session_uuid=session.session_uuid,
            skipped=True,
            skip_reason="session_uuid already indexed",
        )

    # Step 3: Redact
    session.raw_jsonl = engine.redact_session_raw(session.raw_jsonl)
    session.user_messages_text = engine.redact_text(session.user_messages_text)
    session.asst_messages_text = engine.redact_text(session.asst_messages_text)

    # Step 4: Extract metadata
    metadata = extract_metadata(session)

    # Step 5: Summarize
    summary = heuristic_summary(session)

    # Step 6: Write to DB
    upsert_session(conn, session, metadata, summary)
    conn.commit()

    logger.info(
        f"backed_up session={session.session_uuid[:8]} "
        f"project={metadata.project_name} "
        f"messages={metadata.message_count}"
    )

    return ProcessResult(
        session_uuid=session.session_uuid,
        project_name=metadata.project_name,
        duration_secs=metadata.duration_secs,
        message_count=metadata.message_count,
    )
```

**Integration test** (`tests/test_orchestrator.py`):
```python
from pathlib import Path
from vimgym.config import AppConfig
from vimgym.db import init_db, get_connection
from vimgym.pipeline.orchestrator import process_session

DATA_DIR = Path(__file__).parent.parent / "data" / "-Users-shoaibrain-edforge"

def test_full_pipeline_inserts_to_db(tmp_path):
    cfg = AppConfig(vault_dir=tmp_path)
    init_db(cfg.db_path)

    path = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    result = process_session(path, cfg)

    assert result.error is None
    assert result.skipped is False
    assert result.session_uuid == "eaa3009a-c5ab-4015-a3e5-af26622652f9"
    assert result.project_name == "edforge"

    conn = get_connection(cfg.db_path)
    row = conn.execute(
        "SELECT ai_title, project_name FROM sessions WHERE session_uuid = ?",
        (result.session_uuid,)
    ).fetchone()
    assert row is not None
    assert "CloudFormation" in row["ai_title"]

def test_dedup_skips_second_call(tmp_path):
    cfg = AppConfig(vault_dir=tmp_path)
    init_db(cfg.db_path)
    path = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"

    r1 = process_session(path, cfg)
    r2 = process_session(path, cfg)

    assert r1.skipped is False
    assert r2.skipped is True

def test_all_sessions_pipeline(tmp_path):
    """All 5 sessions must complete the full pipeline without error."""
    cfg = AppConfig(vault_dir=tmp_path)
    init_db(cfg.db_path)

    for path in sorted(DATA_DIR.glob("*.jsonl")):
        result = process_session(path, cfg)
        assert result.error is None, f"Pipeline error on {path.name}: {result.error}"

def test_fts_search_after_backup(tmp_path):
    cfg = AppConfig(vault_dir=tmp_path)
    init_db(cfg.db_path)

    # Back up the CORS session
    path = DATA_DIR / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl"
    process_session(path, cfg)

    conn = get_connection(cfg.db_path)
    results = conn.execute(
        "SELECT session_uuid FROM sessions_fts WHERE sessions_fts MATCH 'CORS'",
    ).fetchall()
    assert len(results) > 0
    assert any("3438c55b" in r["session_uuid"] for r in results)
```

### T2.5 — Search queries (`src/vimgym/storage/queries.py`)

See SPEC.md section "T2.5" for the full SQL. Key queries:
- `search_sessions()`: FTS5 MATCH + metadata JOIN + BM25 rank
- `list_sessions()`: structured filters, no FTS
- `get_session()`: prefix match, raises `AmbiguousIDError`
- `get_stats()`: aggregates

```bash
pytest tests/test_queries.py -v
```

**Sprint 2 done. Verify all sessions backed up and searchable:**
```bash
pytest tests/ -v --tb=short
# Must pass: test_db, test_redact, test_writer, test_orchestrator, test_queries
```

---

## Sprint 3: Daemon + REST API + CLI

**Goal**: `vg start` runs. All API endpoints return real data.
`vg search "CORS"` returns the right session from terminal.

**Sprint 3 demo:**
```bash
VIMGYM_WATCH_PATH=./data vg start &
sleep 2
curl http://localhost:7337/health
curl "http://localhost:7337/api/sessions" | python3 -m json.tool | head -40
curl "http://localhost:7337/api/search?q=CORS" | python3 -m json.tool | head -40
vg search "CORS"
vg stop
```

Follow SPEC.md sections T3.1 through T3.6:
- `T3.1`: AppConfig (already done in T1.3)
- `T3.2`: Watcher (`src/vimgym/watcher.py`) — watchdog, debounce, backfill
- `T3.3`: Daemon process manager (`src/vimgym/daemon.py`) — PID file, SIGTERM
- `T3.4`: FastAPI server (`src/vimgym/server.py`) — all routes, WebSocket, CORS
- `T3.5`: Wire `vg start/stop/status/open` in `cli.py`
- `T3.6`: Wire `vg search` in `cli.py`

**Critical watcher edge cases (do not skip):**
- Debounce must reset timer on each new event for same path
- File stability check: size unchanged across 2 polls (1s apart)
- On startup: scan all `*.jsonl` in watch_path, call `process_session` for any not yet in DB (backfill)
- Never crash on malformed session — log error, return, continue watching

```bash
pytest tests/test_watcher.py tests/test_server.py tests/test_daemon.py -v
```

---

## Sprint 4: Web UI

**Goal**: Browser UI at `localhost:7337`. Three panes. Search. Session detail with
full conversation rendering. Export to markdown.

**Sprint 4 demo:**
```bash
VIMGYM_WATCH_PATH=./data vg start
# Browser opens automatically
# 1. Search "CORS" using Cmd+K
# 2. Click result — see full conversation
# 3. Click "Export Markdown" — file downloads
```

Build in this order:
1. **HTML/CSS shell** (`src/vimgym/ui/index.html`, `style.css`): three-pane grid, dark mode
2. **Session inbox** (`app.js`): fetch `/api/sessions`, render cards, pagination
3. **Sidebar** (`app.js`): projects, branches, tools from `/api/projects` + `/api/stats`
4. **Session detail** (`app.js`): fetch `/api/sessions/:uuid`, render messages
5. **Message renderer** (`app.js`): user/assistant cards, tool_use as `<details>`, syntax highlight
6. **Command palette** (`app.js`): `Cmd+K`, debounced search, keyboard nav
7. **Export button**: `GET /api/sessions/:uuid/export?format=markdown`
8. **WebSocket live updates**: prepend new session card on watcher event

**No frameworks. No build step. Vanilla JS only.**
**Bundle `highlight.js` locally** — download to `src/vimgym/ui/vendor/highlight.min.js`.
**No CDN references in any HTML or JS file.**

```bash
pytest tests/test_ui.py -v  # FastAPI TestClient tests for UI serving
```

---

## Sprint 5: Distribution

**Goal**: `brew install vimgym` on clean macOS. `vimgym.xyz` live.

Follow SPEC.md Sprint 5:
- `T5.1`: `vg start` auto-inits vault on first run
- `T5.2`: `python -m build` → wheel; `twine check` passes
- `T5.3`: `Formula/vimgym.rb` Homebrew formula
- `T5.4`: `install.sh` + vimgym.xyz static site
- `T5.5`: `.github/workflows/test.yml` GitHub Actions CI

---

## Sprint 6: Hardening

Follow SPEC.md Sprint 6:
- `T6.1`: `bandit` security audit, `chmod 600` on DB, `127.0.0.1` bind only
- `T6.2`: Schema versioning, `vg upgrade` command
- `T6.3`: Graceful error handling, structured logging
- `T6.4`: `.vimgymignore` support
- `T6.5`: Performance baseline (500 sessions < 500ms search)

---

## Rules You Must Follow at All Times

### 1. Tests are not optional
Every task has a test. Write the test. Run it. It must pass before the task is done.
Do not proceed to the next task with failing tests.

```bash
# After every task:
pytest tests/ -v --tb=short
```

### 2. Use real data, not mocks
All parser tests run against the real JSONL files in `data/`. Never substitute
with synthetic JSON. If a test requires a session file, use one from `data/`.

### 3. Read the SPEC before implementing
If a task says "see SPEC.md section X", read that section before writing code.
The spec contains exact field names, exact SQL, exact function signatures.
Do not guess. Do not improvise. Implement what is specified.

### 4. Never load a whole JSONL file with f.read()
Always stream line by line. Files are up to 4.8MB. `raw_bytes = filepath.read_bytes()`
is allowed for hash computation only — do not process the whole file as a string.

### 5. Never crash on malformed input
Every `json.loads()` call is wrapped in try/except. Every file operation is
wrapped in try/except. Log the error. Return a result with `error` set. Continue.

### 6. Server binds 127.0.0.1 only, never 0.0.0.0
This is a local tool. Security requirement. Validate in `AppConfig.server_host`
setter — raise `ValueError` if value is `"0.0.0.0"`.

### 7. Image base64 never reaches the database
The parser strips base64 data from `raw_jsonl` and `content_json`. The writer
writes those fields to the DB. At no point does a multi-MB base64 string get
stored in SQLite.

### 8. Commit after every sprint
```bash
git add -A
git commit -m "sprint N: <what was built>"
git push origin main
```

---

## Verification Commands

Run these to confirm each sprint is complete before moving on.

**After Sprint 1:**
```bash
pytest tests/test_cli.py tests/test_config.py tests/test_parser.py \
       tests/test_metadata.py tests/test_summary.py -v
vg --version   # prints: vimgym 0.1.0
# Manual: parse real session
python3 -c "
from pathlib import Path
from vimgym.pipeline.parser import parse_session
s = parse_session(Path('data/-Users-shoaibrain-edforge/3438c55b-0df0-4bc0-811e-561afcf19350.jsonl'))
print('UUID:', s.session_uuid)
print('Title:', s.ai_title)
print('Tools:', s.tools_used)
print('Has subagents:', s.has_subagents)
print('Output tokens:', s.output_tokens)
print('Errors:', s.parse_errors)
"
```

**After Sprint 2:**
```bash
pytest tests/ -v
# Manual: full pipeline
python3 -c "
from pathlib import Path
from vimgym.config import AppConfig
from vimgym.db import init_db, get_connection
from vimgym.pipeline.orchestrator import process_session
import sqlite3

cfg = AppConfig(vault_dir=Path('/tmp/vg-test'))
init_db(cfg.db_path)
for p in sorted(Path('data/-Users-shoaibrain-edforge').glob('*.jsonl')):
    r = process_session(p, cfg)
    print(f'{p.name[:8]}: uuid={r.session_uuid[:8]} skip={r.skipped} err={r.error}')
conn = sqlite3.connect(cfg.db_path)
conn.row_factory = sqlite3.Row
n = conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
print(f'Total sessions in DB: {n}')
results = conn.execute(\"SELECT ai_title FROM sessions_fts WHERE sessions_fts MATCH 'CORS' LIMIT 3\").fetchall()
print('FTS search CORS:', [r[0] for r in results])
"
```

**After Sprint 3:**
```bash
VIMGYM_WATCH_PATH=./data vg start &
sleep 3
curl -s http://localhost:7337/health | python3 -m json.tool
curl -s "http://localhost:7337/api/sessions?limit=5" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'Total sessions: {d[\"total\"]}')
for s in d['sessions'][:3]:
    print(f'  {s[\"session_uuid\"][:8]}: {s[\"ai_title\"]}')
"
curl -s "http://localhost:7337/api/search?q=CORS" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'Search results: {d[\"total\"]}')
for r in d['results'][:2]:
    print(f'  {r[\"ai_title\"]}: {r[\"snippet\"][:80]}')
"
vg search "CORS"
vg status
vg stop
```

**After Sprint 4:**
```bash
VIMGYM_WATCH_PATH=./data vg start
# Browser opens. Manually verify:
# 1. Sessions visible in inbox
# 2. Cmd+K opens search, results appear
# 3. Click session → messages rendered with syntax highlight
# 4. "Export Markdown" downloads a file
# 5. Copy a new .jsonl to data/ → new session appears in inbox within 10s
```
