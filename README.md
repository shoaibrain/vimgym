# vimgym

[![PyPI version](https://img.shields.io/pypi/v/vimgym.svg)](https://pypi.org/project/vimgym/)
[![Python versions](https://img.shields.io/pypi/pyversions/vimgym.svg)](https://pypi.org/project/vimgym/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/shoaibrain/vimgym/actions/workflows/ci.yml/badge.svg)](https://github.com/shoaibrain/vimgym/actions/workflows/ci.yml)

> **Status:** v0.2.0 Beta revival. The project was previously marked inactive after
> v0.1.1; v0.2 restores active development around capture correctness, privacy,
> migration, and portable recovery.

Vimgym is local session memory for Claude Code and Codex. It continuously turns
provider session files into one redacted, provider-neutral SQLite/FTS5 vault for
search, browsing, export, diagnostics, and verified backup/restore. It has no
hosted service, account, telemetry, or model call.

```text
~/.claude/projects/**/*.jsonl       $CODEX_HOME/sessions/**/*.jsonl
Claude subagents + text sidecars    $CODEX_HOME/archived_sessions/**/*.jsonl
                 \                         /
                  └── read-only adapters ─┘
                              │
                    redact before staging
                              │
                     SQLite v2 + block FTS5
                              │
                    loopback-only HTTP
```

## What v0.2 captures

Vimgym stores redacted user-visible text, tool calls/results, visible provider
summaries, file activity, metadata, lifecycle, and parent/child lineage. Claude
root sessions, actual subagent JSONL, referenced text tool-result sidecars, and
every supported Codex active/archived session are included. Codex sessions are
classified as user tasks, automations, or subagents.

Binary attachments and images retain metadata only. Hidden or encrypted
reasoning, provider telemetry, world state, inter-agent transport, and native
unredacted JSONL are not copied into the vault. Codex memories, logs, goals,
state/auth databases, and import maps are never scanned.

## Install

The released package remains available from PyPI and Homebrew. For Homebrew 6,
use the fully qualified formula name:

```bash
# macOS
brew install shoaibrain/vimgym/vimgym

# macOS or Linux
pipx install vimgym
```

Then initialize and start the loopback-only daemon:

```bash
vg init
vg doctor
vg start
```

Vimgym targets macOS and Linux with Python 3.11–3.14. A v0.2 release is made
only after the complete OS/Python matrix passes.

## Search, reindex, and diagnostics

```bash
vg search "revision-aware capture"
vg search "restore" --provider codex --session-type subagent
vg search "migration" --lifecycle archived --json

vg reindex --provider claude_code
vg reindex --session SESSION_ID_OR_PREFIX

vg doctor
vg doctor --json
```

The browser keeps the existing three-pane identity while adding provider,
session-kind, lifecycle, lineage, and parser-health context. Search snippets are
structured text parts rather than HTML, transcripts are paginated, and dynamic
provider content is rendered with DOM text nodes.

## Portable backup and restore

```bash
vg backup create /Volumes/Backups
vg backup verify /Volumes/Backups/vimgym-20260101T120000Z-v2.vgbak
vg backup restore backup.vgbak --to ~/.vimgym-restored
vg backup restore backup.vgbak --to ~/.vimgym --replace
```

A `.vgbak` is an owner-only ZIP containing a consistent SQLite online snapshot,
configuration, optional custom redaction rules, and a manifest of sizes and
SHA-256 hashes. Restore verifies paths, hashes, schema, and SQLite integrity
before touching the destination. Replacement requires the daemon to be stopped,
explicit `--replace`, and a rollback backup.

Backups are unencrypted and remain sensitive. Redaction is credential scrubbing,
not anonymization.

## Upgrade from v0.1.1

The daemon takes an owner-only SQLite rollback snapshot, migrates to schema v2,
re-redacts legacy content, rebuilds block-level FTS, validates counts and SQLite
integrity, and only then starts capture and HTTP serving. Source-backed sessions
are marked for current-parser reconciliation; sessions whose original files are
missing remain searchable.

The v0.1 provider-native `/raw` endpoint is removed. Use streamed Markdown or
canonical JSONL export instead. Existing Claude UUID prefixes remain accepted
during v0.2; ambiguous prefixes return HTTP 409.

## Privacy and local security

- Redaction is recursive and runs before staging, FTS, persistence, diagnostics,
  APIs, WebSockets, export, and portable backup.
- An invalid or empty redaction policy fails closed; its deterministic hash
  forces reindex when rules change.
- The server accepts only `127.0.0.1`, `localhost`, or `::1`, validates Host and
  WebSocket Origin, and ships a restrictive self-only Content Security Policy.
- UI assets and fonts are local. Vimgym makes no outbound network request.
- Provider files are always read-only.

## Documentation

- [v0.2 architecture and product contract](docs/V0.2-SPEC.md)
- [User guide](docs/GUIDE.md)
- [Developer reference](docs/DEVELOPER.md)
- [Release process](RELEASE.md)

## Scope

v0.2 deliberately does not add cloud sync, S3, accounts, team collaboration,
telemetry, backup encryption, vector search, embeddings, generated summaries,
tags, cost dashboards, Windows support, provider writeback, or a public plugin
SDK. Model-assisted intelligence begins only after capture, migration, redaction,
backup, and provider normalization have proven release evidence.

## License

MIT. See [LICENSE](LICENSE).
