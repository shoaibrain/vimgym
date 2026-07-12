# Vimgym v0.2 developer reference

This document describes the implemented v0.2 architecture. The decision record
and release contract are in [V0.2-SPEC.md](V0.2-SPEC.md).

## Stack and process model

- Python 3.11–3.14
- FastAPI/Uvicorn on loopback only
- SQLite WAL + FTS5
- Watchdog polling observer with one bounded coalescing worker
- Vanilla HTML/CSS/JavaScript

The daemon is one process. Startup order is deliberately strict:

1. Acquire PID/single-process ownership.
2. Initialize or migrate schema under `.migration.lock`.
3. Save validated loopback configuration.
4. Reconcile every enabled provider source.
5. Start the filesystem observer and one ingestion worker.
6. Start Uvicorn and the WebSocket event pump.

No watcher or HTTP route runs against a partially migrated database.

## Modules

```text
src/vimgym/
├── backup.py                 verified .vgbak create/verify/restore
├── cli.py                    vg command tree
├── config.py                 source discovery and loopback validation
├── daemon.py                 PID, logging, startup/shutdown
├── db.py                     schema v2 and v1 migration
├── ingestion.py              redacting staging and atomic revisions
├── providers/
│   ├── base.py               canonical contracts and deterministic IDs
│   ├── claude.py             Claude root/child/sidecar adapter
│   ├── codex.py              Codex active/archive adapter
│   └── registry.py           two built-in adapters only
├── pipeline/                 v0.1 parser compatibility and redaction policy
├── storage/
│   ├── queries.py            filters, keyset cursors, block FTS
│   ├── export.py             streaming Markdown/canonical JSONL
│   └── writer.py             v0.1 ParsedSession compatibility bridge
├── server.py                 secure local API, WebSocket, static UI
├── ui/                       three-pane browser application
└── watcher.py                startup reconciliation and event coalescing
```

`pipeline/parser.py`, `pipeline/orchestrator.py`, and `storage/writer.py` remain
as an internal compatibility bridge for v0.1 callers/tests. New provider capture
must use `providers` + `ingestion` and must not add another raw persistence path.

## Provider contract

`ProviderAdapter` exposes:

```python
class ProviderAdapter(Protocol):
    provider: Literal["claude_code", "codex"]
    parser_version: str

    def default_sources(self, environment) -> list[SourceSpec]: ...
    def iter_artifacts(self, source) -> Iterable[ArtifactCandidate]: ...
    def probe(self, artifact) -> ArtifactIdentity: ...
    def parse(self, artifact, sink: RecordSink) -> ParseOutcome: ...
```

Adapters are read-only and stream three record types:

- `CanonicalSession`: provider identity, kind/lifecycle/lineage, originator,
  client/model/title, workspace, times, usage, metadata.
- `CanonicalMessage`: deterministic ID, source sequence, role/model/turn/time,
  native and parent identity.
- `CanonicalBlock`: visible/hidden block type, text/data, tool call ID, MIME/error,
  truncation metadata.

An adapter never opens SQLite or writes an export/log. It reports bounded
content-free `Diagnostic` values and a `ParseOutcome` with content hash,
complete line/byte counts, status, ignored/unknown counters, and partial-tail
state.

The registry is intentionally closed to the two built-in providers in v0.2.

## Identity

`VIMGYM_ID_NAMESPACE` is immutable after release.

- Session: UUIDv5 of `session\0provider\0external_session_id`.
- Workspace: provider plus canonical real CWD.
- Message: session namespace plus provider-native ID, otherwise source line/item.
- Block: message namespace plus native call/block identity and index.

Provider plus external session ID is unique. Codex active/archive paths do not
participate in session identity. Claude subagent external identity is
`root:agent:agentId`. `originator` is never overloaded as provider.

## Privacy boundary

`RedactingStagingSink.emit(CanonicalRecord)` is the only production adapter
sink. It:

1. Recursively redacts every content-bearing string.
2. Preserves only operational identity/timestamp/hash/source-root metadata.
3. Wraps the transformed record in `RedactedRecord(policy_hash=...)`.
4. Passes the wrapper to the private staging writer.

The staging writer rejects unwrapped records. Temporary SQLite staging rows are
already redacted. Search text is derived only from visible staged blocks.

`RedactionEngine` compiles every configured rule or raises
`RedactionPolicyError`. It does not silently skip invalid rules or fall back from
a malformed custom policy. `policy_hash` is SHA-256 over canonical rule JSON.

Never place provider objects, tool arguments/results, paths, or exception
payloads directly in logs/diagnostics. Use stable codes and bounded redacted
messages.

## Revision transaction

For each artifact:

- stat/parser/policy equality skips without opening content;
- constant-memory SHA-256 detects metadata-only changes;
- adapters stream complete lines to temporary staging;
- graph validation requires one session and no orphan messages/blocks;
- one `BEGIN IMMEDIATE` updates source/session/artifact metadata, removes only
  that artifact's previous messages/FTS, inserts staged rows, rebuilds derived
  tools/files/counts, and advances the revision;
- rollback leaves the prior committed revision intact;
- events publish only after commit.

Session JSONL artifact identity is provider+session based, so a Codex archive
move changes source/path/lifecycle on the same artifact. Claude text sidecars use
path identity and contribute additional tool-result messages to the root session
without replacing root metadata/messages.

The parser intentionally does a full streaming reparse after a content change.
Do not add byte-offset cursors without benchmark evidence and rewrite/truncation
correctness tests.

## Schema v2

Authoritative tables:

```text
schema_migrations
sources ─┬─ source_artifacts ── messages ── message_blocks
         └─ sessions ──────────┘             │
              │                              └─ message_fts
              ├─ workspaces
              ├─ session_tools
              └─ session_files
```

`projects` and several Claude-named session/message columns remain as a v0.2
read compatibility bridge. They contain redacted canonical data. v1
`sessions_raw` and `sessions_fts` do not exist after migration.

All foreign keys are enabled. Parent/root session IDs permit deferred lineage
because subagents may be discovered before roots; deterministic IDs make them
resolvable once both rows exist.

## Migration

`init_db()` detects a v1 `sessions` shape even when `PRAGMA user_version` was
never set by v0.1. It creates a SQLite online snapshot, then one DDL/data
transaction:

- rename relational v1 inputs;
- remove v1 raw/aggregate FTS;
- create v2;
- map all sessions to Claude Code deterministic IDs;
- recursively re-redact titles/paths/content JSON;
- split message blocks and build block FTS;
- preserve legacy IDs/metadata/counts/tools/files;
- create `needs_reindex` source artifacts;
- reconcile row counts and delete temporary v1 tables;
- commit user/schema version 2;
- run integrity/foreign-key checks, checkpoint, and vacuum deleted pages.

Transaction failure rolls back. Post-commit validation failure restores the
snapshot before raising. A future schema raises `FutureSchemaError` before WAL
configuration or mutation.

## Read API

- Session pages use opaque keyset cursors over `(started_at DESC, id DESC)`.
- Search uses block FTS and opaque rank/message/block cursors.
- Snippets are Python-created `{text, matched}` parts; FTS never emits markup.
- Message pages are sequence-based, max 200, and budgeted below 2 MiB.
- Blocks above 64 KiB become 8 KiB previews with a full-content URL.
- Markdown/canonical JSONL exports query and yield batches instead of building a
  transcript in memory.
- Legacy Claude internal/native UUID prefixes resolve only when unique.

HTTP/WebSocket security lives in `server.py`: forced loopback config, Host and
Origin validation, CSP and hardening headers, no CORS wildcard, and no external
assets. Keep API routes above the root static mount.

## Backup format

`backup.py` is independent of internal table serialization beyond schema/count
verification. It uses `sqlite3.Connection.backup()` into an owner-only temporary
sibling, validates it, builds a ZIP manifest with per-member SHA-256 and size,
fsyncs, and atomically renames.

Verification rejects duplicate/absolute/traversal/unexpected members, malformed
JSON, hash/size/count mismatch, invalid/future SQLite, and failed integrity or
foreign-key checks. Restore hashes bytes again while extracting, validates before
destination mutation, and swaps a whole temporary vault. Replacement requires a
stopped daemon and rollback backup.

Never add PID/log/WAL/SHM/cache/provider files to the archive. Backups are not
encrypted.

## Tests and fixtures

Committed fixtures live under `tests/fixtures/` and must pass
`tests/test_fixture_safety.py`. Do not commit a private path, credential marker,
thinking signature, or long opaque/base64 payload. The sanitizer must remain
byte deterministic.

Fast PR tests cover adapter maps, identity, revision changes, partial tails,
archive moves, redaction sentinel egress, migration, backup corruption/traversal,
cursor pagination, Host/Origin/CSP, and UI static injection/accessibility rules.

Run locally:

```bash
python -m pytest
ruff check src tests
mypy src/vimgym
python -m build
twine check dist/*
```

The deterministic benchmark generator creates the release-scale corpus. Heavy
performance, cross-platform restore, long watcher, dependency-resolution, and
`pip-audit` lanes run nightly/release-candidate rather than slowing every edit.

## Change rules

- Preserve provider files byte-for-byte.
- Keep the privacy boundary before every durable or egress surface.
- Do not add a provider by reading adjacent state databases.
- Do not weaken loopback enforcement to solve remote access.
- Do not reintroduce raw JSONL or HTML snippets.
- Do not turn an internal adapter registry into a plugin marketplace.
- Add a fixture and explicit mapping/exclusion for every new provider record
  family.
