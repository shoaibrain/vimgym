# Vimgym — Technical Specification & Sprint Plan v1.0

> AI session memory for developers. Local. Fast. No cloud.
> Repo: github.com/shoaibrain/vimgym (already correct)
> Domain: vimgym.xyz
> CLI: `vg`

---

## What We Learned From The Real Files

Before any architecture decision, here is the ground truth from inspecting your actual
Claude Code sessions on your machine.

### Actual File System Layout

```
~/.claude/projects/
└── -Users-shoaibrain-edforge/         ← path-encoded: / becomes -
    ├── eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl   (1.5MB, 148 lines)
    ├── eaa3009a-c5ab-4015-a3e5-af26622652f9/
    │   └── (no subdirs for simple sessions)
    ├── 3438c55b-0df0-4bc0-811e-561afcf19350.jsonl   (4.8MB, 470 lines)
    ├── 3438c55b-0df0-4bc0-811e-561afcf19350/
    │   ├── subagents/
    │   │   ├── agent-a4f66f0cf9f3b7a2d.jsonl
    │   │   ├── agent-a4f66f0cf9f3b7a2d.meta.json
    │   │   └── ...
    │   └── tool-results/
    │       ├── toolu_011YPCuq4xjKfYe6ZZn8dYzj.txt
    │       └── ...
```

**Path encoding rule**: `/Users/shoaibrain/edforge` → `-Users-shoaibrain-edforge`
Forward slashes replaced by dashes. This is the project directory name. Reversible.

### Actual JSONL Message Types

Every line in a `.jsonl` file is one of these six types:

| type | count (typical) | purpose |
|---|---|---|
| `queue-operation` | 2–24 | Session lifecycle: enqueue/dequeue. Contains `content` field on async tasks with task output path |
| `user` | 50–160 | User messages. Contains actual prompts, tool_results, image content |
| `assistant` | 50–250 | Claude responses. Contains text, thinking blocks, tool_use blocks, token usage |
| `file-history-snapshot` | 2–29 | Before/after file state for undo. Contains `trackedFileBackups` map |
| `ai-title` | 1 | Claude-generated session title (e.g. "Implement CORS configuration") |
| `last-prompt` | 1 | Last user prompt text, session UUID |

### Actual Top-Level Fields (All Message Types Combined)

```
aiTitle, content, cwd, entrypoint, gitBranch, isMeta, isSidechain,
isSnapshotUpdate, lastPrompt, message, messageId, operation, parentUuid,
permissionMode, promptId, requestId, sessionId, slug, snapshot,
sourceToolAssistantUUID, timestamp, toolUseResult, type, userType, uuid, version
```

### Key Fields Per Message Type

**user messages** (the important ones):
```json
{
  "type": "user",
  "uuid": "7ee7ed57-...",
  "parentUuid": "8a183c75-...",
  "promptId": "6ce28627-...",
  "sessionId": "eaa3009a-...",
  "timestamp": "2026-04-02T21:16:04.066Z",
  "isMeta": false,
  "isSidechain": false,
  "cwd": "/Users/shoaibrain/edforge",
  "gitBranch": "fee-to-enrollment",
  "entrypoint": "claude-vscode",
  "version": "2.1.89",
  "slug": "wise-purring-flute",
  "permissionMode": "default",
  "message": {
    "role": "user",
    "content": [
      { "type": "text", "text": "..." },
      { "type": "image", "source": { "type": "base64", ... } },
      { "type": "tool_result", "tool_use_id": "toolu_...", "content": [...] }
    ]
  },
  "toolUseResult": { "status": "completed", "prompt": "..." }
}
```

**assistant messages**:
```json
{
  "type": "assistant",
  "uuid": "64233068-...",
  "parentUuid": "e8fc9260-...",
  "sessionId": "eaa3009a-...",
  "timestamp": "2026-04-02T21:16:36.241Z",
  "requestId": "req_011CZfdyN...",
  "cwd": "/Users/shoaibrain/edforge",
  "gitBranch": "fee-to-enrollment",
  "entrypoint": "claude-vscode",
  "version": "2.1.89",
  "slug": "wise-purring-flute",
  "message": {
    "role": "assistant",
    "model": "claude-opus-4-6",
    "id": "msg_01EytuDJv3...",
    "content": [
      { "type": "thinking", "thinking": "..." },
      { "type": "text", "text": "..." },
      {
        "type": "tool_use",
        "id": "toolu_016g2L3...",
        "name": "Bash",
        "input": { "command": "..." },
        "caller": { "type": "direct" }
      }
    ],
    "stop_reason": "tool_use",
    "usage": {
      "input_tokens": 327,
      "output_tokens": 54634,
      "cache_read_input_tokens": 27133445,
      "cache_creation_input_tokens": 1108688
    }
  }
}
```

### Real Tool Names Observed

`Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Agent`,
`TodoWrite`, `ToolSearch`, `ExitPlanMode`, `AskUserQuestion`

### Subagent Architecture

When Claude Code uses the `Agent` tool, it spawns a subagent:
- **Main session**: `{UUID}.jsonl` — contains `tool_use` with `name: "Agent"`
- **Subagent file**: `{UUID}/subagents/agent-{id}.jsonl` — full conversation of the subagent
- **Subagent meta**: `{UUID}/subagents/agent-{id}.meta.json` — structured metadata
- **Tool results**: `{UUID}/tool-results/{tool_use_id}.txt` — output of long-running tools

**This is critical**: a session is not a single file. It is a root JSONL + a companion
directory. The parser must handle both.

### Session Stats From Real Files

| session | size | lines | duration | tools |
|---|---|---|---|---|
| eaa3009a | 1.5MB | 148 | 25 min | Bash, Write, Read |
| 3438c55b | 4.8MB | 470 | 5h 20m | Full suite incl. Agent |
| 64778c29 | 4.5MB | 707 | 3h 47m | Full suite incl. Agent |
| 64b0bec2 | 0.3MB | 145 | 11 min | Edit, Grep, Agent |
| 68568954 | 2.0MB | 488 | 22h 2m | Full suite incl. Agent |

---

## V1 Scope — Locked

**Problem 1: Session loss**
Claude Code crashes. Computer runs out of memory. Session file gets corrupted.
You lose the transcript and continuation point.

**Problem 2: Knowledge retrieval**
You built EdForge for a year. Hundreds of sessions. You can't find the one where
you made the decision about the auth flow or the CORS config.

**V1 solves exactly these two problems. Nothing else.**

**What ships:**
- Filesystem watcher: captures every session automatically, zero config
- Parser: extracts structured metadata from real JSONL format (as documented above)
- Storage: SQLite with FTS5 full-text search
- REST API: FastAPI on localhost:7337
- Web UI: three-pane browser interface (session list, search, detail view)
- Resumption export: export any session as markdown to paste back into Claude Code
- CLI: `vg start`, `vg stop`, `vg status`, `vg open`, `vg search`

**What does NOT ship in v1:**
- Subagent parsing (parsed as metadata reference only, full content in v2)
- Summarization via Claude API (optional add-on, not core)
- ChatGPT/Cursor support (v2)
- Tags, manual organization (you search instead)
- Any cloud, sync, or collaboration feature

---

## Architecture

### Process Model

Single Python daemon process. Two threads sharing one SQLite connection pool.

```
vg start
    │
    ├─ Thread 1: watchdog.Observer
    │   └─ watches ~/.claude/projects/**/*.jsonl
    │   └─ debounce(5s) → BackupPipeline.process(path)
    │
    └─ Thread 2: uvicorn (FastAPI)
        └─ serves localhost:7337
        └─ REST API + static Web UI
        └─ WebSocket for live session-added events
```

SQLite with WAL mode — allows concurrent reads from web server while watcher writes.
One writer (watcher thread), many readers (web server). No connection contention.

### File System Layout (Vimgym)

```
~/.vimgym/
├── vault.db              ← SQLite database (WAL mode)
├── vault.db-wal          ← WAL file (auto-managed by SQLite)
├── vault.db-shm          ← Shared memory file (auto-managed)
├── config.json           ← AppConfig
├── redaction-rules.json  ← Regex patterns for secret scrubbing
├── sv.pid                ← Daemon PID file
└── logs/
    └── vimgym.log        ← Structured JSON log lines
```

### Database Schema

```sql
-- WAL mode: enabled at connection time
-- PRAGMA journal_mode=WAL;
-- PRAGMA foreign_keys=ON;
-- PRAGMA synchronous=NORMAL;  -- safe with WAL, faster than FULL

-- ─────────────────────────────────────────
-- Core session metadata (structured, filterable)
-- ─────────────────────────────────────────
CREATE TABLE sessions (
    -- Identity
    id              TEXT PRIMARY KEY,    -- SHA256(session_id + started_at)
    session_uuid    TEXT NOT NULL UNIQUE, -- from sessionId field in JSONL
    slug            TEXT,                -- "wise-purring-flute" (human name)

    -- Source location
    source_path     TEXT NOT NULL,       -- abs path to .jsonl file
    project_dir     TEXT NOT NULL,       -- encoded dir name: -Users-shoaibrain-edforge
    project_name    TEXT NOT NULL,       -- decoded: edforge

    -- Context
    cwd             TEXT,                -- /Users/shoaibrain/edforge
    git_branch      TEXT,                -- fee-to-enrollment
    entrypoint      TEXT,                -- claude-vscode
    claude_version  TEXT,                -- 2.1.89
    permission_mode TEXT,                -- default | plan

    -- Time
    started_at      TEXT NOT NULL,       -- ISO8601, from first queue-operation
    ended_at        TEXT,                -- ISO8601, from last message timestamp
    duration_secs   INTEGER,             -- computed: ended_at - started_at

    -- Content stats
    message_count       INTEGER DEFAULT 0,
    user_message_count  INTEGER DEFAULT 0,
    asst_message_count  INTEGER DEFAULT 0,
    tool_use_count      INTEGER DEFAULT 0,
    has_subagents       INTEGER DEFAULT 0, -- 0 or 1

    -- Token accounting (from assistant message.usage fields)
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_write_tokens  INTEGER DEFAULT 0,

    -- Generated metadata
    ai_title            TEXT,            -- from ai-title record
    summary             TEXT,            -- heuristic or Claude-generated
    tools_used          TEXT,            -- JSON array: ["Bash","Edit","Agent"]
    files_modified      TEXT,            -- JSON array of file paths from Edit/Write

    -- Vimgym metadata
    backed_up_at    TEXT NOT NULL,       -- when Vimgym captured this session
    file_hash       TEXT NOT NULL,       -- SHA256 of raw file content (dedup key)
    file_size_bytes INTEGER,
    schema_version  INTEGER DEFAULT 1
);

-- ─────────────────────────────────────────
-- Full-text search (FTS5)
-- porter + unicode61: handles stemming, unicode normalization
-- ─────────────────────────────────────────
CREATE VIRTUAL TABLE sessions_fts USING fts5(
    session_uuid UNINDEXED,   -- join key back to sessions table
    project_name,             -- searchable: "edforge"
    git_branch,               -- searchable: "fee-to-enrollment"
    ai_title,                 -- searchable: "Implement CORS configuration"
    summary,                  -- searchable: generated summary
    user_messages,            -- concatenated text of all user prompts
    asst_messages,            -- concatenated text of all assistant responses
    tools_used,               -- searchable: "Bash Edit Agent"
    files_modified,           -- searchable: "auth.ts db.ts"
    tokenize = 'porter unicode61'
);

-- ─────────────────────────────────────────
-- Raw session storage (redacted)
-- ─────────────────────────────────────────
CREATE TABLE sessions_raw (
    session_uuid    TEXT PRIMARY KEY REFERENCES sessions(session_uuid),
    raw_jsonl       TEXT NOT NULL,       -- full redacted JSONL, newline-delimited
    FOREIGN KEY (session_uuid) REFERENCES sessions(session_uuid) ON DELETE CASCADE
);

-- ─────────────────────────────────────────
-- Per-message index (for detail view, future analytics)
-- ─────────────────────────────────────────
CREATE TABLE messages (
    id              TEXT PRIMARY KEY,    -- message uuid from JSONL
    session_uuid    TEXT NOT NULL REFERENCES sessions(session_uuid),
    parent_uuid     TEXT,                -- parentUuid from JSONL
    type            TEXT NOT NULL,       -- user | assistant
    role            TEXT NOT NULL,       -- user | assistant
    timestamp       TEXT,
    has_tool_use    INTEGER DEFAULT 0,
    has_thinking    INTEGER DEFAULT 0,
    has_image       INTEGER DEFAULT 0,
    tool_names      TEXT,                -- JSON array of tools in this message
    content_json    TEXT NOT NULL,       -- full content array as JSON (redacted)
    FOREIGN KEY (session_uuid) REFERENCES sessions(session_uuid) ON DELETE CASCADE
);
CREATE INDEX idx_messages_session ON messages(session_uuid);
CREATE INDEX idx_messages_timestamp ON messages(timestamp);

-- ─────────────────────────────────────────
-- Project aggregates (rebuilt on demand)
-- ─────────────────────────────────────────
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

-- ─────────────────────────────────────────
-- Config (single-row key-value store)
-- ─────────────────────────────────────────
CREATE TABLE config (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
-- Seed: INSERT OR IGNORE INTO config VALUES ('schema_version', '1');

-- ─────────────────────────────────────────
-- Indexes for common query patterns
-- ─────────────────────────────────────────
CREATE INDEX idx_sessions_project ON sessions(project_name);
CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX idx_sessions_branch ON sessions(git_branch);
CREATE INDEX idx_sessions_hash ON sessions(file_hash);
CREATE INDEX idx_sessions_uuid ON sessions(session_uuid);
```

---

## Module Architecture

```
vimgym/
├── cli.py              ← 5 commands: start, stop, status, open, search
├── config.py           ← AppConfig dataclass, load/save, env overrides
├── daemon.py           ← PID file, start/stop/is_running
├── db.py               ← Connection pool, init, migrations, WAL setup
├── watcher.py          ← watchdog handler, debounce, backfill on startup
├── pipeline/
│   ├── __init__.py
│   ├── orchestrator.py ← process_session(): coordinates all pipeline steps
│   ├── parser.py       ← JSONL → ParsedSession (the hard part)
│   ├── redact.py       ← RedactionEngine: compile patterns, redact strings
│   ├── metadata.py     ← extract_metadata(): project name, duration, tokens
│   └── summary.py      ← heuristic_summary() (Claude API optional in v1.5)
├── storage/
│   ├── __init__.py
│   ├── writer.py       ← upsert_session(), upsert_messages(), upsert_project()
│   └── queries.py      ← list_sessions(), search(), get_session(), get_stats()
├── server.py           ← FastAPI app factory, all routes, WebSocket
└── ui/                 ← Static files served at /
    ├── index.html
    ├── app.js
    ├── style.css
    └── vendor/
        └── highlight.min.js   ← Bundled, no CDN
```

---

## Pipeline: Step by Step

```
watchdog fires FileModifiedEvent on path/to/UUID.jsonl
    │
    ▼
[debounce] accumulate events for this path
    wait 5s of silence (no new events for this path)
    check: file size stable across 2 polls (1s apart)?
    if not stable after 15s timeout: proceed anyway, log warning
    │
    ▼
[dedup check]
    compute SHA256 of file content
    check sessions.file_hash in DB
    if match: skip, log "already indexed"
    also check sessions.session_uuid (handles file rename/move)
    │
    ▼
[parser.py: ParsedSession]
    open file, read line by line (streaming, NOT load-all)
    for each line:
        try: obj = json.loads(line)
        except JSONDecodeError: log warning, skip line, continue
    
    dispatch on obj['type']:
    
    'queue-operation':
        operation == 'enqueue': set started_at = obj.timestamp
        operation == 'dequeue': (ignore for v1)
        check obj.get('content') for task-notification XML:
            extract: task-id, tool-use-id, status, output-file path
    
    'user':
        skip if obj.get('isMeta') == True
        extract: uuid, parentUuid, timestamp, cwd, gitBranch,
                 entrypoint, version, slug, permissionMode, sessionId
        extract content blocks:
            type == 'text': accumulate to user_messages_text
            type == 'image': set has_image = True (don't store base64)
            type == 'tool_result': extract tool_use_id, note result
        extract toolUseResult if present
    
    'assistant':
        extract: uuid, parentUuid, timestamp, requestId, model
        extract from message.usage:
            input_tokens, output_tokens,
            cache_read_input_tokens, cache_creation_input_tokens
        extract content blocks:
            type == 'thinking': discard (too large, internal)
            type == 'text': accumulate to asst_messages_text
            type == 'tool_use':
                name → add to tools_used set
                if name in ('Write', 'Edit'): extract input.path → files_modified
                if name == 'Agent': set has_subagents = True
    
    'file-history-snapshot':
        extract trackedFileBackups keys → add to files_modified set
    
    'ai-title':
        set ai_title = obj.aiTitle
    
    'last-prompt':
        verify sessionId matches, update last_prompt field
    
    → return ParsedSession dataclass
    │
    ▼
[redact.py: RedactionEngine]
    compile regex patterns at engine init (not per-call)
    apply to: user_messages_text, asst_messages_text, raw_jsonl
    patterns:
        anthropic key: sk-ant-[a-zA-Z0-9_-]{60,}
        openai key:    sk-[a-zA-Z0-9_-]{40,}
        aws access:    AKIA[0-9A-Z]{16}
        aws secret:    (?i)aws.secret.{0,20}[=:]\s*[a-zA-Z0-9/+]{40}
        bearer token:  Bearer\s+[a-zA-Z0-9._\-]{20,}
        github token:  ghp_[a-zA-Z0-9_]{36,}
        database url:  (mongodb|postgres|mysql|redis)://[^\s]{8,}
        jwt:           eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+
        pem block:     -----BEGIN [A-Z ]+-----[\s\S]+?-----END [A-Z ]+-----
        env secret:    (?i)(password|secret|api_key|token)\s*=\s*\S{8,}
    
    IMPORTANT: redact raw_jsonl but skip image base64 blocks entirely
    (they are already excluded from raw_jsonl storage by parser)
    │
    ▼
[metadata.py: extract_metadata]
    project_name:
        option 1: decode project_dir path encoding (- → /)
                  take last component of decoded path
        option 2: take last component of cwd field
        example: -Users-shoaibrain-edforge → edforge
    
    duration_secs:
        parse started_at and ended_at as ISO8601
        compute delta in seconds
        if ended_at is None: use last message timestamp
    
    files_modified:
        deduplicate the set, sort, limit to 50 paths
        strip absolute path prefix matching cwd for display
    
    token_estimate fallback:
        if no token data in messages: len(user_messages_text + asst_messages_text) // 4
    │
    ▼
[summary.py: heuristic_summary]
    title = ai_title if present, else "Untitled session"
    first_prompt = first non-empty user message text, truncated to 120 chars
    files_str = ", ".join(files_modified[:3]) + ("..." if len > 3 else "")
    tools_str = ", ".join(sorted(tools_used))
    
    template:
    "{title}. {first_prompt}. Modified: {files_str}. Tools: {tools_str}."
    truncate entire summary to 280 chars
    │
    ▼
[writer.py: upsert_session]
    BEGIN TRANSACTION
    
    INSERT OR REPLACE INTO sessions (all fields)
    INSERT OR REPLACE INTO sessions_raw (session_uuid, raw_jsonl)
    
    DELETE FROM sessions_fts WHERE session_uuid = ?
    INSERT INTO sessions_fts (session_uuid, project_name, ...)
    
    DELETE FROM messages WHERE session_uuid = ?
    INSERT INTO messages for each user/assistant message
    
    INSERT OR REPLACE INTO projects (upsert aggregates)
    
    COMMIT
    
    emit WebSocket event: {"type": "session_added", "session": {...}}
```

---

## Parser Edge Cases (Non-Negotiable)

1. **Partial file write**: Claude Code appends mid-JSON during streaming. Last line may be
   malformed. `json.JSONDecodeError` → skip line, log at DEBUG, continue. Do not crash.

2. **Image base64 in user messages**: `content[].type == "image"` with `source.data` containing
   megabytes of base64. Do NOT store in DB. Set `has_image = True` flag, skip data entirely.

3. **Thinking blocks**: `content[].type == "thinking"` in assistant messages. These are Claude's
   internal reasoning (can be 10KB+). Store `has_thinking = True` flag. Do not index for search.

4. **File size stability**: Some sessions are written continuously over hours (68568954: 22h).
   Debounce timer must reset on each modification event. Only process when file is truly stable.

5. **Session UUID vs file hash dedup**: If Claude Code rotates sessions (same UUID, new file),
   file_hash changes → we back up as new version. If the exact same file is copied,
   file_hash matches → skip. UUID dedup catches exact same session backed up twice.

6. **Subagent files**: `{UUID}/subagents/agent-{id}.jsonl` are separate parse targets.
   In v1: detect their existence, store `has_subagents = True` on parent session,
   list subagent file paths in metadata. Full subagent content parsing is v2.

7. **Missing timestamps**: Some messages may lack `timestamp`. Fallback order:
   (1) message.timestamp, (2) file mtime, (3) None. Never crash on missing timestamps.

8. **Path encoding edge cases**: `/Users/shoaibrain/my-project` → `-Users-shoaibrain-my-project`
   Edge: project dir contains dashes already. Example: `/Users/shoaibrain/my-cool-project`
   → `-Users-shoaibrain-my-cool-project`. Cannot distinguish original dashes from path separators.
   Solution: always use `cwd` field from JSONL messages as ground truth. Fall back to dir name.

9. **Empty or minimal sessions**: `1fb8b1b8` had only 1 line (`last-prompt` record, 293B).
   Parser must handle sessions with 0 user messages, 0 assistant messages without crashing.

10. **Large tool output**: `queue-operation` records with `content` field contain task-notification
    XML. Parse with string methods, not XML parser (overkill). Extract: task-id, status, output-file.

---

## REST API Endpoints

```
GET  /health
     → {"status": "ok", "version": "0.1.0", "sessions": 312, "uptime_secs": 3600}

GET  /api/sessions
     ?project=edforge
     &branch=dev
     &since=2026-03-01              (ISO date or Nd: "7d", "30d")
     &until=2026-04-06
     &limit=50 &offset=0
     → {"sessions": [...], "total": 312, "has_more": true}

GET  /api/sessions/:uuid_prefix
     → full session record + messages array
     → 404 if not found
     → 409 {"error": "ambiguous_id", "matches": [...]} if prefix matches multiple

GET  /api/search
     ?q=CORS+configuration
     &project=edforge
     &branch=dev
     &since=7d &until=
     &tool=Bash
     &has_subagents=true
     &limit=20
     → {"results": [...], "query": "CORS configuration", "total": 8}
     each result: session fields + snippet (match excerpt with <mark> tags)

GET  /api/projects
     → [{"project_name": "edforge", "session_count": 84, "last_active": "...", ...}]

GET  /api/stats
     → {"total_sessions": 312, "total_duration_secs": 847200, "db_size_bytes": 45000000,
         "top_projects": [...], "top_tools": [...], "sessions_this_week": 12}

GET  /api/stats/timeline
     ?since=365d
     → {"days": [{"date": "2026-04-05", "count": 3}, ...]}

GET  /api/sessions/:uuid/export
     ?format=markdown
     → text/markdown response, Content-Disposition: attachment; filename=...

PATCH /api/sessions/:uuid/tags       (v1.5)
      body: {"tags": ["auth", "cors"]}

DELETE /api/sessions/:uuid           (dangerous, requires confirm=true param)

GET  /ws
     → WebSocket: streams {"type": "session_added", "session": {...}} events
```

---

## Web UI Layout

Three-pane layout. No framework. Vanilla JS + CSS Grid.

```
┌─────────────────────────────────────────────────────────────┐
│  ⬡ vimgym   [Cmd+K — search all sessions]      ● live  ⚙   │
├──────────────┬────────────────────────┬─────────────────────┤
│ PROJECTS     │  Sessions              │  Session Detail     │
│              │                        │                     │
│ All  (312)   │ ● edforge   5h  today  │  edforge / dev      │
│ edforge(84)  │   Implement CORS and   │  Apr 5 · 5h 20m     │
│ vimgym  (3)  │   domain configuration │  claude-opus-4-6    │
│              │                        │  Branch: dev        │
│ BRANCHES     │ ● edforge  3h  2d ago  │  Slug: woolly-kite  │
│ dev    (44)  │   Finance service      │                     │
│ fee-enr(31)  │   production audit     │  TOOLS (10)         │
│              │                        │  Bash Edit Agent    │
│ TOOLS USED   │ ● edforge  25m  4d ago │  Write Grep Read    │
│ Bash   (71)  │   Resolve CloudFormation│                    │
│ Edit   (58)  │   stack dependencies   │  FILES MODIFIED (4) │
│ Agent  (23)  │                        │  server/bin/ecs-... │
│              │ [Load more...]         │  server/lib/cors.ts │
│ TIMELINE     │                        │                     │
│ [heatmap]    │                        │  ─────────────────  │
│              │                        │                     │
│ ⚙ Settings  │                        │  👤 These Two       │
└──────────────┴────────────────────────┤     CloudFormation  │
                                        │     stacks are stuck│
                                        │                     │
                                        │  🤖 I'll analyze   │
                                        │     the circular... │
                                        │  ┌───────────────┐  │
                                        │  │ aws cloudform │□ │
                                        │  │ ation delete  │  │
                                        │  └───────────────┘  │
                                        │                     │
                                        │  [Export Markdown]  │
                                        └─────────────────────┘
```

---

## Bootstrap Guide — From Zero to Running Locally

### Current State (from screenshot)

Your repo `github.com/shoaibrain/vimgym` is already clean:
- React boilerplate deleted, `git commit "clean"` pushed
- `data/` directory exists at repo root with your real session files
- Remaining: `.gitignore`, `public/` (empty), `src/` (empty), `data/`

### The `data/` Directory — Dev/Test Strategy

**This is the key architectural decision for development.**

In production, vimgym watches `~/.claude/projects/`. But during development
and testing, the watcher is pointed at `./data/` in the repo root. This means:

1. All tests run against your real session files without touching your live system
2. Claude Code (the agent building vimgym) has direct access to realistic data
3. You can add/update sessions by copying from `~/.claude/projects/` to `data/`
4. CI runs against the same fixture data — deterministic, reproducible

```
vimgym/
├── data/                          ← watcher target in dev/test mode
│   ├── -Users-shoaibrain-edforge/ ← project directory (path-encoded)
│   │   ├── 3438c55b-...-561afcf19350.jsonl    (4.8MB)
│   │   ├── 3438c55b-...-561afcf19350/
│   │   │   ├── subagents/
│   │   │   └── tool-results/
│   │   ├── 64778c29-...-0bcde0cfa08b.jsonl    (4.5MB)
│   │   ├── 64b0bec2-...-f804ea250cf5.jsonl    (0.3MB)
│   │   ├── 68568954-...-958ffec228eb.jsonl    (2.0MB)
│   │   └── eaa3009a-...-af26622652f9.jsonl    (1.5MB)
│   └── README.md                  ← "Do not commit real secrets"
├── src/
│   └── vimgym/                    ← Python package
├── tests/
│   └── fixtures/                  ← symlink or copy of data/ files for pytest
├── defaults/
├── pyproject.toml
└── .gitignore                     ← MUST include: data/*.jsonl rule with caution
```

**`data/` gitignore strategy:**

```gitignore
# data/ is tracked but session files may contain secrets.
# Run redaction before committing new session files.
# Large files are not committed — use git-lfs or keep local only.
data/**/*.jsonl
data/**/*.txt
data/**/*.meta.json
# Keep the directory structure tracked:
!data/.gitkeep
!data/README.md
```

This means session files in `data/` are local-only on your machine and the
Claude Code agent's working context — not pushed to GitHub. The directory
structure (subdirs) can be committed via `.gitkeep` files.

### Step 1: Scaffold the Python package

```bash
cd ~/path/to/vimgym   # your local clone

# Create Python package structure
mkdir -p src/vimgym/pipeline src/vimgym/storage src/vimgym/ui/vendor
mkdir -p tests/fixtures tests/integration tests/perf
mkdir -p defaults

# Package init files
touch src/vimgym/__init__.py
touch src/vimgym/cli.py
touch src/vimgym/config.py
touch src/vimgym/daemon.py
touch src/vimgym/db.py
touch src/vimgym/watcher.py
touch src/vimgym/server.py
touch src/vimgym/pipeline/__init__.py
touch src/vimgym/pipeline/orchestrator.py
touch src/vimgym/pipeline/parser.py
touch src/vimgym/pipeline/redact.py
touch src/vimgym/pipeline/metadata.py
touch src/vimgym/pipeline/summary.py
touch src/vimgym/storage/__init__.py
touch src/vimgym/storage/writer.py
touch src/vimgym/storage/queries.py

# data/ directory setup
touch data/.gitkeep
cat > data/README.md << 'DATAREADME'
# data/

This directory contains Claude Code session files for development and testing.

## Usage
- Watcher in dev mode: `VIMGYM_WATCH_PATH=./data vg start`
- Pytest: fixtures read from this directory directly
- Update: `cp ~/.claude/projects/-Users-shoaibrain-edforge/*.jsonl data/-Users-shoaibrain-edforge/`
  then run redaction pass before committing

## Security
Session files are gitignored. Never commit raw session files without running
redaction first. See: `vg redact-dir data/`
DATAREADME
```

### Step 2: `pyproject.toml`

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
    "httpx",      # for TestClient
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

[tool.mypy]
python_version = "3.11"
strict = true
```

### Step 3: `defaults/` config files

```bash
cat > defaults/redaction-rules.json << 'RULES'
{
  "version": 1,
  "rules": [
    {"name": "anthropic_key",  "pattern": "sk-ant-[a-zA-Z0-9_\-]{60,}",              "replacement": "[REDACTED_ANTHROPIC_KEY]"},
    {"name": "openai_key",     "pattern": "sk-[a-zA-Z0-9_\-]{40,}",                  "replacement": "[REDACTED_OPENAI_KEY]"},
    {"name": "aws_access",     "pattern": "AKIA[0-9A-Z]{16}",                          "replacement": "[REDACTED_AWS_KEY]"},
    {"name": "aws_secret",     "pattern": "(?i)aws.secret.{0,20}[=:]\s*[a-zA-Z0-9/+]{40}", "replacement": "[REDACTED_AWS_SECRET]"},
    {"name": "bearer_token",   "pattern": "Bearer\s+[a-zA-Z0-9._\-]{20,}",           "replacement": "Bearer [REDACTED]"},
    {"name": "github_token",   "pattern": "ghp_[a-zA-Z0-9_]{36,}",                    "replacement": "[REDACTED_GITHUB_TOKEN]"},
    {"name": "jwt",            "pattern": "eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+", "replacement": "[REDACTED_JWT]"},
    {"name": "database_url",   "pattern": "(mongodb|postgres|mysql|redis)://[^\s]{8,}", "replacement": "[REDACTED_DB_URL]"},
    {"name": "pem_block",      "pattern": "-----BEGIN [A-Z ]+-----[\s\S]+?-----END [A-Z ]+-----", "replacement": "[REDACTED_PEM_BLOCK]"},
    {"name": "env_secret",     "pattern": "(?i)(password|secret|api_key|private_key)\s*=\s*\S{8,}", "replacement": "\1=[REDACTED]"}
  ]
}
RULES

cat > defaults/config.json << 'CFG'
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
CFG
```

### Step 4: Install and verify

```bash
# Create venv
python3.11 -m venv .venv
source .venv/bin/activate

# Install in dev mode
pip install -e ".[dev]"

# Verify entry point
vg --version   # → 0.1.0
vg --help      # → lists start, stop, status, open, search
```

### Step 5: Symlink `data/` as pytest fixture source

```bash
# tests/conftest.py will reference data/ directly.
# No symlink needed — use Path(__file__).parent.parent / "data"
# This works regardless of where pytest is invoked from.

# Verify data/ has your session files:
ls data/-Users-shoaibrain-edforge/*.jsonl
# should show 5 jsonl files
```

### Step 6: Run Sprint 1 first test

```bash
# After T1.1 and T1.2 are implemented:
pytest tests/test_parser.py -v

# After T1.2 (parser functional):
python -c "
from vimgym.pipeline.parser import parse_session
from pathlib import Path
import json

session = parse_session(Path('data/-Users-shoaibrain-edforge/eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl'))
print(json.dumps({
    'session_uuid': session.session_uuid,
    'ai_title': session.ai_title,
    'project': session.cwd,
    'branch': session.git_branch,
    'tools': session.tools_used,
    'duration_secs': None,  # computed in metadata.py
    'messages': len(session.messages),
    'parse_errors': session.parse_errors,
}, indent=2))
"
```

### Step 7: Dev vs Production watch path

The `AppConfig` supports environment variable override:

```bash
# Development — watch the data/ directory in repo:
VIMGYM_WATCH_PATH=./data vg start

# Production — watch real Claude Code sessions:
vg start   # uses default ~/.claude/projects
```

This single env var is the only difference between dev and prod mode.
All tests use `VIMGYM_WATCH_PATH=./data` implicitly via `conftest.py`.


## Sprint Plan

---

## Sprint 1: Parser — Ground Truth

**Goal**: Parse a real Claude Code JSONL file into a `ParsedSession` dataclass.
All edge cases handled. Tested against real fixture files.

**Demo**: `python -m vimgym.pipeline.parser tests/fixtures/session_simple.jsonl`
prints structured JSON of extracted metadata. No crash on any fixture file.

---

### T1.1 — `ParsedSession` and `ParsedMessage` dataclasses (`pipeline/parser.py`)

Define the output types before writing any parsing logic.

```python
@dataclass
class ParsedMessage:
    uuid: str
    parent_uuid: str | None
    type: str                    # 'user' | 'assistant'
    role: str
    timestamp: str | None
    has_tool_use: bool
    has_thinking: bool
    has_image: bool
    tool_names: list[str]        # tools invoked in this message
    content_json: str            # full content array as JSON, images stripped

@dataclass
class ParsedSession:
    session_uuid: str
    slug: str | None
    ai_title: str | None
    last_prompt: str | None

    source_path: str
    project_dir: str             # -Users-shoaibrain-edforge (raw encoded)
    cwd: str | None              # /Users/shoaibrain/edforge
    git_branch: str | None
    entrypoint: str | None
    claude_version: str | None
    permission_mode: str | None

    started_at: str | None
    ended_at: str | None

    messages: list[ParsedMessage]
    user_messages_text: str      # concat of all user text content (for FTS)
    asst_messages_text: str      # concat of all assistant text content (for FTS)

    tools_used: list[str]        # sorted unique list
    files_modified: list[str]    # from Write/Edit tool inputs + file-history-snapshot
    has_subagents: bool

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    raw_jsonl: str               # full file content (images stripped from base64)
    file_hash: str               # SHA256 of raw file before image strip
    parse_errors: list[str]      # non-fatal warnings from parse
```

**Test** (`tests/test_parser.py`):
- Dataclass instantiation with all fields works
- All fields have correct types

---

### T1.2 — Core JSONL parser (`pipeline/parser.py`)

Implement `parse_session(filepath: Path) -> ParsedSession`.

- Open file in streaming mode (`for line in f`), never `f.read()` (files up to 4.8MB)
- `json.JSONDecodeError` on any line → append to `parse_errors`, continue
- Dispatch on `obj['type']`:
  - `queue-operation` → extract `started_at` from first `enqueue` timestamp
  - `user` → skip if `isMeta == True`; extract all fields per spec above
  - `assistant` → extract content blocks, token usage
  - `file-history-snapshot` → extract `trackedFileBackups` keys
  - `ai-title` → set `ai_title`
  - `last-prompt` → set `last_prompt`
- Image handling: `content[].type == 'image'` → set `has_image = True`, **omit base64 data** from `raw_jsonl` and `content_json`. Replace with `{"type": "image", "omitted": true, "media_type": "image/png"}`
- Thinking handling: `content[].type == 'thinking'` → set `has_thinking = True`, omit text from FTS fields, keep in `content_json`

**Test** (`tests/test_parser.py`):
- `session_simple.jsonl` → correct session_uuid, slug, ai_title, cwd, git_branch
- `session_with_agents.jsonl` → `has_subagents == True`, tool_names includes "Agent"
- `session_with_agents.jsonl` → all 10 tool types present in `tools_used`
- Malformed line in file → parse_errors non-empty, rest of session parsed correctly
- Image content → `has_image == True`, no base64 in `raw_jsonl`
- Token totals match manually computed values from fixture

---

### T1.3 — File hash and raw JSONL handling

- `file_hash`: SHA256 of raw file content **before** any modification
- `raw_jsonl`: file content with base64 image data replaced by `{"type":"image","omitted":true}`
- Both computed during single streaming parse pass (not two file reads)

**Test**: `file_hash` is stable (same file → same hash); image omission reduces raw_jsonl size

---

### T1.4 — Project name decoder (`pipeline/metadata.py`)

Implement `decode_project_name(project_dir: str, cwd: str | None) -> str`:
- Primary: if `cwd` present → `Path(cwd).name` (last component of actual path)
- Fallback: decode `project_dir` by reversing path encoding (first dash → `/`, remaining dashes may be original dashes in dir names — therefore `cwd` is ground truth)
- Edge: `cwd == None` → use `project_dir.split('-')[-1]` as best-effort

**Test**: `cwd="/Users/shoaibrain/edforge"` → `"edforge"`; `cwd="/Users/shoaibrain/my-cool-api"` → `"my-cool-api"`; `cwd=None`, `project_dir="-Users-shoaibrain-edforge"` → `"edforge"`

---

### T1.5 — Metadata extractor (`pipeline/metadata.py`)

Implement `extract_metadata(session: ParsedSession) -> SessionMetadata`:
- `duration_secs`: parse ISO8601 timestamps with `datetime.fromisoformat()`, delta in seconds
- `files_modified`: deduplicate, strip `cwd` prefix for display, limit 50
- `message_count`, `user_message_count`, `asst_message_count`, `tool_use_count`

**Test**: duration computed correctly; files deduped; message counts match fixture

---

### T1.6 — Heuristic summarizer (`pipeline/summary.py`)

Implement `heuristic_summary(session: ParsedSession) -> str`:
- `"{ai_title}. {first_user_prompt[:120]}. Files: {top_3_files}. Tools: {tools}."`
- Max 280 chars with `...` truncation
- Graceful: no ai_title → "Untitled"; no files → omit files clause; no tools → omit

**Test**: output ≤ 280 chars; known fixture → expected output; empty session → non-empty string

---

### T1.7 — CLI entry point (`cli.py`) + 5-command argparse

- `vg start`, `vg stop`, `vg status`, `vg open`, `vg search`
- All stubs: print `"[command]: not yet implemented"`, exit 1
- `vg --version` → print `0.1.0`
- `vg --help` → print all commands

**Test**: `vg --version` exits 0; `vg --help` exits 0; `vg start` exits 1 with message

---

### T1.8 — `tests/conftest.py`: shared fixtures

```python
# data/ directory is the source of truth for all session fixtures.
# It contains real (redacted) Claude Code session files.
# Tests reference it directly — no copying to tests/fixtures/ needed.

DATA_DIR = Path(__file__).parent.parent / "data" / "-Users-shoaibrain-edforge"

@pytest.fixture
def simple_session_path() -> Path:
    """Smallest real session: eaa3009a (1.5MB, 148 lines, no subagents)"""
    return DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"

@pytest.fixture
def agents_session_path() -> Path:
    """Large session with Agent tool: 3438c55b (4.8MB, 470 lines, 6 subagents)"""
    return DATA_DIR / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl"

@pytest.fixture
def minimal_session_path() -> Path:
    """Minimal session: 1fb8b1b8 (293B, 1 line, last-prompt only)"""
    return DATA_DIR / "1fb8b1b8-6cb3-4e34-8446-fa60ba5df626.jsonl"

@pytest.fixture
def all_session_paths() -> list[Path]:
    """All 5 real sessions — for batch/integration tests"""
    return sorted(DATA_DIR.glob("*.jsonl"))

@pytest.fixture
def parsed_simple(simple_session_path) -> ParsedSession:
    return parse_session(simple_session_path)

@pytest.fixture
def parsed_agents(agents_session_path) -> ParsedSession:
    return parse_session(agents_session_path)

@pytest.fixture
def tmp_db(tmp_path) -> Path:
    db_path = tmp_path / "vault.db"
    init_db(db_path)
    return db_path
```

**Validation**: `pytest --collect-only` shows all fixtures; no import errors

---

## Sprint 2: Storage — SQLite + Redaction

**Goal**: Full pipeline from parsed session to database. Redaction applied before storage.
Search returns correct results. All in SQLite, no external services.

**Demo**: `python -m vimgym.pipeline.orchestrator tests/fixtures/session_simple.jsonl`
→ session inserted → `sqlite3 ~/.vimgym/vault.db "SELECT ai_title, duration_secs FROM sessions"` shows the row.

---

### T2.1 — Database init + WAL setup (`db.py`)

- `init_db(db_path: Path)`:
  - Create `db_path.parent` if not exists (`chmod 700`)
  - `sqlite3.connect(db_path)` with `check_same_thread=False`
  - Set `PRAGMA journal_mode=WAL`
  - Set `PRAGMA synchronous=NORMAL`
  - Set `PRAGMA foreign_keys=ON`
  - Verify FTS5 available: `CREATE VIRTUAL TABLE _fts5_test USING fts5(x); DROP TABLE _fts5_test;`
    If fails: `raise RuntimeError("SQLite FTS5 not available. Python was built without FTS5 support.")`
  - Execute full schema DDL from spec above
  - `INSERT OR IGNORE INTO config VALUES ('schema_version', '1')`
  - Set `PRAGMA db_path chmod 600` after creation
- `get_connection(db_path: Path) -> sqlite3.Connection`:
  - Returns connection with `row_factory = sqlite3.Row`
  - Thread-local connections (one per thread, WAL handles concurrency)

**Test** (`tests/test_db.py`):
- `init_db()` creates file with correct permissions (0o600)
- All tables exist after init
- FTS5 missing → `RuntimeError` with clear message
- Idempotent: `init_db()` twice → no error, no duplicate tables
- WAL mode confirmed: `PRAGMA journal_mode` returns `'wal'`

---

### T2.2 — Redaction engine (`pipeline/redact.py`)

- `RedactionEngine(rules_path: Path)`:
  - Load rules from JSON at init
  - Compile all regex patterns at init (not per call)
  - `_patterns: list[tuple[str, re.Pattern, str]]` → (name, pattern, replacement)
- `redact_text(text: str) -> str`:
  - Apply all compiled patterns sequentially
  - Replacement: `[REDACTED_{NAME}]`
- `redact_session_raw(raw_jsonl: str) -> str`:
  - Apply `redact_text` to each line of JSONL individually
  - Skip lines where `json.loads` fails (already logged)
  - Skip image base64 fields (already omitted by parser)

Default rules file at `defaults/redaction-rules.json`:
```json
{
  "version": 1,
  "rules": [
    {"name": "anthropic_key",  "pattern": "sk-ant-[a-zA-Z0-9_\\-]{60,}", "replacement": "[REDACTED_ANTHROPIC_KEY]"},
    {"name": "openai_key",     "pattern": "sk-[a-zA-Z0-9_\\-]{40,}", "replacement": "[REDACTED_OPENAI_KEY]"},
    {"name": "aws_access",     "pattern": "AKIA[0-9A-Z]{16}", "replacement": "[REDACTED_AWS_KEY]"},
    {"name": "aws_secret",     "pattern": "(?i)aws.secret.{0,20}[=:]\\s*[a-zA-Z0-9/+]{40}", "replacement": "[REDACTED_AWS_SECRET]"},
    {"name": "bearer_token",   "pattern": "Bearer\\s+[a-zA-Z0-9._\\-]{20,}", "replacement": "Bearer [REDACTED]"},
    {"name": "github_token",   "pattern": "ghp_[a-zA-Z0-9_]{36,}", "replacement": "[REDACTED_GITHUB_TOKEN]"},
    {"name": "jwt",            "pattern": "eyJ[a-zA-Z0-9_\\-]+\\.[a-zA-Z0-9_\\-]+\\.[a-zA-Z0-9_\\-]+", "replacement": "[REDACTED_JWT]"},
    {"name": "database_url",   "pattern": "(mongodb|postgres|mysql|redis)://[^\\s]{8,}", "replacement": "[REDACTED_DB_URL]"},
    {"name": "pem_block",      "pattern": "-----BEGIN [A-Z ]+-----[\\s\\S]+?-----END [A-Z ]+-----", "replacement": "[REDACTED_PEM_BLOCK]"},
    {"name": "env_secret",     "pattern": "(?i)(password|secret|api_key|private_key)\\s*=\\s*\\S{8,}", "replacement": "\\1=[REDACTED]"}
  ]
}
```

**Test** (`tests/test_redact.py`):
- Each pattern: inject known secret → confirm replacement present, secret absent
- `session_with_agents.jsonl` fixture (should have no real secrets after pre-commit redaction) → output identical to input
- Normal code content unchanged
- PEM block multi-line → redacted
- JWT in URL context → redacted

---

### T2.3 — Session writer (`storage/writer.py`)

- `upsert_session(conn, session: ParsedSession, metadata: SessionMetadata, summary: str) -> str`:
  - Single transaction covering all tables
  - `sessions`: INSERT OR REPLACE
  - `sessions_raw`: INSERT OR REPLACE
  - `sessions_fts`: DELETE WHERE session_uuid=?, then INSERT
  - `messages`: DELETE WHERE session_uuid=?, then bulk INSERT
  - `projects`: INSERT OR REPLACE with aggregated counts
  - Return session `id` (SHA256 composite key)

**Test** (`tests/test_writer.py`):
- Insert fixture → all 4 tables have rows
- Re-insert same session → no duplicate rows (idempotent)
- `sessions_fts` row searchable by `project_name`
- `projects` row has correct `session_count`
- Transaction rollback on failure (simulate write error mid-transaction)

---

### T2.4 — Full pipeline orchestrator (`pipeline/orchestrator.py`)

- `process_session(filepath: Path, db_path: Path, rules_path: Path) -> ProcessResult`:
  ```
  1. file_hash = sha256(filepath)
  2. if session_exists_by_hash(conn, file_hash): return ProcessResult(skipped=True)
  3. parsed = parse_session(filepath)
  4. if session_exists_by_uuid(conn, parsed.session_uuid): return ProcessResult(skipped=True)
  5. parsed.raw_jsonl = redact_session_raw(parsed.raw_jsonl)
  6. parsed.user_messages_text = redact_text(parsed.user_messages_text)
  7. parsed.asst_messages_text = redact_text(parsed.asst_messages_text)
  8. metadata = extract_metadata(parsed)
  9. summary = heuristic_summary(parsed)
  10. id = upsert_session(conn, parsed, metadata, summary)
  11. return ProcessResult(session_uuid=parsed.session_uuid, success=True)
  ```
- All exceptions caught, logged, returned in `ProcessResult.error`
- Never raises

**Test** (`tests/test_orchestrator.py`):
- End-to-end: fixture file → DB → `SELECT * FROM sessions` has 1 row
- Duplicate call → `ProcessResult(skipped=True)`
- Malformed fixture → `ProcessResult(error=...)`, no crash, no partial DB state

---

### T2.5 — Search queries (`storage/queries.py`)

- `search_sessions(conn, query, project=None, branch=None, since=None, until=None, tool=None, limit=20) -> list[SearchResult]`:
  ```sql
  SELECT s.*, snippet(sessions_fts, 2, '<mark>', '</mark>', '...', 15) as snippet
  FROM sessions_fts
  JOIN sessions s ON s.session_uuid = sessions_fts.session_uuid
  WHERE sessions_fts MATCH ?
  [AND s.project_name = ?]
  [AND s.git_branch = ?]
  [AND s.started_at >= ?]
  [AND s.started_at <= ?]
  ORDER BY rank
  LIMIT ?
  ```
- `list_sessions(conn, project=None, branch=None, since=None, limit=50, offset=0) -> list[SessionRow]`
- `get_session(conn, uuid_prefix: str) -> SessionRow | None` — raises `AmbiguousIDError` if multiple match
- `get_stats(conn) -> StatsRow`

**Test** (`tests/test_queries.py`):
- Insert 3 known sessions → search by keyword unique to one → returns that one first
- `<mark>` present in snippet
- Prefix lookup resolves correctly; ambiguous prefix → exception
- Date filter excludes sessions outside range

---

## Sprint 3: Daemon + REST API

**Goal**: `vg start` runs the watcher + web server. All API endpoints return real data.
`vg search` works from terminal.

**Demo**: `vg start` → `curl localhost:7337/api/sessions` returns your real EdForge sessions
→ `vg search "CORS configuration"` returns the right session.

---

### T3.1 — AppConfig (`config.py`)

```python
@dataclass
class AppConfig:
    vault_dir: Path = Path("~/.vimgym").expanduser()
    watch_path: Path = Path("~/.claude/projects").expanduser()
    server_host: str = "127.0.0.1"        # NEVER 0.0.0.0
    server_port: int = 7337
    debounce_secs: float = 5.0
    stability_polls: int = 2
    stability_poll_interval: float = 1.0
    auto_open_browser: bool = True
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path: return self.vault_dir / "vault.db"
    @property
    def pid_path(self) -> Path: return self.vault_dir / "sv.pid"
    @property
    def log_path(self) -> Path: return self.vault_dir / "logs" / "vimgym.log"
    @property
    def rules_path(self) -> Path: return self.vault_dir / "redaction-rules.json"
```

Load from `~/.vimgym/config.json`. Env override: `VIMGYM_PATH`, `VIMGYM_PORT`.

**Test**: defaults work; config.json overrides; env var overrides config file; unknown keys in JSON preserved

---

### T3.2 — Watcher (`watcher.py`)

```python
class SessionWatcher(FileSystemEventHandler):
    def __init__(self, config: AppConfig, db_path: Path):
        self._debounce: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_modified(self, event: FileModifiedEvent):
        if not event.src_path.endswith('.jsonl'): return
        if os.path.basename(event.src_path).startswith('.'): return
        self._schedule(event.src_path)

    def on_created(self, event: FileCreatedEvent):
        if not event.src_path.endswith('.jsonl'): return
        self._schedule(event.src_path)

    def _schedule(self, path: str):
        with self._lock:
            if path in self._debounce:
                self._debounce[path].cancel()
            timer = threading.Timer(
                self._config.debounce_secs,
                self._process_when_stable,
                args=[path]
            )
            self._debounce[path] = timer
            timer.start()

    def _process_when_stable(self, path: str):
        # Poll until size is stable
        prev_size = -1
        for _ in range(self._config.stability_polls):
            size = os.path.getsize(path)
            if size == prev_size: break
            prev_size = size
            time.sleep(self._config.stability_poll_interval)
        # Run pipeline
        result = process_session(Path(path), self._config.db_path, self._config.rules_path)
        log.info("backed_up", session=result.session_uuid, skipped=result.skipped)
        if self._ws_broadcast:
            self._ws_broadcast(result)

def start_watching(config: AppConfig, ws_broadcast=None) -> Observer:
    watcher = SessionWatcher(config, config.db_path)
    watcher._ws_broadcast = ws_broadcast
    observer = Observer()
    observer.schedule(watcher, str(config.watch_path), recursive=True)
    observer.start()
    # Backfill: scan existing files not yet in DB
    _backfill(config, watcher)
    return observer
```

**Test** (`tests/test_watcher.py`):
- File created in watched dir → `process_session` called (mock)
- File modified twice rapidly → only one call after debounce
- Non-JSONL file → ignored
- Backfill: existing files not in DB → `process_session` called for each

---

### T3.3 — Daemon process manager (`daemon.py`)

- `start_daemon(config)`: write PID, start watcher thread, start uvicorn
- `stop_daemon(config)`: read PID, SIGTERM, wait 5s, SIGKILL, clean PID file
- `is_running(config) -> bool`: check PID file + `os.kill(pid, 0)`
- On SIGTERM: stop observer, close DB connections, exit 0

**Test**: start/stop (mock subprocess); PID file lifecycle; stale PID handled

---

### T3.4 — FastAPI server (`server.py`)

- `create_app(config: AppConfig) -> FastAPI`
- All routes per API spec above (returning real data from `queries.py`)
- Static files: `StaticFiles(directory="src/vimgym/ui", html=True)` at `/`
- CORS: `CORSMiddleware` allow_origins `["http://localhost:7337", "http://127.0.0.1:7337"]`
- WebSocket `/ws`: connection manager, broadcast on new session

**Test** (`tests/test_server.py`): `TestClient`; `/health` → 200; `/api/sessions` → list; `/api/search?q=CORS` → results with `<mark>`; CORS rejects external origin

---

### T3.5 — `vg start`, `vg stop`, `vg status`, `vg open` (`cli.py`)

- `vg start`: `init_db()` if not exists, `start_daemon()`, open browser if configured
- `vg stop`: `stop_daemon()`
- `vg status`: rich table — running/stopped, session count, db size, last backup
- `vg open`: open `http://localhost:{port}` if running, else error

**Test**: each command with mocked daemon; not-running states correct

---

### T3.6 — `vg search` terminal command (`cli.py`)

- If daemon running: `GET http://localhost:{port}/api/search?q=...` via httpx
- If daemon not running: direct SQLite query via `queries.search_sessions()`
- Output: rich table — DATE, ID (8 chars), PROJECT, BRANCH, DURATION, TITLE
- Flags: `--project`, `--branch`, `--since`, `--limit`, `--json`

**Test**: daemon running → uses API (mock httpx); daemon not running → direct DB; `--json` → valid JSON

---

## Sprint 4: Web UI

**Goal**: Browser UI fully functional. Three panes. Real data. Session detail renders
full conversation with syntax-highlighted code. Export works.

**Demo**: `vg start` → browser → search "CORS" → click result → read full conversation
→ click "Export Markdown" → file downloads.

---

### T4.1 — HTML shell + CSS layout

- `src/vimgym/ui/index.html`: three-pane grid, top bar, no framework
- CSS Grid: `grid-template-columns: 220px 1fr 2fr`
- Dark mode: `prefers-color-scheme: dark` media query
- Responsive: <900px → sidebar collapses; <600px → single pane

**Validation**: renders correctly in Chrome 120+; dark mode switches; no console errors

---

### T4.2 — Session inbox (middle pane)

- Fetch `GET /api/sessions?limit=50` on load
- Session card: project name, date (relative), duration, ai_title, tool chips
- Infinite scroll: fetch next page at scroll 80%
- Click → fetch detail, render in right pane
- Active state: left accent border
- Empty state: "No sessions yet. Claude Code sessions will appear here automatically."

**Test**: cards rendered; click fetches `/api/sessions/:uuid`; scroll triggers next page fetch

---

### T4.3 — Sidebar (left pane)

- `GET /api/projects` → project list with counts
- `GET /api/stats` → top branches, top tools
- Click project/branch/tool → add filter to inbox fetch
- "All Sessions" row resets all filters
- Timeline heatmap: `GET /api/stats/timeline?since=365d` → SVG grid (52×7 cells, purple ramp)

**Test**: project click adds `?project=` param; filter chips appear in inbox header; heatmap cell count matches API response

---

### T4.4 — Session detail (right pane)

- Fetch `GET /api/sessions/:uuid`
- Header: project/branch breadcrumb, date, duration, model, slug, permission_mode
- Tool chips, file list (expandable)
- Message list: render `messages` array in order
  - `user` messages: avatar icon, blue left border, text content
  - `assistant` messages: bot icon, purple left border
  - `tool_use` blocks: `<details><summary>🔧 {tool_name}</summary>` + code block
  - Images: `[Image omitted]` badge
  - Thinking blocks: `<details><summary>💭 Thinking</summary>` + pre block

**Test**: messages rendered in correct order; tool_use is `<details>`; image shows badge; correct message count

---

### T4.5 — Code syntax highlighting

- Bundle `highlight.min.js` to `src/vimgym/ui/vendor/` (no CDN)
- Auto-detect language from code block (bash, python, typescript, json, etc.)
- Copy-to-clipboard button on every `<pre><code>` block (vanilla JS `navigator.clipboard`)

**Test**: code blocks have `hljs` class; copy button present; no external HTTP requests in output

---

### T4.6 — Command palette (`Cmd+K`)

- `keydown` listener on `document` for `Meta+K` (macOS)
- Overlay: search input + results list (10 items max)
- Debounce 200ms → `GET /api/search?q=...`
- Keyboard navigation: `↑↓` arrows, `Enter` to open, `Esc` to close
- Result shows: project, date, snippet with `<mark>` terms bolded

**Test**: Cmd+K opens overlay; typing triggers API; Enter closes and opens session; Esc closes

---

### T4.7 — Export endpoint + UI button

- `GET /api/sessions/:uuid/export?format=markdown`:
  - Renders session as markdown: `# {ai_title}`, metadata table, then each message
  - Response headers: `Content-Disposition: attachment; filename="{slug}-{date}.md"`
  - `Content-Type: text/markdown`
- UI: "Export Markdown" button in session detail header → calls export endpoint

**Test**: response is valid markdown; filename correct; all messages present in output; tool_use rendered as code blocks

---

### T4.8 — WebSocket live updates

- `GET /ws` → WebSocket endpoint
- On new session backed up → server broadcasts `{"type":"session_added","session":{...}}`
- UI: prepend new card to inbox with animation; toast: "New session: {ai_title}"
- Reconnect: auto-reconnect after 2s if disconnected

**Test**: mock WebSocket; new session event → inbox prepends card; disconnect → reconnect attempted

---

## Sprint 5: Distribution

**Goal**: `brew install vimgym && vg start` works on a clean macOS.
vimgym.xyz is live with working install script.

---

### T5.1 — `sv init` flow in `vg start`

- If `~/.vimgym/vault.db` not exists: auto-run init without prompting
- Create dirs, copy `defaults/redaction-rules.json`, write `config.json`
- Print: `"Vault initialized at ~/.vimgym"`

**Test**: first-run creates all dirs and files; second-run does not re-initialize

---

### T5.2 — pyproject.toml + pip package

- `MANIFEST.in`: include `src/vimgym/ui/**`, `defaults/**`
- `python -m build` → sdist + wheel
- `twine check dist/*` passes

**Validation**: install from wheel in clean venv; `vg --version` works; `src/vimgym/ui/index.html` in installed package

---

### T5.3 — Homebrew formula (`Formula/vimgym.rb`)

```ruby
class Vimgym < Formula
  desc "AI session memory for developers"
  homepage "https://vimgym.xyz"
  url "https://files.pythonhosted.org/packages/.../vimgym-0.1.0.tar.gz"
  sha256 "..."
  license "MIT"
  depends_on "python@3.11"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "0.1.0", shell_output("#{bin}/vg --version")
  end
end
```

**Validation**: `brew install --build-from-source Formula/vimgym.rb` on macOS; `vg start` runs

---

### T5.4 — curl install script + vimgym.xyz

- `install.sh`: check Python 3.11+; `pip3 install --user vimgym`; PATH check
- vimgym.xyz: hero, demo GIF (recorded with `vhs`), install command, GitHub link
- `shellcheck install.sh` passes

**Validation**: run on clean macOS; `vg --version` works after

---

### T5.5 — GitHub Actions CI

```yaml
on: [push, pull_request]
jobs:
  test:
    runs-on: macos-14
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python-version }}" }
      - run: pip install -e ".[dev]"
      - run: ruff check src/ tests/
      - run: mypy src/vimgym
      - run: pytest tests/ -v --tb=short
```

**Validation**: green CI on `main`; PR status check required

---

## Sprint 6: Hardening

**Goal**: Production-quality error handling. Security audit. Schema versioning.
Performance validated on 500+ sessions.

---

### T6.1 — Security audit

- `bandit -r src/ --severity-level medium` → fix all MEDIUM+
- `vault.db` permissions: `chmod 600` after creation
- Server binds `127.0.0.1` only — reject `0.0.0.0` in config validation
- CORS: audit that no non-localhost origin accepted

**Test**: DB file permissions 0o600; server rejects non-localhost bind; CORS rejects external origin

---

### T6.2 — Schema versioning + `vg upgrade`

- `config` table: `('schema_version', '1')`
- On startup: read version; if < code version → print upgrade message and exit
- `vg upgrade`: run pending migrations, print what ran
- `src/vimgym/migrations/v1_to_v2.py`: no-op (establishes baseline)

**Test**: fresh init → schema_version 1; outdated DB → start exits with message; `vg upgrade` is idempotent

---

### T6.3 — Graceful error handling

- All FastAPI routes: typed JSON error responses `{"error": "code", "message": "..."}`
- All CLI commands: no Python tracebacks to stdout; `--verbose` exposes them
- Unhandled exceptions: logged to `~/.vimgym/logs/vimgym.log` as JSON

**Test**: missing DB → auto-init; API 500 → JSON body; `vg stop` when not running → clean message

---

### T6.4 — `.vimgymignore` support

- File at `~/.vimgym/.vimgymignore`: glob patterns for project dirs to skip
- Watcher checks before `process_session()`
- Hot reload: re-read on file change

**Test**: matching pattern → session not backed up; non-matching → backed up; reload works

---

### T6.5 — Performance baseline

- Script: insert 500 sessions into fixture DB using orchestrator
- `vg search "CORS"` latency < 500ms
- `GET /api/sessions` latency < 200ms
- Watcher: 20 rapid file modifications → exactly 1 `process_session` call (debounce works)

**Validation**: `pytest tests/perf/ -v` passes all timing assertions

---

## Summary

| Sprint | Tasks | Demo |
|---|---|---|
| 1 | 8 | `parse_session()` on real fixture → structured JSON |
| 2 | 5 | Pipeline runs → session in SQLite → search returns it |
| 3 | 6 | `vg start` → API live → `vg search` in terminal works |
| 4 | 8 | Browser UI: search, inbox, detail, export |
| 5 | 5 | `brew install vimgym` on clean macOS → works |
| 6 | 5 | Security audit, schema versioning, perf baseline |
| **Total** | **37** | **Shippable v1.0** |

---

## V1 Non-Goals (Explicitly Out of Scope)

| Feature | Reason | Target |
|---|---|---|
| Subagent content parsing | Complex, additive work | v2 |
| Claude API summarization | Nice to have, not core | v1.5 |
| Tags / manual organization | Search is sufficient for v1 | v2 |
| ChatGPT/Cursor support | Different formats, different parser | v2 |
| Team / shared vaults | Auth, conflict resolution | v3 |
| Remote sync | `cp vault.db` is sufficient | v2 |
| Mobile | Not a developer workflow | v3 |

