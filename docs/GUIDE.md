# Vimgym — User Guide

> AI session memory for developers. Local. Fast. No cloud.

This guide is for developers who use Claude Code regularly and want to be able
to find any past conversation in under a second. If you want internals, see
[DEVELOPER.md](DEVELOPER.md) instead.

---

## What Is Vimgym?

You've spent a year building a project with Claude Code. You've had hundreds
of sessions with it. You wrote the auth flow with Claude in February. You
hardened the CORS config in April. Today you need to remember the *reasoning*
behind one of those decisions — but the chat is gone. Claude Code's session
list shows recent conversations only, and there's no search.

Vimgym is the missing layer. It runs as a tiny daemon on your machine,
watches `~/.claude/projects/`, and indexes every JSONL file Claude Code
writes. You get:

- **Full-text search** across every prompt, response, tool use, and code edit
- **A three-pane browser UI** at `http://127.0.0.1:7337` — Cmd+K to find anything
- **A terminal command** `vg search "query"` for quick lookups
- **Markdown export** of any session, ready to paste back into Claude Code as
  context for a follow-up conversation
- **18 redaction patterns** that strip API keys, AWS credentials, JWTs, SSH
  keys, kubeconfig certs, and more before anything is written to disk
- **Zero cloud** — your sessions never leave your machine

It's `git log` for your AI conversations.

---

## Installation

There are four supported install paths. Pick one. After install, run
`vg doctor` to verify everything is healthy.

### 1. Homebrew (recommended for macOS)

```bash
brew tap shoaibrain/vimgym
brew install vimgym
```

This installs `vg` to `/opt/homebrew/bin/vg` (Apple Silicon) or
`/usr/local/bin/vg` (Intel) — always on `$PATH`, no shell activation
required, survives shell restart and reboot. To run as a background
service that auto-starts on login:

```bash
brew services start vimgym
```

### 2. pipx (any OS, recommended for Linux)

```bash
pipx install vimgym
```

`pipx` installs vimgym into an isolated virtualenv and symlinks `vg` into
a PATH directory. Equally permanent as Homebrew, no shell activation needed.

### 3. curl one-liner (auto-detects the best method)

```bash
curl -fsSL https://vimgym.xyz/install | sh
```

The installer prefers Homebrew, then pipx, then `pip --user`. If it falls
back to `pip --user` and your PATH does not contain the user bin directory,
the installer prints the exact line to add to your `~/.zshrc` — it never
edits your shell config silently.

### 4. From source (development install)

```bash
git clone https://github.com/shoaibrain/vimgym.git
cd vimgym
make install
source .venv/bin/activate
```

This is for contributors. The resulting `vg` lives inside `.venv/bin/`,
so it only works in shells where the venv is active. If you see a
`⚠ vg is running from a project virtualenv` warning, that's expected
for source installs.

### Verify

```bash
vg --version
vg doctor
```

`vg doctor` runs a full system check (Python version, SQLite FTS5,
vault permissions, configured sources, redaction rules) and exits
non-zero if anything is wrong.

**Requirements:** macOS or Linux, Python 3.11/3.12/3.13. Windows is not
yet supported.

---

## Quick Start (5 minutes)

```bash
$ vg init
✓ vault initialized /Users/you/.vimgym

Detected sources:
  ✓  Claude Code          ~/.claude/projects                  [enabled]
  ⊘  Cursor               ~/.cursor                            parser coming in v2
  ⊘  GitHub Copilot       ~/.copilot                           parser coming in v2
  ⊘  Antigravity          ~/.antigravity                       parser coming in v2
  ⊘  Gemini CLI           ~/.gemini                            parser coming in v2

Next: vg start
```

```bash
$ vg start
vimgym started (pid 12345) on http://127.0.0.1:7337
  vault:    /Users/you/.vimgym
  watching: /Users/you/.claude/projects  (claude_code)
```

Your browser opens to `http://127.0.0.1:7337`. The first time vimgym sees a
populated `~/.claude/projects/`, it backfills every existing session — that
usually takes a few seconds, then everything is searchable.

```bash
$ vg search "CORS"
                          Results for 'CORS'
┏━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓
┃ DATE       ┃ ID       ┃ PROJECT ┃ BRANCH  ┃   DUR ┃ TITLE                 ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩
│ 2026-04-05 │ 3438c55b │ edforge │ dev     │  320m │ Implement env-driven  │
│            │          │         │         │       │ CORS and domain config│
└────────────┴──────────┴─────────┴─────────┴───────┴───────────────────────┘
```

In the browser, press **⌘K** and start typing. Match terms light up in pink.
Press **Enter** on a result to read the full conversation.

That's the whole tool. The rest of this guide explains what each piece does.

---

## The Daemon

`vg` is the CLI. The daemon is a background Python process that runs the
filesystem watcher and the local web server. It's designed to be on
all the time.

### Lifecycle commands

```bash
vg start   # spawn the daemon, open browser
vg stop    # graceful SIGTERM, falls back to SIGKILL after 5s
vg status  # is it running? how many sessions? db size?
vg open    # open the browser UI (only if daemon is running)
```

`vg start` is idempotent — calling it when the daemon is already running just
prints the current URL and exits 0. The PID file at `~/.vimgym/vimgym.pid`
is automatically cleaned up if the daemon crashes (stale-PID detection).

### What the daemon does

1. Loads `~/.vimgym/config.json`.
2. Initializes (or upgrades) the SQLite vault at `~/.vimgym/vault.db`.
3. **Backfill**: walks every enabled source path and indexes anything not
   already in the vault. New install picks up your entire history in seconds.
4. **Watch**: starts a watchdog observer on each enabled source. New JSONL
   files are debounced for 5 seconds (Claude Code writes in bursts), checked
   for size stability, then parsed and indexed.
5. **Serve**: launches the FastAPI server on `127.0.0.1:7337`. The browser UI,
   the REST API, and the WebSocket live-update channel all live here.

The daemon is intentionally a single process. One PID, one log file at
`~/.vimgym/logs/vimgym.log`, one DB. If something looks wrong, that's where
to start looking.

### Auto-start on login (Homebrew)

```bash
brew services start vimgym
```

This installs a launchd plist that boots the daemon at login and restarts it
if it crashes. Use `brew services stop vimgym` to undo.

---

## The Browser UI

Open `http://127.0.0.1:7337` (or run `vg open`).

```
┌─────────────────────────────────────────────────────────────┐
│ vimgym  ◉   [search sessions...  ⌘K]      ● watching   ⚙  │
├──────────────┬────────────────────┬─────────────────────────┤
│  PROJECTS    │  SESSIONS          │  Session Detail         │
│  ◆ All       │  ┌──────────────┐ │  edforge / dev          │
│  ◈ edforge 6 │  │ edforge      │ │  Implement env-driven   │
│  ◈ vimgym  1 │  │ Implement... │ │  CORS and domain config │
│              │  │ ⎇ dev  5h20m │ │                         │
│  BRANCHES    │  └──────────────┘ │  [5h20m] [470 messages] │
│  ⎇ dev     5 │  ┌──────────────┐ │  [⬡ subagents] [...]    │
│  ⎇ fee-... 2 │  │ edforge      │ │                         │
│              │  │ Audit ...    │ │  USER  16:28:49         │
│  ACTIVITY    │  └──────────────┘ │  I need all CORS...     │
│  ▢▢▣▢▢▢▢   │  …                 │                         │
│              │                    │  CLAUDE  16:28:52       │
│  VAULT STATS │                    │  I'll start with...     │
│  sessions  7 │                    │  ▶ Agent  Audit ...     │
│  ai time 32h │                    │  ▶ Bash   Check CDK     │
│              │                    │                         │
│  TOOLS USED  │                    │       [Export Markdown] │
├──────────────┴────────────────────┴─────────────────────────┤
│ NORMAL │ ⬡ vimgym │ edforge / dev │ 7 sessions │ FTS5 ✓    │
└─────────────────────────────────────────────────────────────┘
```

### The three panes

**Sidebar (left, 220px)** — navigation only, no content.
- **Projects**: every project that's been seen, with session counts. Click to filter the inbox.
- **Branches**: every git branch observed, derived from session metadata.
- **Activity**: 26 × 7 cell heatmap of the last 182 days. Hover to see counts; click a day to filter.
- **Vault Stats**: total sessions, AI time, DB size, output tokens.
- **Tools Used**: every Claude Code tool that's appeared in your sessions, color-coded by tool family.

**Inbox (middle, 300px)** — the session list.
- One card per session.
- Project name in cyan, relative timestamp in dim gray.
- Title (Claude Code's auto-generated `ai-title`), truncated to 2 lines.
- Branch chip, duration chip, "⬡ subagents" pink badge if applicable, top tool chips.
- Click a card to open the session.
- Scroll to the bottom to load more (50 at a time, infinite).
- New sessions appear at the top with a green-flash animation, plus a toast bottom-right.

**Detail (right, flex)** — the conversation reader.
- Breadcrumb (project / branch · short uuid).
- Title.
- Meta chips: duration, message count, subagent count, model, slug, date.
- Tool chips: every tool used in this specific session.
- Messages, in order:
  - User messages: cyan left border, `USER` badge.
  - Assistant messages: purple left border, `CLAUDE` badge.
  - Tool use blocks: collapsible `<details>` with the tool name and a short
    description; expand to see the full input as syntax-highlighted JSON.
  - Tool results: same collapsible style, shows the tool output (truncated to 8KB).
  - Code blocks: syntax-highlighted by highlight.js, with a hover-revealed `copy` button.
  - Image blocks: shown as `[image omitted — not stored]` (vimgym does not
    store base64 image data — see [Privacy](#privacy--security)).
  - Thinking blocks: shown as a collapsible `[thinking — omitted]` block.
- Footer: **Export Markdown** button.

### Command palette (⌘K)

The command palette is the fastest way to find anything.

| Key | Action |
|---|---|
| `⌘K` (or `Ctrl+K`) | Open the palette |
| Type | Search runs after 200ms of inactivity |
| `↑` / `↓` | Move selection between results |
| `Enter` | Open the highlighted result in the detail pane |
| `Esc` | Close the palette |

Match terms in result titles and snippets are highlighted in pink. The
palette searches across all projects — there's no per-project filter inside it
(use `vg search --project X` for that).

### Settings panel

Click the **⚙** icon in the top-right of the topbar. The detail pane is
replaced with a settings view showing:

- **Sources** — every detected AI tool with `enabled` toggles. Toggling
  persists to `~/.vimgym/config.json` immediately, but takes effect on the
  next `vg start` (the running watcher does not hot-reload).
- **Vault** — vault path, db size, session count, total duration.
- **Server** — host (127.0.0.1, fixed for security), port, log level, debounce.
- **Redaction** — status (always active, 18 patterns).

To get back to a session, click any inbox card.

### Statusbar (Vim Airline replica)

The bottom strip is the constant orientation marker:

```
[NORMAL] ▶ ⬡ vimgym  edforge / dev  3438c55b  ⋯  7 sessions  watching ~/.claude/projects  FTS5 ✓
```

- **Mode badge** (left, on dark background): `NORMAL` / `SEARCH` / `READ` —
  flips automatically based on what's open.
- **Context segments**: project / branch, short session UUID.
- **Right side**: total sessions, watch path, FTS5 health.

### Export

The **Export Markdown** button downloads a `.md` file of the current session.
The output is paste-friendly: title, full metadata, every message, every
tool call, every diff. Use it to resume a stalled conversation in a fresh
Claude Code session — copy the markdown into a new prompt with "Continue from
where we left off" and Claude Code picks up the thread.

---

## Searching

### From the terminal

```bash
vg search QUERY [flags]
```

**Flags:**

| Flag | Effect |
|---|---|
| `--project NAME` | Restrict to one project |
| `--branch NAME` | Restrict to one git branch |
| `--since DATE` | Either ISO date (`2026-03-01`) or shorthand (`7d`, `30d`) |
| `--limit N` | Max results (default 20) |
| `--json` | Print JSON to stdout instead of a rich table |

**Examples:**

```bash
vg search "CORS configuration"
vg search "auth" --project edforge --since 7d
vg search "rate limiter" --branch dev --limit 5
vg search "docker" --json | jq '.[0].ai_title'
vg search "fee-to-enrollment"        # hyphens just work
```

`vg search` runs against the daemon's HTTP API when the daemon is up, and
falls back to a direct SQLite query when it isn't — so you can search even
when the daemon is stopped.

### From the browser

Press **⌘K**. Type. The search runs after 200ms of inactivity (debounced) and
hits `/api/search?q=…&limit=10`. Results show title, project, branch,
duration, date, and a snippet with match terms highlighted.

### Search syntax

Vimgym uses SQLite's FTS5 with BM25 ranking. The query is auto-escaped, so:

- **Multiple words are AND** — `auth login` finds sessions that mention both.
- **Hyphens, slashes, and colons just work** — `fee-to-enrollment`,
  `server/lib/auth.ts`, and `Bearer:` are all valid queries. They're
  internally quoted as literal phrases so FTS5 doesn't interpret them as
  syntax.
- **No prefix wildcards yet** — `auth*` won't expand. Use the full word.
- **Phrase queries**: every token already becomes a phrase. To search for an
  exact multi-word phrase, just type the words; they're treated as adjacent.

The search index covers: project name, branch, AI title, summary, all user
text, all assistant text, tool names, and modified file paths. **Thinking
blocks are not indexed** — they're internal model reasoning, not user content.

---

## Configuration

### `~/.vimgym/config.json`

The config file is created on first `vg init`. Edit it directly with any text
editor, then `vg stop && vg start` to apply changes.

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

**Common knobs:**

- `server_port` — change if 7337 conflicts with something
- `auto_open_browser` — set to `false` if you don't want a browser tab on `vg start`
- `debounce_secs` — increase if Claude Code writes are getting batched too aggressively
- `log_level` — `DEBUG` for verbose logs in `~/.vimgym/logs/vimgym.log`

### Sources

A "source" is a watched directory belonging to one AI tool.

```bash
vg config sources                     # show all sources, status, parser availability
vg config sources claude_code --enable
vg config sources cursor --disable
```

In v1, only `claude_code` has a parser. The other sources (Cursor, Copilot,
Antigravity, Gemini) are auto-detected so they show up in the table, but
disabled by default. When their parsers ship in v2, you'll just toggle them on.

### Adding a custom watch path

If your Claude Code data lives somewhere non-standard:

```bash
# 1. Find the current path
vg config sources

# 2. Edit the JSON directly
$EDITOR ~/.vimgym/config.json
# change the "path" field of the claude_code source

# 3. Restart
vg stop && vg start
```

### Dev mode (override the watch path)

For testing or for pointing vimgym at an arbitrary directory of JSONL files:

```bash
VIMGYM_WATCH_PATH=./fixtures vg start
```

This replaces `sources[]` for that single run with one source called
`env_override`. Your on-disk config is not modified — restart without the env
var and you're back to normal.

---

## Redaction

Vimgym ships with **18 secret-detection patterns** that strip credentials from
session content **before anything is written to the vault**. The original
files in `~/.claude/projects/` are never touched.

Coverage:

- **API keys**: Anthropic (`sk-ant-…`), OpenAI (`sk-…`), GitHub (`ghp_…`)
- **AWS**: access keys (`AKIA…`), secrets, session tokens (`AQoXb…`), ARNs
- **Cloud-native**: kubeconfig certificates, k8s tokens, Docker registry auth, npm tokens
- **Private keys**: PEM blocks (RSA / EC / OPENSSH / DSA), inline private keys
- **Web auth**: Bearer tokens, JWTs (`eyJ…`)
- **Database URLs**: `postgresql://`, `postgres://`, `mongodb://`, `mysql://`, `redis://` (the password segment is replaced specifically for postgres URIs)
- **Generic**: any `password=`, `secret=`, `api_key=`, `private_key=` assignment with 8+ characters

Custom patterns can be added by placing a `redaction-rules.json` file at
`~/.vimgym/redaction-rules.json` — see the format in the [DEVELOPER
docs](DEVELOPER.md#redaction-rules-reference).

---

## Privacy & Security

- **All data stays local.** The vault is a single SQLite file at
  `~/.vimgym/vault.db`. Nothing is uploaded anywhere.
- **The server binds 127.0.0.1 only.** It is not reachable from the network,
  even on the same Wi-Fi.
- **The vault file is `chmod 600`.** Owner read/write only; other users on the
  machine can't read it.
- **Redaction runs before storage.** 18 patterns scrub secrets from the
  in-memory copy before insertion.
- **Image base64 data is not stored.** Multi-megabyte image attachments are
  replaced with `{type: "image", omitted: true}` markers.
- **Thinking blocks are not indexed.** Claude's internal reasoning blocks are
  preserved in the message structure (so the UI can show them as collapsed
  placeholders) but never appended to the FTS text columns.
- **No telemetry.** Vimgym makes zero outbound network calls. The only network
  activity is the daemon's filesystem watcher (which is local).

---

## Troubleshooting

### Anything looks wrong → `vg doctor`

Always start here:

```bash
vg doctor
```

It checks Python, SQLite FTS5, vault permissions, the daemon state,
every configured source, and the redaction rules. If a green ✓ becomes
a red ✗, the line right after it tells you how to fix it.

### `vg start` fails immediately

Check Python and FTS5:

```bash
python3 --version    # must be 3.11 or newer
python3 -c "import sqlite3; conn=sqlite3.connect(':memory:'); conn.execute('CREATE VIRTUAL TABLE t USING fts5(x)')"
# silent = ok; OperationalError = your Python was built without FTS5
```

If FTS5 is missing on macOS, reinstall Python via Homebrew (`brew install
python@3.12` ships with FTS5).

Look at the daemon log:

```bash
tail -50 ~/.vimgym/logs/vimgym.log
```

The first few lines after a failed start usually identify the problem
(permissions, port conflict, malformed config).

### Sessions aren't appearing

```bash
vg status
```

Check that the daemon is running and that **watching** points at the right
path. Then verify Claude Code actually has sessions there:

```bash
ls ~/.claude/projects/
```

If the directory is populated but vimgym shows zero sessions, the backfill
failed. Check the log:

```bash
grep backfill ~/.vimgym/logs/vimgym.log
```

### Search returns no results

```bash
vg status                  # session count > 0?
vg search "test"           # any results at all?
sqlite3 ~/.vimgym/vault.db "SELECT COUNT(*) FROM sessions"
```

If `vault.db` has rows but search returns nothing for a term you know is
present, the FTS index may have drifted. Rebuild it:

```bash
vg stop
sqlite3 ~/.vimgym/vault.db "INSERT INTO sessions_fts(sessions_fts) VALUES('rebuild')"
vg start
```

### Browser doesn't open

```bash
vg open                                       # explicitly open browser
# or
open http://127.0.0.1:7337                    # macOS
```

You can also disable auto-open in `~/.vimgym/config.json`
(`"auto_open_browser": false`) if you want to be the one to decide.

### Port conflict

Edit `~/.vimgym/config.json`:

```json
{ "server_port": 7338 }
```

Then `vg stop && vg start`. Or use the env override for one run:

```bash
VIMGYM_PORT=7338 vg start
```

### Export shows `[REDACTED_*]` placeholders

This is correct. If a secret was in the original session, vimgym scrubbed it
during indexing. The exported markdown shows the redaction marker. If you
need the original, look at the source file in `~/.claude/projects/` directly.

### The watcher missed a session

A few possible causes:

1. **The session is still being written.** Vimgym waits 5 seconds after the
   last write before processing, plus a stability check. A session in active
   use won't appear until you stop typing.
2. **The session UUID was already indexed.** Vimgym dedups by both file hash
   AND session UUID. If a session was indexed earlier and the file has since
   grown, the new content is *not* re-indexed. v1 limitation; see [DEVELOPER
   docs § Known Limitations](DEVELOPER.md#known-limitations-v1).
3. **The file is in a companion subdirectory.** Subagent files in
   `{UUID}/subagents/` and tool result files in `{UUID}/tool-results/` are
   intentionally skipped — they're not session files. The parent session is
   marked `has_subagents=true` instead.

To force a fresh re-index of a session:

```bash
vg stop
sqlite3 ~/.vimgym/vault.db "DELETE FROM sessions WHERE session_uuid LIKE 'abc%'"
vg start    # backfill picks it up again
```

---

## Updating

### Homebrew

```bash
brew upgrade vimgym
```

The vault is preserved at `~/.vimgym/vault.db`. Schema migrations run
automatically on the next `vg start` — the v1 → v2 migration is idempotent
and safe.

### pip

```bash
pip install --upgrade vimgym
```

---

## Uninstalling

```bash
vg stop                          # kill the daemon
brew uninstall vimgym            # or: pip uninstall vimgym
rm -rf ~/.vimgym                 # WARNING: deletes the vault permanently
```

---

## Keyboard Shortcuts (Browser UI)

| Key | Action |
|---|---|
| `⌘K` / `Ctrl+K` | Open command palette |
| `↑` / `↓` | Navigate palette results |
| `Enter` | Open selected session |
| `Esc` | Close palette / clear active session and return to welcome screen |

---

## FAQ

**Q: Does vimgym send my sessions anywhere?**
No. Zero outbound network calls. The server binds 127.0.0.1 and is unreachable
from the network. There is no telemetry, no analytics, no error reporting.

**Q: What happens to sessions I delete in Claude Code?**
Claude Code's deletes don't propagate. Vimgym indexed the file when it first
appeared, and the vault entry stays even if the source file disappears. To
remove a session from vimgym, delete the row directly:
`sqlite3 ~/.vimgym/vault.db "DELETE FROM sessions WHERE session_uuid LIKE 'prefix%'"`.

**Q: Can I use vimgym with Cursor / Copilot / Gemini?**
Vimgym auto-detects all four (Cursor, Copilot, Antigravity, Gemini CLI) and
shows them in `vg config sources`, but their parsers aren't written yet.
Only Claude Code is fully supported in v1. The architecture is multi-source
ready — when parsers ship, you'll just toggle them on.

**Q: How do I back up my vault?**
The vault is a single SQLite file. Copy `~/.vimgym/vault.db` anywhere. Rsync
it. Put it in iCloud. There's nothing else to back up.

**Q: How do I sync my vault across machines?**
There's no built-in sync. The vault is local-first by design. If you want
cross-machine search, the supported approach is to back up `vault.db` to a
shared location (Dropbox, iCloud, S3) and let your other machines read it.
Vimgym does not write to the vault from multiple machines simultaneously —
WAL mode handles single-host concurrency, not multi-host.

**Q: Can I search across multiple projects at once?**
Yes. Search runs across the entire vault by default — just type the query.
Use `--project NAME` from the terminal or the sidebar project filter in the
browser to scope down to one.

**Q: Does vimgym slow down Claude Code?**
No. Vimgym only reads files; it never writes to `~/.claude/projects/`. The
filesystem watcher is event-driven (kqueue/FSEvents on macOS) so it has near-
zero polling overhead. The processing pipeline runs on a separate thread so
it can't block anything Claude Code is doing.

**Q: Why is the matrix rain so subtle?**
4% opacity. Loud enough to be there, quiet enough not to distract. There's no
toggle in v1 — if you really hate it, comment out the `<canvas>` line in
`src/vimgym/ui/index.html`.

**Q: Can I write my own search adapter for an LLM?**
Not yet. The vault is a regular SQLite file with FTS5 — you can absolutely
write a script that connects to it and feeds results into a local LLM. There
just isn't an integration in v1.
