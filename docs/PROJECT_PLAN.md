# SessionVault - AI Session Memory for Developers

## Project Plan v1.0

---

## 1. Executive Summary

### The Problem

Every day, millions of developers have deep, productive conversations with AI assistants - debugging complex systems, architecting solutions, learning new frameworks, building features. These sessions contain **distilled knowledge**: the exact reasoning chain that solved a production bug, the architectural decision that shaped a system, the obscure API pattern that took 30 minutes to figure out.

Then the context window resets. The session expires. The history gets buried. **That knowledge is gone.**

Developers are left with the code changes but lose the *reasoning* behind them. They re-discover the same solutions, re-explain the same contexts, and lose the institutional memory of how and why things were built.

### The Solution

**SessionVault** is a developer tool that captures, indexes, and makes searchable your entire history of AI-assisted development sessions. Think of it as **git log for your AI conversations** - every session backed up, tagged, searchable, and browsable.

Starting with Claude Code (v1), SessionVault:
- **Backs up** session JSONL files to a local git repository automatically
- **Indexes** sessions with rich metadata (project, branch, date, files touched, tools used)
- **Searches** across your full AI conversation history with intuitive queries
- **Summarizes** sessions automatically so you can scan months of work at a glance
- **Redacts** sensitive data (API keys, credentials) before storage
- **Renders** sessions as browsable markdown/HTML for easy review

### Why This Matters

A full-time engineer working with AI generates roughly **20-40 sessions per week** across multiple projects. That's **1,000-2,000 sessions per year** - a massive, untapped knowledge base. SessionVault turns that exhaust into an asset.

### Target Users

- Full-time software engineers using Claude Code daily
- Engineering teams wanting to preserve institutional AI-assisted knowledge
- Developers working across multiple projects who need cross-project search
- Anyone who has ever thought: *"I solved this exact problem in a Claude session last month..."*

---

## 2. Core Concepts

### 2.1 The Session as a First-Class Object

A **session** is the atomic unit of SessionVault. Each session represents one continuous interaction with Claude Code and contains:

```
Session
  +-- id (UUID from Claude Code)
  +-- project (derived from cwd / git repo)
  +-- branch (git branch at time of session)
  +-- started_at / ended_at
  +-- duration
  +-- messages[] (user prompts + AI responses)
  +-- tools_used[] (Read, Edit, Bash, etc.)
  +-- files_modified[]
  +-- summary (auto-generated)
  +-- tags[] (user-applied)
  +-- token_count (estimated)
  +-- version (Claude Code version)
```

### 2.2 The Vault

The **vault** is a local git repository that stores all backed-up sessions. Structure:

```
~/.sessionvault/
  +-- vault.json              # Vault configuration
  +-- index.json              # Search index (fast lookups)
  +-- sessions/
  |     +-- 2026/
  |           +-- 04/
  |                 +-- 2026-04-05_e6819902_vimgym.md       # Rendered session
  |                 +-- 2026-04-05_e6819902_vimgym.jsonl     # Raw backup
  |                 +-- 2026-04-05_e6819902_vimgym.meta.json # Metadata
  +-- tags/
  |     +-- auth-feature.json  # Tag -> session mappings
  +-- summaries/
  |     +-- weekly/
  |     +-- monthly/
  +-- .redaction-rules.json    # Sensitive data patterns
  +-- .gitignore
```

### 2.3 The Index

A lightweight JSON-based index that enables fast search without scanning every file. Updated on each backup. Contains:
- Session ID -> file path mapping
- Full-text trigram index for content search
- Project -> sessions mapping
- Tag -> sessions mapping
- Date-sorted session list

---

## 3. Use Cases

### UC-1: Automatic Post-Session Backup
> *"Every time I finish a Claude Code session, my conversation is automatically saved to my vault."*

**Flow:**
1. Developer finishes Claude Code session (exits or session ends)
2. Claude Code SessionEnd hook fires
3. SessionVault copies the JSONL file to the vault
4. Metadata is extracted and indexed
5. Sensitive data is redacted
6. Auto-summary is generated
7. Changes are staged in git (not committed - awaiting review)

### UC-2: Manual Backup
> *"I want to back up a specific session or all recent sessions right now."*

```bash
# Back up current/latest session
sv backup

# Back up a specific session by ID
sv backup e6819902-1b6f-4b63-ab12-aac1619d6ceb

# Back up all sessions from a project
sv backup --project vimgym

# Back up all sessions from the last 7 days
sv backup --since 7d
```

### UC-3: Search - "Find the session where I built X"
> *"I know I built a rate limiter with Claude last month. Where was that?"*

```bash
# Natural keyword search
sv search "rate limiter"

# Search within a specific project
sv search "rate limiter" --project api-server

# Search only in user prompts
sv search "rate limiter" --prompts-only

# Search by date range
sv search "auth" --since 2026-03-01 --until 2026-03-31

# Search by tag
sv search --tag auth-feature

# Find sessions that modified a specific file
sv search --file src/middleware/auth.ts

# Find sessions where a specific tool was used heavily
sv search --tool Bash --min-uses 10
```

### UC-4: Browse Sessions
> *"Show me what I worked on last week with AI."*

```bash
# List recent sessions
sv list --last 7d

# List sessions for a project
sv list --project vimgym

# Open a session in the browser (rendered HTML)
sv view e6819902

# Open the full vault browser
sv browse
```

### UC-5: Tag and Organize
> *"Tag this session as part of the auth-feature work."*

```bash
# Tag a session
sv tag e6819902 auth-feature

# Tag the most recent session
sv tag --latest deployment-fix

# List all tags
sv tags

# Remove a tag
sv untag e6819902 auth-feature
```

### UC-6: Review and Commit
> *"Review what's staged and commit to my vault."*

```bash
# See what's staged
sv status

# Review staged sessions (opens diff)
sv review

# Commit staged sessions
sv commit

# Push vault to remote (if configured)
sv push
```

### UC-7: Session Summaries
> *"Give me a weekly digest of my AI-assisted work."*

```bash
# Generate weekly summary
sv summary --week

# Generate summary for a project
sv summary --project vimgym --since 30d

# View existing summaries
sv summaries
```

### UC-8: Export and Share
> *"Export a session to share with my team."*

```bash
# Export as markdown
sv export e6819902 --format md

# Export as HTML (standalone, styled)
sv export e6819902 --format html

# Export redacted version (extra scrubbing)
sv export e6819902 --format md --redact-strict
```

---

## 4. Technical Architecture

### 4.1 Technology Choice: Bash + Python Hybrid

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| CLI entry point | Bash | Zero-dependency, works everywhere, fast startup |
| Core logic | Python 3.8+ | JSON processing, indexing, search, templating |
| Search index | SQLite FTS5 | Full-text search, battle-tested, zero-config |
| Rendering | Jinja2 templates | Markdown/HTML session rendering |
| Hook integration | Bash script | Claude Code hooks are shell commands |
| Configuration | JSON | Consistent with Claude Code's own config format |

**Why not pure Bash?** JSONL parsing, full-text indexing, and HTML rendering are painful in Bash. Python is ubiquitous on developer machines and gives us SQLite FTS5 for free.

**Why not Rust/Go?** Compilation and distribution overhead. Python can be run directly. V2 could consider a compiled binary for performance.

### 4.2 System Architecture

```
+------------------+       +-------------------+       +------------------+
|   Claude Code    |       |   SessionVault    |       |   Vault Store    |
|                  |       |   CLI (sv)        |       |   (~/.sessionvault)
|  Session JSONL   +------>+                   +------>+                  |
|  ~/.claude/      |       |  - backup         |       |  - sessions/     |
|  projects/       | hook  |  - search         |       |  - index.db      |
|  sessions/       | or    |  - list           |       |  - tags/         |
+------------------+ cmd   |  - tag            |       |  - summaries/    |
                           |  - view           |       |  - .git/         |
                           |  - browse         |       +------------------+
                           |  - export         |
                           |  - status/commit  |       +------------------+
                           |  - summary        |       |   Browser        |
                           |                   +------>+   (HTML viewer)  |
                           +-------------------+       +------------------+
```

### 4.3 Data Flow: Backup Pipeline

```
JSONL File (raw)
    |
    v
[1. Parse] -- Read JSONL, extract messages, metadata
    |
    v
[2. Redact] -- Apply regex patterns to strip secrets
    |           (API keys, tokens, .env values)
    |
    v
[3. Extract Metadata] -- Project, branch, date, duration,
    |                     tools used, files modified
    |
    v
[4. Summarize] -- Generate 1-3 sentence summary from
    |              first/last messages + key actions
    |
    v
[5. Index] -- Update SQLite FTS5 index with content
    |          and metadata for fast search
    |
    v
[6. Store] -- Write .jsonl, .meta.json, .md files
    |          to vault directory structure
    |
    v
[7. Stage] -- git add (staged, not committed)
```

### 4.4 JSONL Schema (Claude Code)

Based on reverse-engineering the actual Claude Code session format:

```jsonc
// Each line is one of these types:

// Queue operations (session lifecycle)
{"type": "queue-operation", "operation": "enqueue|dequeue", "timestamp": "ISO8601", "sessionId": "UUID", "content": "..."}

// User messages
{"type": "user", "message": {"role": "user", "content": "..."}, "uuid": "UUID", "timestamp": "ISO8601", "cwd": "/path", "gitBranch": "branch-name", "version": "2.x.x", ...}

// Assistant messages
{"type": "assistant", "message": {"role": "assistant", "content": [...]}, "uuid": "UUID", "timestamp": "ISO8601", ...}

// Tool uses and results embedded in assistant message content blocks
```

### 4.5 Search Architecture

**SQLite FTS5** provides:
- Full-text search across all message content
- Ranking by relevance (BM25)
- Prefix matching, phrase matching, boolean operators
- Near-zero setup (Python's `sqlite3` is built-in)

```sql
CREATE VIRTUAL TABLE sessions_fts USING fts5(
    session_id,
    project,
    branch,
    user_messages,
    assistant_messages,
    summary,
    tags,
    files_modified,
    tools_used,
    tokenize='porter unicode61'
);
```

Query examples:
```python
# "Find where I built auth"
SELECT * FROM sessions_fts WHERE sessions_fts MATCH 'auth'
ORDER BY rank;

# "Rate limiter in api-server project"
SELECT * FROM sessions_fts
WHERE sessions_fts MATCH 'rate limiter'
AND project = 'api-server';
```

### 4.6 Redaction Engine

Simple, extensible pattern-based redaction:

```json
// .redaction-rules.json
{
  "version": 1,
  "rules": [
    {"name": "api_keys", "pattern": "(?i)(api[_-]?key|apikey)\\s*[=:]\\s*['\"]?([a-zA-Z0-9_\\-]{20,})", "replacement": "$1=***REDACTED***"},
    {"name": "bearer_tokens", "pattern": "Bearer\\s+[a-zA-Z0-9\\-._~+/]+=*", "replacement": "Bearer ***REDACTED***"},
    {"name": "aws_keys", "pattern": "AKIA[0-9A-Z]{16}", "replacement": "***AWS_KEY_REDACTED***"},
    {"name": "env_secrets", "pattern": "(?i)(password|secret|token|private_key)\\s*[=:]\\s*['\"]?([^\\s'\"]{8,})", "replacement": "$1=***REDACTED***"},
    {"name": "connection_strings", "pattern": "(?i)(mongodb|postgres|mysql|redis)://[^\\s]+", "replacement": "***CONNECTION_STRING_REDACTED***"}
  ]
}
```

### 4.7 Claude Code Hook Integration

SessionVault registers a `SessionEnd` hook (or post-session hook via Claude Code's hook system):

```json
// ~/.claude/settings.json (additions)
{
  "hooks": {
    "PostToolUse": [],
    "SessionEnd": [
      {
        "command": "sv backup --auto",
        "description": "Auto-backup session to SessionVault"
      }
    ]
  }
}
```

---

## 5. CLI Reference (Planned)

```
sv - SessionVault: AI Session Memory for Developers

USAGE:
    sv <command> [options]

COMMANDS:
    init            Initialize a new vault
    backup          Back up sessions to the vault
    search          Search across all sessions
    list            List sessions with filters
    view            View a session (terminal or browser)
    browse          Open vault browser (HTML)
    tag             Add tags to a session
    untag           Remove tags from a session
    tags            List all tags
    status          Show vault status (staged/unstaged)
    review          Review staged changes
    commit          Commit staged sessions to vault
    push            Push vault to remote git
    summary         Generate session summaries
    export          Export sessions (md/html)
    config          View/edit vault configuration
    hook            Install/manage Claude Code hooks
    redact          Run redaction on existing sessions
    stats           Show vault statistics

GLOBAL OPTIONS:
    --vault PATH    Use a specific vault (default: ~/.sessionvault)
    --verbose       Verbose output
    --help          Show help
    --version       Show version
```

---

## 6. Product Roadmap

### Phase 1: Foundation (v0.1) - "It Works"
**Goal:** Core backup and search functionality

- [ ] `sv init` - Create vault, initialize git repo, default config
- [ ] `sv backup` - Parse JSONL, extract metadata, redact, store
- [ ] `sv search` - Full-text search with SQLite FTS5
- [ ] `sv list` - List sessions with date/project filters
- [ ] `sv view` - Render session as readable terminal output
- [ ] `sv status` / `sv commit` - Git staging workflow
- [ ] `sv config` - Basic configuration
- [ ] Redaction engine with default rules
- [ ] Metadata extraction (project, branch, date, tools, files)

### Phase 2: Polish (v0.2) - "It's Nice"
**Goal:** Improved UX and automation

- [ ] `sv browse` - Local HTML viewer with search
- [ ] `sv hook install` - Auto-install Claude Code SessionEnd hook
- [ ] `sv tag` / `sv untag` / `sv tags` - Tagging system
- [ ] `sv export` - Markdown and standalone HTML export
- [ ] `sv summary` - Auto-generated session summaries
- [ ] `sv stats` - Usage statistics and insights
- [ ] Improved terminal output with colors and formatting
- [ ] `.svignore` - Patterns for sessions to skip

### Phase 3: Intelligence (v0.3) - "It's Smart"
**Goal:** AI-enhanced features

- [ ] Semantic search (embedding-based similarity)
- [ ] Auto-tagging based on content analysis
- [ ] Cross-session knowledge graph ("sessions related to this one")
- [ ] Weekly/monthly digest generation
- [ ] "Continue where I left off" - context extraction for new sessions
- [ ] Project-level knowledge base generation

### Phase 4: Multi-Tool (v1.0) - "It's Universal"
**Goal:** Support beyond Claude Code

- [ ] ChatGPT export import (JSON format)
- [ ] Cursor session import
- [ ] Copilot Chat import
- [ ] Aider session import
- [ ] Windsurf session import
- [ ] Unified search across all AI tools
- [ ] Plugin architecture for community-contributed importers

---

## 7. File Structure (Implementation)

```
sessionvault/
  +-- bin/
  |     +-- sv                      # Main CLI entry point (bash)
  +-- lib/
  |     +-- __init__.py
  |     +-- cli.py                  # Argument parsing, command dispatch
  |     +-- backup.py               # JSONL parsing, backup pipeline
  |     +-- search.py               # SQLite FTS5 search engine
  |     +-- index.py                # Index management
  |     +-- redact.py               # Redaction engine
  |     +-- metadata.py             # Metadata extraction from sessions
  |     +-- render.py               # Markdown/HTML rendering
  |     +-- summary.py              # Session summarization
  |     +-- tags.py                 # Tagging system
  |     +-- vault.py                # Vault management (init, config, git)
  |     +-- sources/
  |     |     +-- __init__.py
  |     |     +-- claude_code.py    # Claude Code JSONL parser
  |     +-- templates/
  |           +-- session.md.j2     # Markdown session template
  |           +-- session.html.j2   # HTML session template
  |           +-- browse.html.j2    # Vault browser template
  |           +-- summary.md.j2     # Summary template
  +-- tests/
  |     +-- test_backup.py
  |     +-- test_search.py
  |     +-- test_redact.py
  |     +-- test_metadata.py
  +-- defaults/
  |     +-- redaction-rules.json    # Default redaction patterns
  |     +-- vault-config.json       # Default vault configuration
  +-- setup.py                      # pip installable
  +-- pyproject.toml
  +-- README.md
  +-- LICENSE                       # MIT
```

---

## 8. Configuration

### Vault Configuration (`vault.json`)

```json
{
  "version": 1,
  "vault_path": "~/.sessionvault",
  "sources": {
    "claude_code": {
      "enabled": true,
      "session_dir": "~/.claude/projects",
      "metadata_dir": "~/.claude/sessions"
    }
  },
  "backup": {
    "auto_backup": true,
    "auto_commit": false,
    "staging_review": true,
    "retention_days": null
  },
  "redaction": {
    "enabled": true,
    "rules_file": ".redaction-rules.json",
    "strict_mode": false
  },
  "search": {
    "index_assistant_messages": true,
    "index_tool_results": false,
    "max_results": 20
  },
  "rendering": {
    "theme": "dark",
    "syntax_highlight": true,
    "browser_port": 8484
  },
  "git": {
    "auto_push": false,
    "remote": null,
    "branch": "main"
  }
}
```

---

## 9. Security & Privacy Considerations

1. **Local-first**: All data stays on your machine by default. No cloud, no telemetry.
2. **Redaction before storage**: Sensitive patterns are stripped before files are written to the vault.
3. **Private git repo**: If pushing to a remote, the tool warns if the remote repo is public.
4. **No raw JSONL in git by default**: Only redacted copies are stored (configurable).
5. **`.gitignore` defaults**: Vault ignores temp files, partial backups, index DB (regenerable).
6. **Audit trail**: Every backup operation is logged so you can verify what was stored.

---

## 10. Heuristics & Estimates

### Session Volume (Full-Time AI-Assisted Engineer)

| Metric | Estimate |
|--------|----------|
| Sessions per day | 4-8 |
| Sessions per week | 20-40 |
| Sessions per month | 80-160 |
| Sessions per year | 1,000-2,000 |
| Avg session size (JSONL) | 50-500 KB |
| Vault size after 1 year | 500 MB - 2 GB |
| Index size (SQLite) | 50-200 MB |

### Performance Targets

| Operation | Target |
|-----------|--------|
| `sv backup` (single session) | < 2 seconds |
| `sv search` (full-text) | < 500ms |
| `sv list` | < 200ms |
| `sv view` (terminal render) | < 1 second |
| `sv browse` (HTML startup) | < 3 seconds |
| Full reindex (1000 sessions) | < 60 seconds |

---

## 11. Open Questions & Future Considerations

1. **Session deduplication**: What if a session is backed up twice? Use session UUID as dedupe key.
2. **Incremental backup**: For long-running sessions, should we support appending to an existing backup?
3. **Multi-machine sync**: If vault is on GitHub, how to handle merges from multiple machines? (Git handles this naturally.)
4. **Team vaults**: Shared vault for a team? Requires consent + stricter redaction.
5. **LLM-powered search (v0.3)**: Use Claude API to enable natural language queries like "find where I debugged that memory leak in the payment service."
6. **Session replay**: Could we "replay" a session step by step? Useful for learning/review.
7. **Integration with CLAUDE.md**: Auto-generate project memory files from session history.

---

## 12. Success Metrics

- **Adoption**: Engineer installs and uses it for 30+ days
- **Retrieval**: Developer finds a past session within 10 seconds
- **Coverage**: 95%+ of sessions are automatically backed up
- **Trust**: Zero incidents of sensitive data leaking through the vault
- **Scale**: Handles 2,000+ sessions without performance degradation

---

## 13. Getting Started (Installation - Planned)

```bash
# Install via pip
pip install sessionvault

# Or clone and install
git clone https://github.com/shoaibrain/sessionvault.git
cd sessionvault
pip install -e .

# Initialize vault
sv init

# Install Claude Code hook for auto-backup
sv hook install

# Back up existing sessions
sv backup --all

# Search your history
sv search "rate limiter"

# Browse sessions
sv browse
```

---

*SessionVault - Because your AI conversations are worth remembering.*
