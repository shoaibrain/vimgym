# Vimgym — Developer Reference

> Architecture, module-level reference, schemas, and extension points for v0.1.

This document is the source of truth for engineers working *on* vimgym. For end-user
docs see [GUIDE.md](GUIDE.md).

---

## Architecture Overview

Vimgym is a single-process Python daemon. Inside that process two threads share one
SQLite database via WAL mode: a watchdog observer that detects new Claude Code
JSONL files, and a uvicorn ASGI server that serves the REST API + Web UI.

```
                        ┌──────────────────────┐
                        │   ~/.claude/projects │
                        │  (other AI tools)    │
                        └──────────┬───────────┘
                                   │ FS events
                                   ▼
┌─────────────────────────────────────────────────────────────┐
│ vimgym daemon (single Python process)                        │
│                                                              │
│  ┌─────────────────────┐         ┌──────────────────────┐   │
│  │ Thread 1            │         │ Thread 2 (asyncio)   │   │
│  │ watchdog Observer   │         │ uvicorn / FastAPI    │   │
│  │   ↓ debounce 5s     │         │   /health            │   │
│  │   ↓ stability poll  │         │   /api/sessions      │   │
│  │   ↓ orchestrator    │         │   /api/search        │   │
│  │     parse           │         │   /api/config/...    │   │
│  │     redact          │         │   /ws (push)         │   │
│  │     metadata        │         │   /  (static UI)     │   │
│  │     summary         │         │                      │   │
│  │     upsert          │         │                      │   │
│  └──────────┬──────────┘         └──────────┬───────────┘   │
│             │                                ▲              │
│             │ writes                         │ reads        │
│             ▼                                │              │
│            ┌─────────────────────────────────┴─┐            │
│            │  ~/.vimgym/vault.db (SQLite WAL)  │            │
│            │  schema v2: 5 tables + FTS5       │            │
│            └────────────────────┬──────────────┘            │
│                                 │                            │
│             ┌───────────────────┴───────────┐               │
│             │ events.event_queue (in-proc)  │               │
│             │ watcher → broadcaster → ws    │               │
│             └───────────────────────────────┘               │
└─────────────────────────────────────────────────────────────┘
```

**Why one process, two threads, WAL?**

- **One process** keeps deployment trivial (single PID, single PID file, single
  Homebrew service block) and avoids IPC.
- **Two threads** because the watcher does blocking syscalls (`os.path.getsize`,
  `time.sleep` for stability polling) that would starve the asyncio loop.
- **WAL mode** lets the asyncio thread read while the watcher thread writes
  without lock contention. We have one writer, many readers — the textbook WAL
  case.
- **In-process queue** for live updates instead of pub/sub or sockets — the
  watcher publishes to `events.event_queue`, the broadcaster pumps it into the
  WebSocket fanout. No serialization, no extra moving parts.

---

## Repository Structure

```
vimgym/
├── src/vimgym/
│   ├── __init__.py             # __version__ = "0.1.0"
│   ├── cli.py                  # vg start/stop/status/open/search/init/config
│   ├── config.py               # AppConfig, SourceConfig, detect_sources, init_vault
│   ├── db.py                   # SQLite init + WAL setup
│   ├── daemon.py               # PID file, start/stop/run_foreground
│   ├── events.py               # cross-thread queue.Queue event bus
│   ├── watcher.py              # SessionWatcher (one per source), backfill
│   ├── server.py               # FastAPI app factory + lifespan + all routes
│   ├── pipeline/
│   │   ├── parser.py           # ParsedSession, ParsedMessage, parse_session()
│   │   ├── redact.py           # RedactionEngine (compiled regex)
│   │   ├── metadata.py         # extract_metadata, decode_project_name
│   │   ├── summary.py          # heuristic_summary (≤280 chars)
│   │   └── orchestrator.py     # process_session() — full pipeline, never raises
│   ├── storage/
│   │   ├── writer.py           # upsert_session() — single transaction, 5 tables
│   │   ├── queries.py          # search/list/get/stats/timeline
│   │   └── export.py           # render_session_markdown()
│   └── ui/
│       ├── index.html          # 3-pane shell
│       ├── style.css           # Neon Void design tokens + components
│       ├── app.js              # vanilla JS, no framework
│       └── vendor/
│           └── highlight.min.js  # bundled, no CDN
│
├── tests/                      # 117 tests, see "Testing Guide" below
├── data/-Users-shoaibrain-edforge/   # 6 real Claude Code sessions used as fixtures
├── defaults/
│   ├── config.json             # default AppConfig values
│   └── redaction-rules.json    # 18 redaction patterns
├── docs/
│   ├── DEVELOPER.md            # this file
│   ├── GUIDE.md                # user guide
│   └── index.html              # vimgym.xyz landing page
├── Formula/vimgym.rb           # Homebrew formula (with brew services block)
├── install.sh                  # curl | sh installer (Homebrew/pipx/pip)
├── .github/workflows/test.yml  # CI: ruff + mypy + pytest + build + shellcheck
├── MANIFEST.in
├── pyproject.toml
├── LICENSE                     # MIT
└── README.md
```

---

## Tech Stack

| Component | Technology | Why |
|---|---|---|
| Language | Python 3.11+ | Modern type hints, dataclasses, asyncio maturity |
| HTTP server | FastAPI 0.110+ + uvicorn | Async, automatic OpenAPI, native WebSocket support |
| Database | SQLite + FTS5 | Zero-install, single-file, BM25 ranking out of the box |
| File watching | watchdog 4.x | Cross-platform, mature, handles macOS FSEvents |
| CLI | argparse + rich | argparse for parsing, rich for tables and color |
| HTTP client | httpx 0.27+ | Sync + async, used by `vg search` and tests |
| Front-end | Vanilla JS, no framework | Zero build step, ships as static files in the wheel |
| Code highlighting | highlight.js 11.9 | Bundled locally, no CDN dependency |
| Tests | pytest + pytest-asyncio + httpx TestClient | Standard, fast, real-fixture friendly |
| Lint | ruff | Single tool replaces flake8 + isort + pyupgrade |
| Type check | mypy | Catches the bugs ruff misses |
| Distribution | Homebrew formula + curl install + PyPI wheel | Three install paths cover every developer's setup |

---

## Data Flow: Backup Pipeline

What happens between Claude Code writing a JSONL file and that session being
searchable in vimgym:

```
1. Claude Code writes/appends   ~/.claude/projects/-Users-X-myproject/{UUID}.jsonl
                                                │
2. watchdog FSEvent  ───────────────────────────┘
   on_created OR on_modified
                                                │
3. SessionWatcher._is_session_file()  ──────────┘
   filter: must end .jsonl
   filter: must NOT contain /subagents/ or /tool-results/
   filter: must NOT be a hidden file (.something)
                                                │
4. SessionWatcher._schedule()  ─────────────────┘
   per-path threading.Timer with 5s debounce
   re-fires reset the timer (Claude Code may write in bursts)
                                                │
5. SessionWatcher._wait_for_stability()  ───────┘
   poll os.path.getsize() until two consecutive reads agree
   capped at 15s — beyond that, log warning and proceed anyway
                                                │
6. orchestrator.process_session()  ─────────────┘
   wrapped in try/except — NEVER raises
   ┌──────────────────────────────────────────┐
   │ a. parser.parse_session(path)            │
   │    streams line-by-line, never read()    │
   │    handles 6 message types               │
   │    strips base64 image data              │
   │    omits thinking block content          │
   │    computes SHA256 of original bytes     │
   │ b. dedup check                            │
   │    session_exists_by_hash(file_hash)?    │
   │    session_exists_by_uuid(session_uuid)? │
   │    either → return ProcessResult(skipped)│
   │ c. redact.RedactionEngine                 │
   │    18 compiled regex patterns            │
   │    apply to raw_jsonl + user/asst text   │
   │ d. metadata.extract_metadata             │
   │    project_name from cwd (or fallback)   │
   │    duration, message counts, tool counts │
   │ e. summary.heuristic_summary             │
   │    title + first prompt + files + tools  │
   │    ≤280 chars, no API call               │
   │ f. writer.upsert_session                  │
   │    single transaction across 5 tables:   │
   │    sessions / sessions_raw / sessions_fts│
   │    / messages / projects                 │
   │    FTS5 is DELETE+INSERT (no UPDATE)     │
   └──────────────────────────────────────────┘
                                                │
7. events.publish({type: "session_added", ...}) ┘
   in-process queue.Queue
                                                │
8. server lifespan broadcaster pump  ───────────┘
   asyncio task, polls queue with 0.25s timeout
   forwards to all open WebSocket clients
                                                │
9. browser app.js handleNewSession()  ──────────┘
   prepends card to inbox with green-flash animation
   shows toast bottom-right
   refreshes /api/stats counters
```

**Error isolation** — `process_session()` is the only call site allowed to raise
on parse errors, and even it catches all exceptions and returns
`ProcessResult(error=str)`. The watcher logs and moves on. A single bad file
never takes down the daemon.

---

## Module Reference

### `src/vimgym/config.py`

**`SourceConfig`** dataclass — one per configured AI tool watch path:

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | `str` | required | Unique slug, used as `source_id` in DB rows |
| `name` | `str` | required | Display name in sidebar / settings UI |
| `type` | `str` | required | `"claude_code"` is the only parser available in v1; others (`"unknown"`) are detected but disabled |
| `path` | `str` | required | Raw path string, may contain `~` |
| `enabled` | `bool` | `True` | Watcher only schedules enabled sources |
| `auto_detected` | `bool` | `False` | True if added by `detect_sources()` |
| `expanded_path` | `Path` (property) | — | `Path(path).expanduser()` |
| `exists()` | `bool` | — | Whether the expanded path exists on disk |

**`AppConfig`** dataclass — top-level config:

| Field | Type | Default |
|---|---|---|
| `vault_dir` | `Path` | `~/.vimgym` |
| `server_host` | `str` | `127.0.0.1` (never `0.0.0.0`, security) |
| `server_port` | `int` | `7337` |
| `auto_open_browser` | `bool` | `True` |
| `log_level` | `str` | `"INFO"` |
| `debounce_secs` | `float` | `5.0` |
| `stability_polls` | `int` | `2` |
| `stability_poll_interval` | `float` | `1.0` |
| `sources` | `list[SourceConfig]` | `[]` |
| `enabled_sources` (property) | filters `enabled and exists()` |
| `watch_paths` (property) | `[s.expanded_path for s in enabled_sources]` |
| `watch_path` (property) | first enabled path; legacy compat for tests |
| `db_path` / `pid_path` / `log_path` / `rules_path` | derived from `vault_dir` |

**`KNOWN_SOURCES`** — registry consulted by `detect_sources()`:

```python
KNOWN_SOURCES = [
    {"id": "claude_code",  "type": "claude_code", "check_path": "~/.claude",      "watch_path": "~/.claude/projects"},
    {"id": "cursor",       "type": "unknown",     "check_path": "~/.cursor",      "watch_path": "~/.cursor"},
    {"id": "copilot",      "type": "unknown",     "check_path": "~/.copilot",     "watch_path": "~/.copilot"},
    {"id": "antigravity",  "type": "unknown",     "check_path": "~/.antigravity", "watch_path": "~/.antigravity"},
    {"id": "gemini",       "type": "unknown",     "check_path": "~/.gemini",      "watch_path": "~/.gemini"},
]
```

A source is enabled by default only if `type == "claude_code"`. Everything else
is detected so the user can see it in `vg config sources`, but disabled until a
parser ships.

**`detect_sources(home_dir=None)`** scans `home_dir` (defaults to `Path.home()`)
for `KNOWN_SOURCES.check_path` entries. The `home_dir` argument is the test
hook that lets `tmp_path` stand in for `$HOME` without scanning the real disk
— `~`-prefixed `watch_path` values are re-anchored on the supplied home_dir.

**Environment overrides:**

| Env var | Effect |
|---|---|
| `VIMGYM_PATH` | Overrides `vault_dir` (the `~/.vimgym` location) |
| `VIMGYM_PORT` | Overrides `server_port` |
| `VIMGYM_WATCH_PATH` | Replaces *all* `sources[]` with a single source `id="env_override"`. Used for the dev workflow `VIMGYM_WATCH_PATH=./data vg start`. Never forwarded by `start_daemon()` to the child process — the child reads its own on-disk config. |

**`init_vault(cfg=None)`** — creates `vault_dir`, **always re-runs**
`detect_sources()`, merges the result into the existing config by `id`
(preserving the user's `enabled` toggles, refreshing path/type/name), persists
via `save_config()`. Returns `(cfg, newly_added_sources)`. Called by `vg init`
and lazily by `vg start`. Re-running `vg init` after installing a new AI tool
picks it up automatically; user-disabled sources are NOT silently re-enabled.

---

### `src/vimgym/db.py`

`init_db(db_path)` is idempotent: creates the parent dir with `0o700`, opens a
connection with `check_same_thread=False`, sets `PRAGMA journal_mode=WAL`,
`PRAGMA synchronous=NORMAL`, `PRAGMA foreign_keys=ON`, validates FTS5 by
creating and dropping a temp virtual table, executes the `SCHEMA_DDL` script,
seeds `schema_version` to `1`, then `chmod 600`s the database file.

`get_connection(db_path)` returns a thread-local `sqlite3.Connection`. Each
thread gets its own connection (cached in `threading.local()`); WAL mode
serializes writes safely. `row_factory = sqlite3.Row` for dict-like access.

**Schema:** see [Database Schema Reference](#database-schema-reference) below.

> **Note on migrations.** v0.1 ships with a single schema. There is no
> migration runner — everything in `SCHEMA_DDL` is the initial state. If a
> future schema change is required, the migration framework lives a `git
> log` away (it was prototyped during Sprint 5 development) and can be
> reintroduced cleanly. For now, "delete `~/.vimgym/vault.db` and re-run
> `vg start`" is the supported upgrade path.

---

### `src/vimgym/pipeline/parser.py`

**`ParsedMessage`** — one per non-meta message:

| Field | Type | Notes |
|---|---|---|
| `uuid` | `str` | From `obj.uuid`; falls back to `_line_{N}` if absent |
| `parent_uuid` | `str \| None` | From `obj.parentUuid` |
| `type`, `role` | `str` | `"user"` or `"assistant"` |
| `timestamp` | `str \| None` | ISO8601 from JSONL |
| `has_tool_use`, `has_thinking`, `has_image` | `bool` | Flag bits for the inbox UI |
| `tool_names` | `list[str]` | Tool names invoked in this message |
| `content_json` | `str` | Full content array as JSON, with images and thinking blocks replaced by `{omitted: true}` markers |

**`ParsedSession`** — output of `parse_session()`. Selected fields:

| Field | Notes |
|---|---|
| `session_uuid`, `slug`, `ai_title`, `last_prompt` | Identity |
| `source_path`, `project_dir`, `cwd`, `git_branch`, `entrypoint`, `claude_version`, `permission_mode` | Source context |
| `started_at`, `ended_at` | First and last timestamps observed |
| `messages` | `list[ParsedMessage]` |
| `user_messages_text`, `asst_messages_text` | Concatenated text for FTS indexing (no thinking content) |
| `tools_used` | sorted unique list |
| `files_modified` | sorted unique, capped at 50, populated from Write/Edit `file_path` AND `file-history-snapshot.trackedFileBackups` keys |
| `has_subagents` | `True` if any `tool_use` had `name == "Agent"` |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | Summed from assistant `message.usage` |
| `raw_jsonl` | Full file with image base64 and thinking content stripped |
| `file_hash` | SHA256 of the *original* bytes, computed before any modification |
| `parse_errors` | `list[str]` of `"line N: ..."` errors |
| `source_id` | Set by orchestrator just before upsert; defaults to `"claude_code"` |

**`parse_session(filepath)`** behavior, by message type:

| `type` field | Action |
|---|---|
| `queue-operation` | First `enqueue` sets `started_at`. SessionId captured if not yet known. |
| `user` (with `isMeta: True`) | **Skipped entirely** — internal tooling, not user content. |
| `user` | Extract session-level fields (cwd, branch, entrypoint, version, slug, permissionMode); accumulate text content; record `has_image` if any block is `type=image`. |
| `assistant` | Sum `message.usage` tokens; iterate `content[]`; record `has_tool_use`/`has_thinking`/`has_image`; collect `tool_names`; if `Write`/`Edit`, extract `input.file_path` (or `input.path` as fallback) into `files_modified`; if `Agent`, set `has_subagents=True`. |
| `file-history-snapshot` | Add every `snapshot.trackedFileBackups` key to `files_modified`. |
| `ai-title` | Set `session.ai_title`. |
| `last-prompt` | Set `session.last_prompt`. |
| Unknown | Append `"line N: unknown type 'X'"` to `parse_errors`, preserve line as-is. |

**Image handling** (`_process_content_blocks`): an image block
`{type: "image", source: {type: "base64", data: "..."}}` is replaced in both
`content_json` and `raw_jsonl` with `{type: "image", omitted: true, media_type: ...}`.
The base64 payload (often multi-megabyte) is **never written to the database**.
Tested by `test_image_base64_not_in_raw_jsonl` against real fixtures with images.

**Thinking handling**: thinking blocks become `{type: "thinking", omitted: true}`
in `content_json`. The reasoning text is never appended to `asst_messages_text`,
so it never enters the FTS index.

**Streaming**: `parse_session()` reads `filepath.read_bytes()` once for SHA256,
then processes line-by-line via `splitlines()`. The largest fixture is 4.8MB
(470 lines) and parses in <100ms.

**Never raises** on malformed input. Each `json.JSONDecodeError` is caught and
recorded in `parse_errors`; processing continues.

---

### `src/vimgym/pipeline/redact.py`

**`RedactionEngine(rules_path)`** — loads `rules_path` (JSON), compiles every
regex once at construction. If `rules_path` doesn't exist, falls back to
`defaults/redaction-rules.json` shipped in the repo.

`redact_text(text)` applies every compiled pattern in order, returning the
substituted string. Used on `user_messages_text` and `asst_messages_text`
before FTS indexing.

`redact_session_raw(raw_jsonl)` splits the JSONL into lines and applies
`redact_text()` to each line. Lines that don't parse as JSON are still
redacted (a partial-write line may still contain a secret).

The 18 patterns are documented in [Redaction Rules Reference](#redaction-rules-reference).

---

### `src/vimgym/pipeline/metadata.py`

**`SessionMetadata`** — what the writer needs that isn't on `ParsedSession`:

| Field | Source |
|---|---|
| `session_uuid` | passthrough |
| `project_name` | `decode_project_name(project_dir, cwd)` |
| `duration_secs` | `(ended_at - started_at).seconds`, or `None` if either is missing or unparseable |
| `message_count`, `user_message_count`, `asst_message_count` | counted from `session.messages` |
| `tool_use_count` | sum of `len(m.tool_names)` |
| `files_modified_display` | `files_modified` with `cwd` prefix stripped, for UI display |

**`decode_project_name(project_dir, cwd)`** — uses `cwd` as ground truth when
present (`Path(cwd).name`), since the path-encoding scheme `/Users/x/my-project →
-Users-x-my-project` is **not reversible** when the project name itself
contains dashes. Falls back to taking the last `-`-segment of the encoded dir
name when `cwd` is absent.

---

### `src/vimgym/pipeline/summary.py`

`heuristic_summary(session)` — no API calls. Format:

```
{ai_title or "Untitled session"}. {first user prompt, ≤120 chars}. Files: {≤3 basenames}. Tools: {≤5 names}.
```

Truncated to **280 characters**. Used to populate the FTS5 `summary` column and
the `summary` field on the `sessions` table. A future v2 may swap in an
LLM-backed summary; the heuristic is the v1 baseline.

---

### `src/vimgym/pipeline/orchestrator.py`

**`ProcessResult`** dataclass:

| Field | Notes |
|---|---|
| `session_uuid` | empty string if extraction failed |
| `project_name` | from metadata |
| `skipped` | `True` if dedup'd |
| `skip_reason` | human-readable reason |
| `error` | `str` or `None` — non-None means the file was not indexed |
| `duration_secs`, `message_count` | for the live-update WebSocket payload |

**`process_session(filepath, config, source_id="claude_code")`** is the only
top-level entry point used by both the watcher and pytest. It is wrapped in a
top-level try/except — **never raises**. The watcher and the test suite both
rely on this contract.

Pipeline steps inside `_process()`:

1. Open the thread-local DB connection (raises early if vault is broken).
2. Cache and reuse the `RedactionEngine` for this `rules_path`.
3. `parse_session(filepath)` → `ParsedSession`.
4. Set `session.source_id = source_id`.
5. **Dedup**: if `session_exists_by_hash(file_hash)` → return `ProcessResult(skipped=True, skip_reason="file_hash already indexed")`.
6. **Dedup**: if `session_exists_by_uuid(session_uuid)` → same. (Catches the case where the file is byte-different but represents a known session UUID — which is what happens for in-progress sessions like this very build conversation.)
7. Apply redaction to `raw_jsonl`, `user_messages_text`, `asst_messages_text`.
8. `extract_metadata(session)` → `SessionMetadata`.
9. `heuristic_summary(session)` → `str`.
10. `upsert_session(conn, session, metadata, summary)` — single transaction.
11. Log `backed_up source=X session=Y project=Z messages=N`.
12. Return `ProcessResult` (the watcher's `_run` then publishes the live-update event).

---

### `src/vimgym/storage/writer.py`

**`upsert_session(conn, session, metadata, summary)`** runs everything inside
a single `BEGIN`/`COMMIT` block, with `ROLLBACK` on any exception:

| Table | Operation |
|---|---|
| `sessions` | `INSERT OR REPLACE` (composite key = `SHA256(session_uuid + started_at)`) |
| `sessions_raw` | `INSERT OR REPLACE` (the redacted JSONL) |
| `sessions_fts` | `DELETE WHERE session_uuid = ?` then `INSERT` — FTS5 has no `UPDATE` |
| `messages` | `DELETE WHERE session_uuid = ?` then bulk `INSERT` via `executemany` |
| `projects` | Recomputed from `sessions` aggregates (correct under `INSERT OR REPLACE`) |

Why recompute project aggregates instead of incrementing? Because
`INSERT OR REPLACE` looks like a fresh insert to a counter, which would
double-count on session updates. A `SUM` query is microseconds at this scale.

**`session_exists_by_hash(conn, file_hash)`** and
**`session_exists_by_uuid(conn, session_uuid)`** are the dedup checks called
by `orchestrator._process()`.

---

### `src/vimgym/storage/queries.py`

`search_sessions(conn, query, project=None, branch=None, since=None, until=None, tool=None, limit=20)` returns
`list[SearchResult]` (a dataclass with `session_uuid, project_name, ai_title,
started_at, duration_secs, git_branch, snippet, rank`).

The SQL joins `sessions_fts` with `sessions` and uses FTS5's
`snippet(sessions_fts, 5, '<mark>', '</mark>', '...', 15)` for the result
preview, ordered by `rank` (BM25 by default).

**`_escape_fts_query(query)`** is the most important function in this file.
FTS5 treats hyphens, slashes, and colons as syntax: a query like
`fee-to-enrollment` would error out without escaping. The fix wraps each
whitespace-separated token in double quotes so FTS5 reads each as a literal
phrase. Multi-word queries become AND across phrases. This is what makes
`vg search "fee-to-enrollment"` work in production.

`list_sessions(conn, project, branch, since, until, limit, offset)` — structured
filtering, no FTS. Drives the inbox.

`count_sessions(...)` — COUNT(*) under the same filters as `list_sessions`.

`get_session(conn, uuid_prefix)` — `WHERE session_uuid LIKE 'prefix%' LIMIT 10`.
Raises `AmbiguousIDError` if more than one matches; returns `None` if zero.
The CLI/UI handle 409 responses for ambiguity.

`get_session_messages(conn, session_uuid)` — joined to detail view.

`get_stats(conn) -> StatsRow` — `total_sessions`, `total_duration_secs`,
`total_input_tokens`, `total_output_tokens`, `db_size_bytes`,
`sessions_this_week`, `top_projects`, `top_tools`. `top_tools` is computed
in Python by parsing the JSON `tools_used` column from each session — simpler
than per-tool tables and fast at v1 scale.

`get_timeline(conn, since_days=365)` — `[{date: 'YYYY-MM-DD', count: N}, ...]`
via `GROUP BY substr(started_at, 1, 10)`. Drives the sidebar heatmap.

`list_projects(conn)` — passthrough on the `projects` table, ordered by
`session_count DESC`.

**`_parse_since(since)`** accepts either ISO-8601 or `Nd` shorthand (`7d`,
`30d`). Used by `since` filters across `search_sessions` and `list_sessions`.

---

### `src/vimgym/storage/export.py`

`render_session_markdown(session, messages)` — produces a paste-friendly
Markdown document with metadata header and per-message blocks. Output format:

```markdown
# {ai_title}

## Metadata
- **Project:** `edforge`
- **Branch:** `dev`
- **CWD:** `/Users/.../edforge`
- **Started:** 2026-04-05T16:28:49Z
- **Duration:** 5h 20m
- **Tools used:** `Bash`, `Edit`, ...
- **Files modified:** ...

## Conversation

### 👤 User  _16:28:49_
{text content}

### 🤖 Claude  _16:28:52_
{text content}

**🔧 tool_use_name**
```json
{tool input}
```
---
```

Internal helper `_tool_result_text(content)` handles the dual content shape
(string vs `list[{type:"text", text:...}]`) discovered in Sprint 2.

The composite-key `sqlite3.Row` accessor uses `_row(row, key, default)` because
`sqlite3.Row` supports `__getitem__` but not `.get()`.

---

### `src/vimgym/events.py`

A bounded `queue.Queue(maxsize=1024)` named `event_queue`, plus a `publish(event)`
helper that calls `put_nowait` and silently drops if the queue is full.

This is the cross-thread bus between the watcher (writer thread) and the
server's broadcaster (asyncio task). It exists in its own module to avoid
circular imports between `watcher.py` and `server.py`.

A bounded queue is a deliberate safety net: a runaway watcher cannot grow
process memory unbounded, even in pathological cases.

---

### `src/vimgym/watcher.py`

**`_is_session_file(path: str) -> bool`** — the filter rules:

1. Must end in `.jsonl`.
2. Must NOT have a basename starting with `.` (hidden files).
3. Must NOT contain `/subagents/` or `/tool-results/` in its path.

The companion-dir exclusion is critical: when Claude Code spawns a subagent it
writes to `~/.claude/projects/{UUID}/subagents/agent-{id}.jsonl`. Those are
**not** root session files and would crash the parser if processed as such.

**`SessionWatcher(config, source_id)`** — one watcher instance per *configured
source*. Each `Observer.schedule()` call binds a watcher to its source path.
The watcher carries its `source_id` so the orchestrator records correct
provenance on each row.

Hooks: `on_created`, `on_modified` both call `_maybe_schedule()`. `on_moved`
treats the destination as a new file. All three normalize watchdog's
`str | bytes` `src_path` into `str` before further checks.

**Debounce**: `_schedule(path)` keeps a `dict[str, threading.Timer]`. Each new
event for a path cancels and replaces its existing timer. Timer callback is
`_process_when_stable`. This collapses bursts of events (Claude Code writes
multiple times per turn) into a single processing run.

**Stability check** (`_wait_for_stability`): polls `os.path.getsize()` until
two consecutive reads are equal. Capped at 15s — beyond that, log a warning
and proceed. This protects the parser from processing a half-written file in
the middle of a network latency spike.

**`_run(path)`** calls `process_session(...)`, logs the result, and publishes a
`session_added` event when the orchestrator returns a non-skipped success.

**`backfill(config)`** is called once at daemon startup. Walks every enabled
source's path with `rglob("*.jsonl")` and runs each through `process_session`.
Sources whose `type != "claude_code"` are skipped (no parser yet). Existing
sessions get caught by hash dedup and skipped silently. Returns the count of
newly indexed files.

**`start_watching(config)` returns `(BaseObserver, list[SessionWatcher])`**. One
`Observer`, multiple handlers (one per source). The daemon's `run_foreground`
holds the observer for shutdown.

---

### `src/vimgym/server.py`

**`create_app(config)`** is the FastAPI app factory. It uses an
`@asynccontextmanager` `lifespan` for startup/shutdown — migrated from the
deprecated `@app.on_event` API in this release.

The lifespan:

- **Startup**: spawns the broadcaster `pump()` as an asyncio task. The pump
  loops on `event_queue.get(timeout=0.25)` (in an executor thread because
  `queue.get` is blocking) and forwards each event to the WebSocket fanout.
  The 0.25s timeout is non-negotiable: a no-timeout `queue.get` blocks the
  executor thread forever, and asyncio cannot cancel a thread blocked in C —
  that was a real shutdown deadlock found in Sprint 3.

- **Shutdown**: sets `_broadcaster_stop = True`, then `await asyncio.wait_for(task, timeout=1.0)` to drain. Falls back to `task.cancel()` if it doesn't drain in time.

**`WSManager`** — an `asyncio.Lock`-protected `set[WebSocket]`. `connect`,
`disconnect`, `broadcast(event)`. Dead connections are pruned during broadcast.

**CORS** allows `http://localhost:{port}` and `http://127.0.0.1:{port}`.
External origins are rejected. The server only ever binds to `127.0.0.1`.

**Routes** (see [API Reference](#api-reference) below).

**Static UI mount** at `/`: `StaticFiles(directory="src/vimgym/ui", html=True)`.
The mount is added last so it doesn't shadow `/api/...` routes. If the UI
directory doesn't exist (during partial development), a placeholder JSON
response is registered at `/` instead.

---

### `src/vimgym/daemon.py`

**`is_running(config)`** reads `config.pid_path`, checks if the PID is alive
via `os.kill(pid, 0)`, and **deletes the PID file if the PID is stale**. This
is what makes `vg start` work cleanly after an OS reboot.

**`start_daemon(config)`** spawns the foreground runner as a detached
subprocess: `subprocess.Popen([sys.executable, "-m", "vimgym.daemon",
"--run-foreground"], start_new_session=True)`. Writes the child's PID to
`pid_path`. Polls `socket.create_connection(host, port)` for up to 2 seconds
to confirm the server is actually accepting connections before returning. If
the child exits immediately, raises `RuntimeError` with a pointer to the log
file.

**Critical**: the env passed to the child contains `VIMGYM_PATH` and
`VIMGYM_PORT` but **not** `VIMGYM_WATCH_PATH`. The child reads its on-disk
config from `VIMGYM_PATH`, which already contains the full `sources[]` list.
Forwarding `VIMGYM_WATCH_PATH` would collapse multi-source configs to a
single `env_override` source — a bug found and fixed in Sprint 5.

**`stop_daemon(config)`** reads the PID, sends `SIGTERM`, waits up to 5s for
the process to exit, falls back to `SIGKILL`. Removes the PID file. Returns
`True` if a daemon was actually stopped, `False` if none was running.

**`run_foreground(config)`** is what the spawned subprocess executes. Sequence:

1. Create `vault_dir` and `logs/` directories.
2. `init_db()` — creates / opens the vault, sets WAL mode, validates FTS5.
3. `save_config()` — persists current config (including any auto-detected sources).
4. Configure logging to both file and stderr.
5. `backfill(config)` — pick up files written while the daemon was off.
6. `start_watching(config)` — schedule observer.
7. Build the FastAPI app via `create_app(config)`.
8. Construct `uvicorn.Server` with `access_log=False`.
9. Install `SIGTERM`/`SIGINT` handlers that set `server.should_exit = True`.
10. `server.run()` — blocks until shutdown signal.
11. On exit: stop observer, publish a sentinel `{type: "shutdown"}` event so the broadcaster pump unblocks immediately.

**`main()`** is the module entry point used by `python -m vimgym.daemon`. Only
the `--run-foreground` flag is recognized; everything else prints
`vimgym daemon: use \`vg start\` instead` and exits 1.

---

### `src/vimgym/cli.py`

8 commands. Argparse subparsers, dispatched by name in `main()`:

| Command | Behavior |
|---|---|
| `vg init` | `init_vault()` — creates vault dir, runs `detect_sources()`, persists config, prints detection table. Exit 0. |
| `vg start` | Auto-runs `init_vault()` on first invocation. Calls `start_daemon()`. Prints PID + URL + each watching source. Opens browser if `auto_open_browser`. Exits 1 if `start_daemon` raises. |
| `vg stop` | `stop_daemon()`. Prints `vimgym stopped` or `vimgym was not running`. Always exits 0. |
| `vg status` | Rich table with status (running/stopped), pid, url, vault, watching, sessions, db size. Exits 0. |
| `vg open` | Opens `http://{host}:{port}` if running, else exits 1 with error. |
| `vg search QUERY [--project P] [--branch B] [--since S] [--limit N] [--json]` | If daemon running → HTTP `/api/search`. If not → direct DB query. `--json` prints JSON to stdout; otherwise rich table. Exits 2 if no query supplied. |
| `vg config` | Prints active config summary table. |
| `vg config sources [SOURCE_ID] [--enable\|--disable]` | No args: rich table of sources with status and parser availability. With ID + flag: toggle and persist. Exit 1 if ID not found, 2 if ID supplied without flag. |

**`_search_via_api()`** uses `httpx.get` with a 5s timeout and falls back to
`_search_via_db()` if the request fails — so search keeps working even if the
daemon is in a degraded state.

---

### Web UI (`src/vimgym/ui/`)

**`index.html`** — minimal app shell. Order matters:

1. Google Fonts CDN link (the *only* external resource).
2. `style.css` link (`/style.css`).
3. `<canvas id="matrix-canvas">` — the matrix rain layer.
4. Command palette overlay (hidden by default, toggled by `.open` class).
5. Toast slot.
6. `.app` grid container with `.topbar`, `.sidebar`, `.inbox`, `.detail`, `.statusbar`.
7. `<script src="/vendor/highlight.min.js">` (bundled, not CDN).
8. `<script src="/app.js">`.

**`style.css`** — organized in marked sections (RESET / VARIABLES / SCANLINES /
MATRIX CANVAS / LAYOUT / TOPBAR / SIDEBAR / INBOX / DETAIL / MESSAGES /
TOOL BLOCKS / CODE BLOCKS / STATUSBAR / COMMAND PALETTE / SETTINGS PANEL /
WELCOME SCREEN / TOAST / ANIMATIONS / SCROLLBARS / HIGHLIGHT.JS OVERRIDE).

Design tokens (from the prototype, never invented):

```
--void-0  #060608   deepest void (body bg)
--void-1  #0C0C10   topbar / sidebar
--void-2  #111118   inbox / detail header
--void-3  #18181F   cards
--void-4  #22222C   tool blocks
--void-5  #2E2E3A   borders
--matrix  #00FF41   active / success / live
--pink    #FF078E   selection / search / CTA / statusbar
--cyan    #0DF1ED   borders / project names / user msgs
--amber   #FFB800   warnings / durations
--purple  #BD93F9   assistant msgs / AI
```

**`app.js`** — vanilla JS, no framework, no build step. Sections:

| Section | Functions |
|---|---|
| STATE | `State` object — single source of truth, no reactive system |
| API HELPERS | `apiFetch(url)` — try/catch, returns null on error |
| UTILITIES | `relativeTime`, `formatDuration`, `formatBytes`, `formatTokens`, `escapeHtml`, `toolChipClass` |
| MATRIX RAIN | IIFE: 12px Japanese katakana + hex chars, ~20fps |
| SIDEBAR | `loadSidebar`, `renderProjects`, `renderBranches`, `renderHeatmap`, `renderStats`, `renderToolsList` |
| INBOX | `loadInbox`, `renderInbox`, `sessionCardHTML`, `attachCardClickHandlers`, `setupInboxScroll` |
| DETAIL | `loadDetail`, `renderDetail` |
| CONTENT RENDERING | `messageHTML`, `contentBlockHTML`, `extractToolResultText`, `renderMarkdownLite` |
| SETTINGS | `openSettings`, `renderSettingsSources`, `renderSettingsVault`, `renderSettingsServer`, `renderSettingsRedaction` |
| COMMAND PALETTE | `openCommandPalette`, `closeCommandPalette`, `runCommandSearch`, `renderCommandResults`, `highlightMatches`, `openSelectedResult`, `moveCommandSelection` |
| STATUSBAR | `updateStatusbar`, `loadHealth` |
| EXPORT | `exportSession` — Blob + `<a download>` |
| WEBSOCKET | `connectWebSocket`, `handleNewSession` (auto-reconnect on close) |
| TOAST | `showToast` |
| KEYBOARD SHORTCUTS | `setupKeyboard` — ⌘K/Esc/↑↓/Enter |
| INIT | DOMContentLoaded handler |

**API call map** (action → endpoint):

| User action | HTTP call |
|---|---|
| App boot | `GET /health`, `GET /api/stats`, `GET /api/projects`, `GET /api/stats/timeline?since=365d`, `GET /api/sessions?limit=50` |
| Click project | `GET /api/sessions?project=X&limit=50` |
| Click branch | `GET /api/sessions?branch=X&limit=50` |
| Scroll to bottom of inbox | `GET /api/sessions?offset=N&...` |
| Click session card | `GET /api/sessions/{uuid}` |
| Type in palette (200ms debounce) | `GET /api/search?q=X&limit=10` |
| Click Export | `GET /api/sessions/{uuid}/export?format=markdown` |
| Toggle source in settings | `PATCH /api/config/sources/{id}` `{enabled: bool}` |
| Persistent | `WS /ws` |

**WebSocket flow**: server pushes `{type: "session_added", session: {...}}` →
client calls `handleNewSession()` → re-fetches the full session row → prepends
a `<div class="session-card new">` to the inbox → green-flash CSS animation →
toast bottom-right → re-fetches `/api/stats` for the sidebar counters.

**Why highlight.js is bundled**: zero runtime CDN dependency, zero outbound
network calls from the daemon. The `test_no_external_urls_in_app_js` test
enforces this — any future code that adds a CDN reference fails the build.

---

## Database Schema Reference

```sql
CREATE TABLE sessions (
    -- Identity
    id              TEXT PRIMARY KEY,        -- SHA256(session_uuid + started_at)
    session_uuid    TEXT NOT NULL UNIQUE,    -- from sessionId in JSONL
    slug            TEXT,                    -- e.g. "wise-purring-flute"

    -- Source location
    source_path     TEXT NOT NULL,           -- absolute path to .jsonl file
    project_dir     TEXT NOT NULL,           -- raw encoded dir name
    project_name    TEXT NOT NULL,           -- decoded via cwd

    -- Context
    cwd             TEXT,
    git_branch      TEXT,
    entrypoint      TEXT,                    -- claude-vscode | claude-cli
    claude_version  TEXT,
    permission_mode TEXT,                    -- default | plan

    -- Time
    started_at      TEXT NOT NULL,           -- ISO8601
    ended_at        TEXT,
    duration_secs   INTEGER,

    -- Content stats
    message_count       INTEGER DEFAULT 0,
    user_message_count  INTEGER DEFAULT 0,
    asst_message_count  INTEGER DEFAULT 0,
    tool_use_count      INTEGER DEFAULT 0,
    has_subagents       INTEGER DEFAULT 0,   -- 0 or 1

    -- Token accounting
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_write_tokens  INTEGER DEFAULT 0,

    -- Generated metadata
    ai_title       TEXT,
    summary        TEXT,                     -- heuristic, ≤280 chars
    tools_used     TEXT,                     -- JSON array
    files_modified TEXT,                     -- JSON array

    -- Vimgym metadata
    backed_up_at    TEXT NOT NULL,
    file_hash       TEXT NOT NULL,           -- SHA256 of original bytes (dedup)
    file_size_bytes INTEGER,
    schema_version  INTEGER DEFAULT 1,
    source_id       TEXT DEFAULT 'claude_code'   -- v2: which source produced this row
);

-- Full-text search (FTS5 with porter stemmer + unicode normalization)
CREATE VIRTUAL TABLE sessions_fts USING fts5(
    session_uuid UNINDEXED,
    project_name,
    git_branch,
    ai_title,
    summary,
    user_messages,        -- concatenated user text
    asst_messages,        -- concatenated assistant text (no thinking)
    tools_used,
    files_modified,
    tokenize = 'porter unicode61'
);

-- Raw JSONL (redacted) for export and re-rendering
CREATE TABLE sessions_raw (
    session_uuid TEXT PRIMARY KEY REFERENCES sessions(session_uuid) ON DELETE CASCADE,
    raw_jsonl    TEXT NOT NULL
);

-- Per-message detail (renders the conversation in the detail pane)
CREATE TABLE messages (
    id            TEXT PRIMARY KEY,           -- session_uuid + ":" + message_uuid
    session_uuid  TEXT NOT NULL REFERENCES sessions(session_uuid) ON DELETE CASCADE,
    parent_uuid   TEXT,
    type          TEXT NOT NULL,              -- user | assistant
    role          TEXT NOT NULL,              -- user | assistant
    timestamp     TEXT,
    has_tool_use  INTEGER DEFAULT 0,
    has_thinking  INTEGER DEFAULT 0,
    has_image     INTEGER DEFAULT 0,
    tool_names    TEXT,                       -- JSON array
    content_json  TEXT NOT NULL               -- block array, image base64 stripped
);

-- Project aggregates (recomputed on every upsert)
CREATE TABLE projects (
    project_name        TEXT PRIMARY KEY,
    project_dir         TEXT NOT NULL,
    cwd                 TEXT,
    session_count       INTEGER DEFAULT 0,
    last_active         TEXT,
    total_duration_secs INTEGER DEFAULT 0,
    total_input_tokens  INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0
);

-- Single-row key/value store (schema_version, future settings)
CREATE TABLE config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Indexes
CREATE INDEX idx_sessions_project   ON sessions(project_name);
CREATE INDEX idx_sessions_started   ON sessions(started_at DESC);
CREATE INDEX idx_sessions_branch    ON sessions(git_branch);
CREATE INDEX idx_sessions_hash      ON sessions(file_hash);
CREATE INDEX idx_sessions_uuid      ON sessions(session_uuid);
CREATE INDEX idx_sessions_source    ON sessions(source_id);
CREATE INDEX idx_messages_session   ON messages(session_uuid);
CREATE INDEX idx_messages_timestamp ON messages(timestamp);
```

**Schema version:** v0.1 ships with `schema_version = 1`. The single
`SCHEMA_DDL` script in `db.py` is the source of truth. There is no migration
runner; the seeded `config.schema_version` row exists for future use.

---

## API Reference

All endpoints are served from `http://127.0.0.1:7337` by default.

| Method | Path | Query / body | Response |
|---|---|---|---|
| GET | `/health` | — | `{status, version, sessions, uptime_secs}` |
| GET | `/api/sessions` | `project, branch, since, until, limit (≤500), offset` | `{sessions: [...], total, has_more}` |
| GET | `/api/sessions/{prefix}` | — | full session row + `messages: [...]` (each with parsed `content` array). 404 if not found. 409 if prefix is ambiguous (`{detail: {error: "ambiguous_id", matches: [uuids]}}`). |
| GET | `/api/sessions/{prefix}/raw` | — | `text/plain` raw redacted JSONL |
| GET | `/api/sessions/{prefix}/export` | `format=markdown` | `text/markdown; charset=utf-8` with `Content-Disposition: attachment; filename="{slug}-{date}.md"` |
| GET | `/api/search` | `q` (required), `project, branch, since, until, tool, limit (≤100)` | `{query, total, results: [{session_uuid, project_name, ai_title, started_at, duration_secs, git_branch, snippet, rank}]}` |
| GET | `/api/projects` | — | `[{project_name, project_dir, cwd, session_count, last_active, total_duration_secs, total_input_tokens, total_output_tokens}]` |
| GET | `/api/stats` | — | `{total_sessions, total_duration_secs, total_input_tokens, total_output_tokens, db_size_bytes, sessions_this_week, top_projects, top_tools}` |
| GET | `/api/stats/timeline` | `since=Nd` (default `365d`) | `{days: [{date, count}]}` |
| GET | `/api/config` | — | `{vault_dir, server_host, server_port, log_level, auto_open_browser, debounce_secs, schema_version}` |
| GET | `/api/config/sources` | — | `{sources: [{id, name, type, path, enabled, exists, auto_detected, parser_available}]}` |
| PATCH | `/api/config/sources/{id}` | `{enabled: bool}` | `{id, enabled, note: "takes effect on next vg start"}`. 404 if id not found. |
| WS | `/ws` | — | server-push: `{type: "session_added", session: {session_uuid, project_name, duration_secs, message_count, source_id}}` |
| GET | `/` | — | `index.html` (or fallback JSON if UI not built) |
| GET | `/style.css`, `/app.js`, `/vendor/highlight.min.js` | — | Static asset |

**Notes:**

- All `since` parameters accept either ISO-8601 or `Nd` shorthand (e.g. `7d`, `30d`).
- The `q` parameter to `/api/search` is escaped via `_escape_fts_query()` — hyphens, slashes, and colons in queries are safe.
- Snippet HTML in `/api/search` results contains `<mark>` tags from FTS5's `snippet()` function. The client renders this as-is (it is server-generated, not user input).
- The `/ws` endpoint is push-only; sending text from the client is allowed but ignored.

---

## Configuration Reference

### `~/.vimgym/config.json` (schema v2)

```json
{
  "schema_version": 1,
  "vault_dir": "/Users/you/.vimgym",
  "server_host": "127.0.0.1",
  "server_port": 7337,
  "auto_open_browser": true,
  "log_level": "INFO",
  "debounce_secs": 5.0,
  "stability_polls": 2,
  "stability_poll_interval": 1.0,
  "sources": [
    {
      "id": "claude_code",
      "name": "Claude Code",
      "type": "claude_code",
      "path": "/Users/you/.claude/projects",
      "enabled": true,
      "auto_detected": true
    }
  ]
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `schema_version` | int | `1` | Initial v0.1 schema |
| `vault_dir` | str | `~/.vimgym` | Where vault.db, logs, and PID file live |
| `server_host` | str | `127.0.0.1` | **Never** set to `0.0.0.0` — security |
| `server_port` | int | `7337` | |
| `auto_open_browser` | bool | `true` | Launch browser on `vg start` |
| `log_level` | str | `"INFO"` | Standard Python logging levels |
| `debounce_secs` | float | `5.0` | How long after the last write before processing |
| `stability_polls` | int | `2` | Consecutive size-stable reads required |
| `stability_poll_interval` | float | `1.0` | Seconds between stability polls |
| `sources` | list | `[]` | See SourceConfig schema below |

### Source schema

| Field | Type | Notes |
|---|---|---|
| `id` | str | Unique slug. Used as `source_id` in DB rows. |
| `name` | str | Display name. |
| `type` | str | `"claude_code"` (only parser available in v1). |
| `path` | str | Watch path; `~` is expanded at runtime. |
| `enabled` | bool | Watcher only schedules enabled sources. |
| `auto_detected` | bool | True if added by `vg init`'s `detect_sources()`. |

### Environment variables

| Variable | Effect |
|---|---|
| `VIMGYM_PATH` | Override `vault_dir`. Used for testing isolated vaults. |
| `VIMGYM_PORT` | Override `server_port`. |
| `VIMGYM_WATCH_PATH` | **Replace all** `sources[]` with one source `id="env_override"`. Dev-only escape hatch. The daemon does not forward this to its child process — the child reads its own on-disk config. |

---

## Redaction Rules Reference

`defaults/redaction-rules.json` is shipped with vimgym; users can override by
placing their own at `~/.vimgym/redaction-rules.json`. Patterns are compiled
once per `RedactionEngine` instance and applied in declaration order.

| # | Name | Catches | Replacement |
|---|---|---|---|
| 1 | `anthropic_key` | `sk-ant-…` (60+ chars) | `[REDACTED_ANTHROPIC_KEY]` |
| 2 | `openai_key` | `sk-…` (40+ chars) | `[REDACTED_OPENAI_KEY]` |
| 3 | `aws_access` | `AKIA` + 16 alphanumeric | `[REDACTED_AWS_KEY]` |
| 4 | `aws_secret` | `aws.secret… = abc…` (40 chars) | `[REDACTED_AWS_SECRET]` |
| 5 | `aws_session_token` | `AQoXb…` (100+ chars) | `[REDACTED_AWS_SESSION]` |
| 6 | `arn` | `arn:aws:service:region:account:resource` | `arn:aws:[REDACTED]` |
| 7 | `bearer_token` | `Bearer …` (20+ chars) | `Bearer [REDACTED]` |
| 8 | `github_token` | `ghp_…` (36+ chars) | `[REDACTED_GITHUB_TOKEN]` |
| 9 | `jwt` | `eyJ…\.…\.…` three-segment JWT | `[REDACTED_JWT]` |
| 10 | `kubeconfig_cert` | `certificate-authority-data: …` (50+ chars) | `certificate-authority-data: [REDACTED]` |
| 11 | `k8s_token` | `token: …` (40+ chars) | `token: [REDACTED]` |
| 12 | `private_key_inline` | `-----BEGIN (EC\|RSA\|OPENSSH\|DSA) PRIVATE KEY-----…-----END … PRIVATE KEY-----` | `[REDACTED_PRIVATE_KEY]` |
| 13 | `pem_block` | Generic `-----BEGIN X-----…-----END X-----` | `[REDACTED_PEM_BLOCK]` |
| 14 | `docker_auth` | `"auth": "…"` (20+ chars base64) | `"auth": "[REDACTED]"` |
| 15 | `postgres_password` | `postgresql://user:pass@` URI form | `postgresql://[user]:[REDACTED]@` |
| 16 | `database_url` | `(mongodb\|postgres\|mysql\|redis)://…` | `[REDACTED_DB_URL]` |
| 17 | `npm_token` | `//registry.npmjs.org/:_authToken=…` | `//registry.npmjs.org/:_authToken=[REDACTED]` |
| 18 | `env_secret` | `(?i)(password\|secret\|api_key\|private_key)=…` (8+ chars) | `\1=[REDACTED]` |

Patterns are applied to:
- `raw_jsonl` (the full JSONL stored in `sessions_raw`)
- `user_messages_text` (concatenated for FTS)
- `asst_messages_text` (concatenated for FTS)

The original files in `~/.claude/projects/` are **never modified**. Redaction
runs on the in-memory copy before insertion.

---

## Source Adapter Interface

To add a new source adapter (Cursor, Copilot, Gemini, etc.):

### 1. Add an entry to `KNOWN_SOURCES` in `config.py`

```python
KNOWN_SOURCES.append({
    "id":         "cursor",
    "name":       "Cursor",
    "type":       "cursor",                    # ← new type
    "check_path": "~/.cursor",
    "watch_path": "~/.cursor/sessions",        # ← wherever sessions actually live
    "note":       "Cursor IDE",
})
```

Set `type` to a new identifier (`"cursor"`). Setting it to `"claude_code"` is
wrong because it would route Cursor's files through the Claude Code parser.

### 2. Create a parser

Add `src/vimgym/sources/cursor.py` with a function:

```python
from vimgym.pipeline.parser import ParsedSession

def parse_cursor_session(filepath: Path) -> ParsedSession:
    """Parse a Cursor session file into the universal ParsedSession shape."""
    ...
    return ParsedSession(
        session_uuid=...,
        ...,
        source_id="cursor",
    )
```

The output type is `ParsedSession` regardless of source — that's the universal
shape the storage layer accepts. You only need to figure out how Cursor stores
sessions and how to map them onto `ParsedMessage` blocks (`text`, `tool_use`,
`tool_result`, `image`, `thinking`).

### 3. Dispatch in the orchestrator

Update `pipeline/orchestrator.py` to dispatch on `source_type`:

```python
def _process(filepath, config, source_id):
    source_type = _lookup_source_type(config, source_id)   # new helper
    if source_type == "claude_code":
        session = parse_session(filepath)
    elif source_type == "cursor":
        from vimgym.sources.cursor import parse_cursor_session
        session = parse_cursor_session(filepath)
    else:
        return ProcessResult(error=f"no parser for source type {source_type}")
    session.source_id = source_id
    # ... rest of the pipeline is identical ...
```

### 4. Update the watcher dispatch

`watcher.py:start_watching()` currently filters out `type != "claude_code"`.
Update to allow your new type:

```python
PARSER_TYPES = {"claude_code", "cursor"}
...
if source.type not in PARSER_TYPES:
    logger.info("watcher: source %s detected but parser unavailable", source.id)
    continue
```

### 5. Register your file filter (if needed)

If Cursor sessions live in non-`.jsonl` files or in companion subdirectories
that need to be excluded, extend `_is_session_file()` or create a parallel
function and parameterize the dispatch.

### 6. Tests

Add `tests/test_sources_cursor.py` with:
- A real Cursor session fixture in `data/cursor/`
- A test that calls `parse_cursor_session(fixture)` and asserts the
  `ParsedSession` shape
- An end-to-end test through `process_session()` with `source_id="cursor"`
- A regression test that the resulting DB row has `source_id = "cursor"`

The existing `tests/test_orchestrator.py::test_full_pipeline_inserts_to_db` is
the model.

---

## Testing Guide

### Running tests

```bash
source .venv/bin/activate
pytest tests/ -v --tb=short
# 117 passed
```

Faster targeted runs:

```bash
pytest tests/test_parser.py -v          # parser only
pytest tests/test_server.py -v          # API endpoints (TestClient)
pytest tests/test_daemon.py -v          # spawns real subprocesses; ~12s
pytest tests/test_watcher.py -v         # exercises watchdog with real files
```

### Test fixture data

`data/-Users-shoaibrain-edforge/` contains 6 real Claude Code sessions from
the EdForge project, redacted before commit. They are the canonical test
fixtures and are referenced from `tests/conftest.py`:

| File | Size | Lines | Duration | Tools | Used to test |
|---|---|---|---|---|---|
| `1fb8b1b8-...jsonl` | 293 B | 1 | — | — | Minimal/edge: only `last-prompt` record |
| `eaa3009a-...jsonl` | 1.5 MB | 148 | 25 m | Bash, Read, Write | Default "simple" fixture |
| `64b0bec2-...jsonl` | 0.3 MB | 145 | 11 m | Edit, Grep | Edit-heavy session |
| `eaa3009a` (CloudFormation title) | — | — | — | — | `test_summary_contains_title` |
| `3438c55b-...jsonl` | 4.8 MB | 470 | 5h 20m | Full suite + Agent | Largest fixture; subagent detection; CORS search target |
| `64778c29-...jsonl` | 4.5 MB | 707 | 3h 47m | Full suite + Agent | Long session with images |
| `68568954-...jsonl` | 2.0 MB | 488 | 22h 2m | Full suite | Multi-day session — exercises stability poll |

Companion directories (`{UUID}/subagents/`, `{UUID}/tool-results/`) sit
alongside the JSONL files. The watcher's `_is_session_file()` filter
exists specifically to keep these out of the parse path.

### `conftest.py` fixtures

| Fixture | Scope | What it provides |
|---|---|---|
| `data_dir` | session | `Path` to `data/-Users-shoaibrain-edforge` |
| `simple_session_path` | session | `eaa3009a-...jsonl` |
| `agents_session_path` | session | `3438c55b-...jsonl` |
| `minimal_session_path` | session | `1fb8b1b8-...jsonl` |
| `all_session_paths` | session | `list[Path]` of all 6 fixtures |
| `parsed_simple` | session | `parse_session(simple_session_path)` cached |
| `parsed_agents` | session | `parse_session(agents_session_path)` cached |
| `tmp_db` | function | A fresh `init_db()`'d vault.db in `tmp_path` |

### Running against real Claude Code sessions

```bash
VIMGYM_WATCH_PATH=$HOME/.claude/projects vg start
# Indexes everything, then keeps the daemon running
```

This is how the tool gets dogfooded during development. Every build session
gets indexed by vimgym itself.

### Adding a new test fixture

1. Copy a real session from `~/.claude/projects/...` into
   `data/-Users-shoaibrain-edforge/`.
2. Run a redaction pass over it before committing — `vimgym.pipeline.redact`
   has the engine; a one-liner script in `scripts/redact-fixture.py` would be
   the right place if you commit fixtures regularly.
3. Add a fixture in `conftest.py` if you need it across multiple tests.
4. Write your test against `parse_session()` or `process_session()`.

### Test count by sprint

| Sprint | New tests | Cumulative |
|---|---|---|
| 1 | 24 (parser, metadata, summary, config, CLI scaffold) | 24 |
| 2 | 34 (db, redact, writer, orchestrator, queries) | 58 |
| 3 | 24 (watcher, server, daemon) | 82 |
| 4 | 11 (UI static serving, export, timeline) | 93 |
| 5 | 18 (sources[], detection, init_vault re-detect+merge, sources API, CLI extensions) | 111 |

### CI

`.github/workflows/test.yml` runs on `macos-14` against Python 3.11 and 3.12:

1. `ruff check src/ tests/`
2. `mypy src/vimgym/ --ignore-missing-imports` (strict — no `|| true`)
3. `pytest tests/ -v --tb=short --timeout=60`
4. `python -m build`
5. `twine check dist/*`

A separate `shellcheck` job runs on `ubuntu-latest` over `install.sh`.

---

## Known Limitations (v1)

1. **Only Claude Code is parsed.** Cursor, Copilot, Antigravity, and Gemini
   are detected by `vg init` and listed in `vg config sources`, but their
   parsers are not yet written. They are persisted as `enabled=false`.

2. **Subagent JSONL content is not parsed.** Sessions that spawn subagents are
   detected (`has_subagents=true` flag, pink chip in the inbox), but the
   subagent's own conversation files in `{UUID}/subagents/agent-{id}.jsonl`
   are filtered out by `_is_session_file()`. v2 will parse them.

3. **`vg config sources --enable` does not hot-reload the daemon.** The change
   is persisted to `config.json` immediately, but the running watcher doesn't
   pick it up. The CLI prints `(takes effect on next vg start)`. v1.5 could
   add a SIGHUP handler.

4. **Sessions actively being written are indexed once.** Once a session UUID
   exists in the DB, the dedup-by-uuid check skips subsequent writes for that
   session even though the file_hash has changed. The session is captured at
   first stable state. To re-process, manually delete the row and restart.
   v1.5 could re-process when the hash changes for a known UUID.

5. **The vault is single-machine.** No sync, no cloud, no multi-device
   coordination. By design — that is the whole point of "local-first". v2 may
   offer optional encrypted backup to S3/Backblaze.

6. **macOS only for the v1 release.** Linux works (the codebase has no
   macOS-specific calls outside the watchdog FSEvents observer, which has a
   Linux equivalent), but it isn't tested in CI yet.

7. **The settings panel reads but doesn't write the rules file.** Toggling
   individual redaction rules from the browser is v2.

8. **No FTS5 query syntax exposed.** Users can't write `auth AND -test` or
   prefix queries like `auth*`. Every query is auto-quoted by
   `_escape_fts_query()` and tokens are AND'd. This is the right default
   (hyphenated branch names "just work") but power users may want an opt-in
   raw mode in v1.5.

---

## Contributing

- Branch off `main`. Feature branches: `feat/short-description`. Fixes:
  `fix/short-description`. Docs: `docs/short-description`.
- Commit messages use [Conventional Commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- Every PR must:
  - Pass `ruff check`, `mypy`, `pytest`, `python -m build`, and `twine check`
    (the CI workflow runs all of these).
  - Add or update tests for any behavior change.
  - Update [DEVELOPER.md](DEVELOPER.md) if you touched module structure or
    public APIs.
  - Update [GUIDE.md](GUIDE.md) if you changed user-visible behavior.

Run the full check locally before pushing:

```bash
source .venv/bin/activate
ruff check src/ tests/
mypy src/vimgym/ --ignore-missing-imports
pytest tests/ -q
python -m build
twine check dist/*
```
