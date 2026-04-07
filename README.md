# vimgym

> AI session memory for developers. Local. Fast. No cloud.

Vimgym automatically captures every Claude Code session and makes your entire
AI conversation history searchable in under 500ms. It's `git log` for your
AI conversations.

```
                  ┌──────────────────────────┐
                  │   ~/.claude/projects/    │
                  └────────────┬─────────────┘
                               │ filesystem watcher
                               ▼
            ┌─────────────────────────────────┐
            │  vimgym daemon (single process) │
            │  parser → redact → SQLite + FTS5│
            └────────────┬────────────────────┘
                         │
                         ▼
            ┌────────────────────────────────────┐
            │  http://127.0.0.1:7337             │
            │  ⌘K  to search · live updates · WS │
            └────────────────────────────────────┘
```

## Install

```bash
brew install vimgym
# or
curl -fsSL https://vimgym.xyz/install | sh
# or
pip install --user vimgym
```

## Quick start

```bash
vg init     # detect AI tool sources, create vault
vg start    # spawn daemon, open browser
```

In the browser, press **⌘K** and start typing. Or from the terminal:

```bash
vg search "CORS configuration"
vg search "auth" --project edforge --since 7d
vg search "rate limiter" --branch dev --json | jq
```

## Features

- **Automatic capture** — filesystem watcher catches every new session within seconds; zero configuration
- **Full-text search** — SQLite FTS5 with BM25 ranking, sub-500ms on any vault size, hyphen-safe queries
- **Three-pane web UI** — Neon Void design, command palette (⌘K), live updates via WebSocket
- **Session detail** — full conversation rendering with syntax-highlighted code, collapsible tool blocks, copy buttons
- **Markdown export** — one click to get a paste-friendly transcript for resuming a session in Claude Code
- **18-pattern redaction** — strips API keys, AWS credentials, kubeconfig certs, SSH keys, JWT tokens, and more *before* anything is written
- **Source-aware** — auto-detects Claude Code, Cursor, Copilot, Antigravity, Gemini (only Claude Code parser ships in v1)
- **Local-first** — server binds 127.0.0.1, vault file is `chmod 600`, zero outbound network calls

## CLI

```
vg start                       Start daemon (watcher + web server), open browser
vg stop                        Graceful shutdown
vg status                      Daemon health, vault stats, source list
vg open                        Open browser if daemon is running
vg search QUERY [flags]        Terminal search with --project --branch --since --limit --json
vg init                        Initialize vault, detect AI tool sources
vg config                      Print active configuration
vg config sources              List configured sources
vg config sources ID --enable  Enable a source (takes effect on next vg start)
vg config sources ID --disable Disable a source
```

## Documentation

- **[User Guide](docs/GUIDE.md)** — installation, UI walkthrough, search syntax, configuration, troubleshooting
- **[Developer Reference](docs/DEVELOPER.md)** — architecture, module reference, schema, API, source adapter interface

## Status

v0.1.0 — first working release. 117 tests passing. macOS supported. Linux works
but isn't yet covered by CI. Windows and additional source parsers (Cursor,
Copilot, Gemini) are v2.

## Requirements

- macOS (v1)
- Python 3.11+
- Claude Code

## License

MIT. See [LICENSE](LICENSE).
