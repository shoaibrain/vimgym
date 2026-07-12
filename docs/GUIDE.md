# Vimgym v0.2 user guide

Vimgym keeps a local, redacted, searchable copy of Claude Code and Codex
sessions. It reads provider files but never changes them, makes no model/API
call, and serves the browser only on loopback.

## Install and initialize

```bash
# macOS / Homebrew 6+
brew install shoaibrain/vimgym/vimgym

# macOS or Linux
pipx install vimgym

vg init
vg doctor
vg start
```

`vg init` discovers `~/.claude/projects`, `$CODEX_HOME/sessions`, and
`$CODEX_HOME/archived_sessions` (with `CODEX_HOME=~/.codex` by default). Only
existing roots are added. Review them with:

```bash
vg config sources
vg config sources codex_archived --disable
```

Configuration lives in `~/.vimgym/config.json` unless `VIMGYM_PATH` selects a
different vault. The HTTP host must remain `127.0.0.1`, `localhost`, or `::1`.

## Capture behavior

On every start Vimgym reconciles all configured files, then watches them through
one bounded queue. A changed artifact is processed after the default five-second
stability debounce. Appends, rewrites, truncation, parser upgrades, and policy
changes replace one atomic revision of the same session. A partial final JSONL
line is deferred until it is complete.

Claude root sessions, actual subagent sessions, and textual tool-result sidecars
are included. Codex user tasks, automations, subagents, and active/archived
lifecycle are included. Moving a Codex file to `archived_sessions` updates the
same session rather than duplicating it.

If an original file disappears, its artifact health becomes `missing`; retained
history is not deleted.

## Browser

Open <http://127.0.0.1:7337> or run `vg open`.

- The left pane filters provider, session type, and lifecycle and shows source
  health.
- The center pane pages sessions with provider/type/lifecycle badges.
- The detail pane shows lineage, redacted metadata, and a paginated transcript.
- Press Command-K (or Control-K) to search across both providers.
- Within-session filtering affects the currently mounted transcript.
- Oversized blocks load only when requested; at most 300 messages remain mounted.

Search matches are rendered as structured text, not provider-controlled HTML.
The UI uses local assets and does not contact a CDN or font service.

## CLI search

```bash
vg search "backup checksum"
vg search "migration" --provider claude_code
vg search "restore" --provider codex --session-type subagent
vg search "nightly" --session-type automation --lifecycle archived
vg search "redaction" --project vimgym --branch main --since 30d --json
```

Search is lexical SQLite FTS5. It covers visible redacted message text, tool
names/arguments/results, and visible provider summaries. Hidden reasoning and
telemetry are not searchable because they are not stored as visible content.

## Export

The detail view provides two streamed exports:

- Markdown: paste-friendly provider-neutral transcript.
- Canonical JSONL: one redacted session record followed by ordered message/block
  records.

The v0.1 provider-native raw endpoint no longer exists. Exports never contain
unredacted provider JSONL.

## Reindex

Parser and redaction-policy changes automatically invalidate affected artifacts.
You can also force reconciliation:

```bash
vg reindex
vg reindex --provider codex
vg reindex --session SESSION_ID_OR_UNIQUE_PREFIX
```

Reindex is safe to repeat. A failed new revision leaves the last valid revision
available.

## Portable backup and restore

Create a backup to a directory or explicit `.vgbak` filename:

```bash
vg backup create /Volumes/Backups
vg backup create ~/Backups/vimgym-manual.vgbak
vg backup verify ~/Backups/vimgym-manual.vgbak
```

The daemon may stay running during backup. Vimgym uses SQLite's online backup
API and verifies the resulting snapshot, manifest sizes/hashes, JSON, schema,
canonical counts, and SQLite integrity before reporting success.

Restore is whole-vault replacement, not merge:

```bash
# Fresh destination
vg backup restore backup.vgbak --to ~/.vimgym-restored

# Existing destination: stop its daemon and explicitly request replacement
vg stop
vg backup restore backup.vgbak --to ~/.vimgym --replace
```

Replacement creates a rollback backup before the atomic swap. Traversal,
duplicate members, corrupt hashes, invalid SQLite, and unsupported future schema
fail without changing the destination. Missing provider roots are disabled and
reported; restored sessions remain searchable.

`.vgbak` files are owner-only but unencrypted. Treat them as sensitive work
history. Redaction scrubs credentials; it does not anonymize people, repositories,
or business context.

## Diagnostics

```bash
vg status
vg doctor
vg doctor --json | jq
```

Doctor checks Python/SQLite FTS5, vault permissions/schema/integrity, daemon
state, configured source paths, artifact outcomes, disk space, and redaction
policy health. Common artifact statuses are `imported`, `unchanged`, `updated`,
`degraded`, `failed`, `missing`, and `needs_reindex`.

Logs rotate under `~/.vimgym/logs/`. Diagnostics and logs are bounded and
redacted. Provider-native records are not logged.

## Redaction rules

Built-in credential patterns ship inside the wheel. A custom
`~/.vimgym/redaction-rules.json` replaces the built-in policy and must contain at
least one valid rule:

```json
{
  "rules": [
    {
      "name": "internal-token",
      "pattern": "INTERNAL_[A-Z0-9]{24}",
      "replacement": "[REDACTED_INTERNAL_TOKEN]"
    }
  ]
}
```

Malformed, unreadable, or empty custom policies fail closed. Changing rules
changes the policy hash and forces current source artifacts through reindex.
Previously retained sessions whose provider files are missing keep their last
valid redacted revision.

## Upgrade from v0.1.1

Start v0.2 normally. Before modifying the database, Vimgym takes an owner-only
SQLite rollback snapshot. It migrates and re-redacts sessions/messages, rebuilds
block FTS, removes v1 raw tables, reconciles counts, runs SQLite integrity and
foreign-key checks, vacuums deleted pages, and only then starts the watcher and
server.

There is no down migration. To roll back: stop v0.2, restore the pre-migration
v1 snapshot from `~/.vimgym/backups/`, and reinstall v0.1.1.

## Supported scope

v0.2 supports macOS and Linux with Python 3.11–3.14 after the release matrix
passes. It does not support Windows, cloud sync/S3, accounts, telemetry, team
collaboration, encrypted backups, vector/semantic search, generated summaries,
tags/cost dashboards, provider writeback, or third-party adapters.
