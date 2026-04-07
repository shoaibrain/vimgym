# vimgym

[![PyPI version](https://img.shields.io/pypi/v/vimgym.svg)](https://pypi.org/project/vimgym/)
[![Python versions](https://img.shields.io/pypi/pyversions/vimgym.svg)](https://pypi.org/project/vimgym/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/shoaibrain/vimgym/actions/workflows/ci.yml/badge.svg)](https://github.com/shoaibrain/vimgym/actions/workflows/ci.yml)

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
# macOS — recommended
brew tap shoaibrain/vimgym
brew install vimgym

# Any OS
pipx install vimgym

# One-liner (auto-detects best method)
curl -fsSL https://vimgym.xyz/install | sh
```

After install, run `vg doctor` to verify everything is healthy.

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
vg start [--no-browser]        Start daemon (watcher + web server), open browser
vg stop                        Graceful shutdown
vg status                      Daemon health, vault stats, source list
vg open                        Open browser if daemon is running
vg doctor                      Run system diagnostics (Python, FTS5, vault, sources, redaction)
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

v0.1.1 — first official release. 117 tests passing on Python 3.11 / 3.12 / 3.13,
Linux + macOS. Windows and additional source parsers (Cursor, Copilot, Gemini)
are v2.

## Requirements

- macOS or Linux
- Python 3.11+
- Claude Code

## Contributing

```bash
git clone https://github.com/shoaibrain/vimgym.git
cd vimgym
make install        # creates .venv, installs in editable mode with [dev] extras
source .venv/bin/activate
make test           # 117 tests, ~40 seconds
```

See [docs/DEVELOPER.md](docs/DEVELOPER.md) for the architecture overview.

## License

MIT. See [LICENSE](LICENSE).
