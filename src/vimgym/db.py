"""SQLite vault initialization, v1→v2 migration, and connection management.

Schema v2 stores a redacted, provider-neutral canonical record. Provider-native
JSONL never enters the vault. Legacy identifier columns remain during the v0.2
compatibility window, but v1 raw and session-aggregate FTS tables are removed.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from vimgym.pipeline.redact import RedactionEngine
from vimgym.storage.fts import insert_block_fts

SCHEMA_VERSION = 2
_IDENTITY_NAMESPACE = uuid.UUID("be5cb4ba-1681-50d5-bf58-b5e75cd1a87c")


class FutureSchemaError(RuntimeError):
    """The vault was created by a newer Vimgym and must not be modified."""


class MigrationError(RuntimeError):
    """A migration could not produce a validated v2 vault."""


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id                  TEXT PRIMARY KEY,
    provider            TEXT NOT NULL,
    name                TEXT NOT NULL,
    root_path           TEXT NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1,
    config_json         TEXT NOT NULL DEFAULT '{}',
    health              TEXT NOT NULL DEFAULT 'unknown',
    diagnostic_count    INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    last_seen_at        TEXT,
    last_indexed_at     TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_provider ON sources(provider);

CREATE TABLE IF NOT EXISTS workspaces (
    id              TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    canonical_cwd   TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(provider, canonical_cwd)
);

CREATE TABLE IF NOT EXISTS sessions (
    id                      TEXT PRIMARY KEY,
    provider                TEXT NOT NULL,
    external_session_id     TEXT NOT NULL,
    session_uuid            TEXT,
    legacy_id               TEXT,
    source_id               TEXT NOT NULL REFERENCES sources(id),
    kind                    TEXT NOT NULL DEFAULT 'unknown',
    lifecycle               TEXT NOT NULL DEFAULT 'active',
    parent_session_id       TEXT,
    root_session_id         TEXT,
    originator              TEXT,
    client_name             TEXT,
    client_version          TEXT,
    model                   TEXT,
    title                   TEXT,
    title_source            TEXT,
    workspace_id            TEXT REFERENCES workspaces(id),

    slug                    TEXT,
    source_path             TEXT NOT NULL,
    project_dir             TEXT NOT NULL DEFAULT '',
    project_name            TEXT NOT NULL DEFAULT '',
    cwd                     TEXT,
    git_branch              TEXT,
    entrypoint              TEXT,
    claude_version          TEXT,
    permission_mode         TEXT,

    started_at              TEXT NOT NULL,
    updated_at              TEXT,
    ended_at                TEXT,
    duration_secs           INTEGER,
    message_count           INTEGER NOT NULL DEFAULT 0,
    user_message_count      INTEGER NOT NULL DEFAULT 0,
    asst_message_count      INTEGER NOT NULL DEFAULT 0,
    tool_use_count          INTEGER NOT NULL DEFAULT 0,
    has_subagents           INTEGER NOT NULL DEFAULT 0,
    input_tokens            INTEGER NOT NULL DEFAULT 0,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens      INTEGER NOT NULL DEFAULT 0,

    ai_title                TEXT,
    summary                 TEXT,
    tools_used              TEXT NOT NULL DEFAULT '[]',
    files_modified          TEXT NOT NULL DEFAULT '[]',

    parser_version          TEXT NOT NULL DEFAULT '1',
    redaction_policy_hash   TEXT NOT NULL,
    revision                INTEGER NOT NULL DEFAULT 1,
    health                  TEXT NOT NULL DEFAULT 'ok',
    diagnostics_json        TEXT NOT NULL DEFAULT '[]',
    backed_up_at            TEXT NOT NULL,
    last_seen_at            TEXT,
    indexed_at              TEXT,
    file_hash               TEXT NOT NULL DEFAULT '',
    file_size_bytes         INTEGER NOT NULL DEFAULT 0,
    schema_version          INTEGER NOT NULL DEFAULT 2,

    UNIQUE(provider, external_session_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_legacy_uuid
    ON sessions(session_uuid) WHERE session_uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_provider_kind
    ON sessions(provider, kind, lifecycle);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_name);
CREATE INDEX IF NOT EXISTS idx_sessions_branch ON sessions(git_branch);
CREATE INDEX IF NOT EXISTS idx_sessions_hash ON sessions(file_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source_id);

CREATE TABLE IF NOT EXISTS source_artifacts (
    id                      TEXT PRIMARY KEY,
    source_id               TEXT NOT NULL REFERENCES sources(id),
    session_id              TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    relative_path           TEXT NOT NULL,
    lifecycle               TEXT NOT NULL DEFAULT 'active',
    size_bytes              INTEGER NOT NULL DEFAULT 0,
    mtime_ns                INTEGER NOT NULL DEFAULT 0,
    content_sha256          TEXT NOT NULL DEFAULT '',
    parser_version          TEXT NOT NULL,
    redaction_policy_hash   TEXT NOT NULL,
    processed_bytes         INTEGER NOT NULL DEFAULT 0,
    processed_lines         INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'pending',
    diagnostics_json        TEXT NOT NULL DEFAULT '[]',
    last_seen_at            TEXT,
    last_indexed_at         TEXT,
    UNIQUE(source_id, relative_path)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON source_artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_status ON source_artifacts(status);

CREATE TABLE IF NOT EXISTS messages (
    id                      TEXT PRIMARY KEY,
    session_id              TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    artifact_id             TEXT REFERENCES source_artifacts(id) ON DELETE CASCADE,
    session_uuid            TEXT,
    external_message_id     TEXT,
    sequence                INTEGER NOT NULL,
    source_line             INTEGER NOT NULL DEFAULT 0,
    item_index              INTEGER NOT NULL DEFAULT 0,
    type                    TEXT NOT NULL,
    role                    TEXT NOT NULL,
    model                   TEXT,
    turn_id                 TEXT,
    timestamp               TEXT,
    parent_message_id       TEXT,
    parent_uuid             TEXT,
    visibility              TEXT NOT NULL DEFAULT 'visible',
    has_tool_use            INTEGER NOT NULL DEFAULT 0,
    has_thinking            INTEGER NOT NULL DEFAULT 0,
    has_image               INTEGER NOT NULL DEFAULT 0,
    tool_names              TEXT NOT NULL DEFAULT '[]',
    content_json            TEXT NOT NULL DEFAULT '[]',
    revision                INTEGER NOT NULL DEFAULT 1,
    UNIQUE(artifact_id, sequence, item_index)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_legacy_session ON messages(session_uuid);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);

CREATE TABLE IF NOT EXISTS message_blocks (
    id                  TEXT PRIMARY KEY,
    message_id          TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    artifact_id         TEXT REFERENCES source_artifacts(id) ON DELETE CASCADE,
    sequence            INTEGER NOT NULL,
    block_index         INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    visibility          TEXT NOT NULL DEFAULT 'visible',
    text_content        TEXT,
    data_json           TEXT,
    name                TEXT,
    call_id             TEXT,
    mime_type           TEXT,
    is_error            INTEGER NOT NULL DEFAULT 0,
    truncated           INTEGER NOT NULL DEFAULT 0,
    original_bytes      INTEGER NOT NULL DEFAULT 0,
    revision            INTEGER NOT NULL DEFAULT 1,
    UNIQUE(message_id, block_index)
);
CREATE INDEX IF NOT EXISTS idx_blocks_session ON message_blocks(session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_blocks_call ON message_blocks(call_id);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    searchable_text,
    content = '',
    detail = none,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS session_tools (
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tool_name   TEXT NOT NULL,
    use_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, tool_name)
);

CREATE TABLE IF NOT EXISTS session_files (
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    operation   TEXT NOT NULL DEFAULT 'referenced',
    PRIMARY KEY(session_id, path, operation)
);

CREATE TABLE IF NOT EXISTS projects (
    project_name        TEXT PRIMARY KEY,
    project_dir         TEXT NOT NULL,
    cwd                 TEXT,
    session_count       INTEGER DEFAULT 0,
    last_active         TEXT,
    total_duration_secs INTEGER DEFAULT 0,
    total_input_tokens  INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS config (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""


_local = threading.local()


def _identity(*parts: str) -> str:
    return str(uuid.uuid5(_IDENTITY_NAMESPACE, "\0".join(parts)))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migration_checkpoint(_name: str) -> None:
    """Fault-injection seam used by migration recovery tests."""


def _check_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_test USING fts5(x)")
        conn.execute("DROP TABLE _fts5_test")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "SQLite FTS5 not available. Python was built without FTS5 support."
        ) from exc


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


@contextmanager
def _migration_lock(vault_dir: Path) -> Iterator[None]:
    lock_path = vault_dir / ".migration.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _create_rollback_snapshot(conn: sqlite3.Connection, db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)
    except OSError:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"pre-migration-v1-{stamp}.db"
    dst = sqlite3.connect(target)
    try:
        conn.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise MigrationError("pre-migration snapshot failed integrity_check")
    finally:
        dst.close()
    os.chmod(target, 0o600)
    return target


def _flatten_strings(value: Any) -> str:
    parts: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for key, child in item.items():
                parts.append(str(key))
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return "\n".join(part for part in parts if part)


def _block_kind(block: dict[str, Any]) -> tuple[str, str]:
    native = str(block.get("type") or "unknown")
    if native == "tool_use":
        return "tool_call", "visible"
    if native == "tool_result":
        return "tool_result", "visible"
    if native == "image":
        return "attachment", "visible"
    if native == "thinking":
        return "omitted", "hidden"
    if native == "text":
        return "text", "visible"
    return "unknown_event", "hidden"


def _migrate_v1(conn: sqlite3.Connection, db_path: Path) -> None:
    """Transform a v0.1.1 database into the canonical v2 schema."""
    snapshot = _create_rollback_snapshot(conn, db_path)
    _migration_checkpoint("snapshot_created")
    engine = RedactionEngine(db_path.parent / "redaction-rules.json")
    now = _utcnow()

    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        # Keep DDL and data copy in one explicit script-owned transaction.
        # sqlite3.executescript() commits an already-open transaction, so the
        # BEGIN must be part of the script itself.
        migration_sql = ["BEGIN IMMEDIATE;"]
        for index in (
            "idx_sessions_project",
            "idx_sessions_started",
            "idx_sessions_branch",
            "idx_sessions_hash",
            "idx_sessions_uuid",
            "idx_sessions_source",
            "idx_messages_session",
            "idx_messages_timestamp",
        ):
            migration_sql.append(f'DROP INDEX IF EXISTS "{index}";')
        for table in ("sessions", "messages", "projects", "config"):
            if _table_exists(conn, table):
                migration_sql.append(f'ALTER TABLE "{table}" RENAME TO "_v1_{table}";')
        migration_sql.append(SCHEMA_DDL)
        conn.executescript("\n".join(migration_sql))
        _migration_checkpoint("schema_created")

        old_session_count = conn.execute("SELECT COUNT(*) FROM _v1_sessions").fetchone()[0]
        old_message_count = (
            conn.execute("SELECT COUNT(*) FROM _v1_messages").fetchone()[0]
            if _table_exists(conn, "_v1_messages")
            else 0
        )

        session_map: dict[str, str] = {}
        session_artifact_map: dict[str, str] = {}
        rows = conn.execute("SELECT * FROM _v1_sessions ORDER BY started_at, rowid")
        for legacy in rows:
            external_id = str(legacy["session_uuid"])
            internal_id = _identity("session", "claude_code", external_id)
            session_map[external_id] = internal_id
            source_id = str(legacy["source_id"] or "claude_code")
            source_path = str(legacy["source_path"] or "")
            redacted_source_path = engine.redact_text(source_path)[:16_384]
            source_root = str(Path(source_path).parent)
            conn.execute(
                """
                INSERT OR IGNORE INTO sources(
                    id, provider, name, root_path, enabled, health,
                    created_at, updated_at, last_seen_at
                ) VALUES (?, 'claude_code', 'Claude Code', ?, 1, 'degraded', ?, ?, ?)
                """,
                (source_id, source_root, now, now, now),
            )

            cwd = engine.redact_text(str(legacy["cwd"] or ""))[:16_384]
            workspace_id = _identity("workspace", "claude_code", cwd)
            project_name = engine.redact_text(str(legacy["project_name"] or ""))[:500]
            conn.execute(
                """
                INSERT OR IGNORE INTO workspaces(
                    id, provider, canonical_cwd, display_name, created_at
                ) VALUES (?, 'claude_code', ?, ?, ?)
                """,
                (workspace_id, cwd, project_name or Path(cwd).name or "Unknown", now),
            )

            tools = engine.redact_value(json.loads(legacy["tools_used"] or "[]"))
            files = engine.redact_value(json.loads(legacy["files_modified"] or "[]"))
            title = engine.redact_text(str(legacy["ai_title"] or "")).strip()[:500] or None
            started = str(legacy["started_at"] or now)
            conn.execute(
                """
                INSERT INTO sessions(
                    id, provider, external_session_id, session_uuid, legacy_id,
                    source_id, kind, lifecycle, originator, client_name, client_version,
                    title, title_source, workspace_id, slug, source_path, project_dir,
                    project_name, cwd, git_branch, entrypoint, claude_version,
                    permission_mode, started_at, updated_at, ended_at, duration_secs,
                    message_count, user_message_count, asst_message_count,
                    tool_use_count, has_subagents, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, ai_title, summary,
                    tools_used, files_modified, parser_version, redaction_policy_hash,
                    revision, health, diagnostics_json, backed_up_at, last_seen_at,
                    indexed_at, file_hash, file_size_bytes, schema_version
                ) VALUES (
                    ?, 'claude_code', ?, ?, ?, ?, 'user', 'active',
                    'Claude Code', 'claude_code', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 1, ?, 1,
                    'degraded', '["migrated; source reindex required"]', ?, ?, ?, ?, ?, 2
                )
                """,
                (
                    internal_id,
                    external_id,
                    external_id,
                    str(legacy["id"]),
                    source_id,
                    engine.redact_text(str(legacy["claude_version"] or "")) or None,
                    title,
                    "provider" if title else "fallback",
                    workspace_id,
                    engine.redact_text(str(legacy["slug"] or ""))[:500] or None,
                    redacted_source_path,
                    engine.redact_text(str(legacy["project_dir"] or ""))[:16_384],
                    project_name,
                    cwd or None,
                    engine.redact_text(str(legacy["git_branch"] or ""))[:4096] or None,
                    engine.redact_text(str(legacy["entrypoint"] or "")) or None,
                    engine.redact_text(str(legacy["claude_version"] or "")) or None,
                    engine.redact_text(str(legacy["permission_mode"] or "")) or None,
                    started,
                    started,
                    legacy["ended_at"],
                    legacy["duration_secs"],
                    legacy["message_count"],
                    legacy["user_message_count"],
                    legacy["asst_message_count"],
                    legacy["tool_use_count"],
                    legacy["has_subagents"],
                    legacy["input_tokens"],
                    legacy["output_tokens"],
                    legacy["cache_read_tokens"],
                    legacy["cache_write_tokens"],
                    title,
                    json.dumps(tools, ensure_ascii=False),
                    json.dumps(files, ensure_ascii=False),
                    engine.policy_hash,
                    now,
                    now,
                    now,
                    str(legacy["file_hash"] or ""),
                    int(legacy["file_size_bytes"] or 0),
                ),
            )
            artifact_id = _identity("artifact", "claude_code", internal_id, "session_jsonl")
            session_artifact_map[internal_id] = artifact_id
            conn.execute(
                """
                INSERT INTO source_artifacts(
                    id, source_id, session_id, relative_path, size_bytes,
                    content_sha256, parser_version, redaction_policy_hash, status,
                    diagnostics_json, last_seen_at, last_indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'needs_reindex',
                          '["migrated from schema v1"]', ?, ?)
                """,
                (
                    artifact_id,
                    source_id,
                    internal_id,
                    Path(source_path).name,
                    int(legacy["file_size_bytes"] or 0),
                    str(legacy["file_hash"] or ""),
                    engine.policy_hash,
                    now,
                    now,
                ),
            )
            for tool in tools if isinstance(tools, list) else []:
                conn.execute(
                    "INSERT OR REPLACE INTO session_tools VALUES (?, ?, 1)",
                    (internal_id, str(tool)),
                )
            for file_path in files if isinstance(files, list) else []:
                conn.execute(
                    "INSERT OR REPLACE INTO session_files VALUES (?, ?, 'modified')",
                    (internal_id, str(file_path)),
                )
        _migration_checkpoint("sessions_copied")

        if _table_exists(conn, "_v1_messages"):
            counters: dict[str, int] = {}
            legacy_messages = conn.execute(
                "SELECT rowid AS _rowid, * FROM _v1_messages ORDER BY rowid"
            )
            for legacy in legacy_messages:
                external_session = str(legacy["session_uuid"])
                internal_session = session_map.get(external_session)
                if internal_session is None:
                    continue
                sequence = counters.get(internal_session, 0) + 1
                counters[internal_session] = sequence
                external_message = str(legacy["id"]).split(":", 1)[-1]
                message_id = _identity("message", internal_session, external_message)
                try:
                    native_blocks = json.loads(legacy["content_json"] or "[]")
                except json.JSONDecodeError:
                    native_blocks = []
                if not isinstance(native_blocks, list):
                    native_blocks = []
                redacted_blocks = engine.redact_value(native_blocks)
                role = str(legacy["role"] or "unknown")
                conn.execute(
                    """
                    INSERT INTO messages(
                        id, session_id, artifact_id, session_uuid, external_message_id, sequence,
                        source_line, type, role, timestamp, parent_uuid,
                        has_tool_use, has_thinking, has_image, tool_names, content_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        internal_session,
                        session_artifact_map[internal_session],
                        external_session,
                        external_message,
                        sequence,
                        sequence,
                        legacy["type"],
                        role,
                        legacy["timestamp"],
                        legacy["parent_uuid"],
                        legacy["has_tool_use"],
                        legacy["has_thinking"],
                        legacy["has_image"],
                        json.dumps(engine.redact_value(json.loads(legacy["tool_names"] or "[]"))),
                        "[]",
                    ),
                )
                for block_index, block in enumerate(redacted_blocks):
                    if not isinstance(block, dict):
                        block = {"type": "unknown", "value": block}
                    kind, visibility = _block_kind(block)
                    block_id = _identity("block", message_id, str(block_index))
                    text = block.get("text")
                    if kind == "tool_result":
                        text = _flatten_strings(block.get("content"))
                    elif kind == "tool_call":
                        text = _flatten_strings(block.get("input"))
                    elif kind in {"attachment", "omitted"}:
                        text = None
                    encoded_block = json.dumps(block, ensure_ascii=False)
                    cursor = conn.execute(
                        """
                        INSERT INTO message_blocks(
                            id, message_id, session_id, artifact_id, sequence, block_index, kind,
                            visibility, text_content, data_json, name, call_id,
                            mime_type, is_error, original_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            block_id,
                            message_id,
                            internal_session,
                            session_artifact_map[internal_session],
                            sequence,
                            block_index,
                            kind,
                            visibility,
                            str(text) if text is not None else None,
                            encoded_block,
                            block.get("name"),
                            block.get("id") or block.get("tool_use_id"),
                            block.get("mime_type") or block.get("media_type"),
                            1 if block.get("is_error") else 0,
                            len(encoded_block.encode("utf-8")),
                        ),
                    )
                    if cursor.lastrowid is None:
                        raise MigrationError("message block insert did not return a row id")
                    insert_block_fts(
                        conn,
                        int(cursor.lastrowid),
                        str(text) if text is not None else None,
                        encoded_block,
                        visible=visibility == "visible",
                    )
        _migration_checkpoint("messages_copied")

        # Provider titles win. Sessions without one receive a deterministic,
        # already-redacted preview from their first visible user text block.
        for migrated_session in conn.execute(
            "SELECT id FROM sessions WHERE title IS NULL ORDER BY id"
        ).fetchall():
            first_user_text = conn.execute(
                """
                SELECT b.text_content FROM messages m
                JOIN message_blocks b ON b.message_id=m.id
                WHERE m.session_id=? AND m.role='user' AND b.kind='text'
                  AND b.visibility='visible' AND b.text_content!=''
                ORDER BY m.sequence,b.block_index LIMIT 1
                """,
                (migrated_session["id"],),
            ).fetchone()
            if first_user_text is not None:
                preview = str(first_user_text[0]).strip()[:120]
                if preview:
                    conn.execute(
                        """
                        UPDATE sessions SET title=?,ai_title=?,title_source='fallback'
                        WHERE id=?
                        """,
                        (preview, preview, migrated_session["id"]),
                    )

        # Recompute legacy project aggregates from canonical sessions.
        for row in conn.execute(
            """
            SELECT project_name, MIN(project_dir) project_dir, MIN(cwd) cwd,
                   COUNT(*) session_count, MAX(started_at) last_active,
                   COALESCE(SUM(duration_secs),0) duration,
                   COALESCE(SUM(input_tokens),0) input_tokens,
                   COALESCE(SUM(output_tokens),0) output_tokens
            FROM sessions GROUP BY project_name
            """
        ):
            conn.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(row))
        _migration_checkpoint("derived_built")

        if _table_exists(conn, "_v1_config"):
            conn.execute(
                "INSERT OR IGNORE INTO config SELECT key, value FROM _v1_config WHERE key!='schema_version'"
            )
        conn.execute("INSERT OR REPLACE INTO config(key,value) VALUES('schema_version','2')")
        conn.execute(
            "INSERT INTO schema_migrations VALUES (2, 'provider-neutral-vault', ?)",
            (now,),
        )

        new_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        new_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if new_sessions != old_session_count or new_messages != old_message_count:
            raise MigrationError(
                "migration row-count reconciliation failed "
                f"({old_session_count}/{old_message_count} -> {new_sessions}/{new_messages})"
            )

        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity is None or integrity[0] != "ok" or fk_errors:
            raise MigrationError("pre-commit migration validation failed")
        # Exercise the exact block-level FTS query path even when the fixed
        # probe has no matches. This catches an unusable index before legacy
        # raw/search tables are removed.
        conn.execute(
            "SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH ?",
            ("vimgymmigrationprobe",),
        ).fetchone()
        _migration_checkpoint("precommit_validated")

        for table in (
            "sessions_fts",
            "sessions_raw",
            "_v1_messages",
            "_v1_sessions",
            "_v1_projects",
            "_v1_config",
        ):
            if _table_exists(conn, table):
                conn.execute(f'DROP TABLE "{table}"')
        _migration_checkpoint("legacy_removed")
        conn.execute("PRAGMA user_version=2")
        conn.execute("COMMIT")
        _migration_checkpoint("committed")
        conn.execute("PRAGMA foreign_keys=ON")

        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity is None or integrity[0] != "ok" or fk_errors:
            raise MigrationError("post-commit migration validation failed")
        _migration_checkpoint("postcommit_validated")
        # Dropped v1 pages can otherwise retain deleted raw JSON bytes in the
        # SQLite freelist. Rebuild and checkpoint before daemon startup so the
        # active vault and its WAL/SHM sidecars contain canonical redacted data
        # only. The owner-only rollback snapshot intentionally remains an exact
        # v1 recovery artifact.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:
        committed = not conn.in_transaction
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys=ON")
        if committed:
            # A post-commit validation failure must restore v1 before the daemon
            # can start. Backup API copies pages without exposing provider data.
            source = sqlite3.connect(snapshot)
            try:
                source.backup(conn)
            finally:
                source.close()
        raise MigrationError(
            f"schema v1 migration failed; rollback snapshot: {snapshot}: {exc}"
        ) from exc


def init_db(db_path: Path) -> None:
    """Initialize or migrate a vault before watcher/server startup.

    The operation is idempotent and serialized by an owner-only file lock.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(db_path.parent, 0o700)
    except OSError:
        pass

    with _migration_lock(db_path.parent):
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise FutureSchemaError(
                    f"vault schema v{version} is newer than supported v{SCHEMA_VERSION}"
                )
            _configure(conn)
            _check_fts5(conn)

            session_columns = _columns(conn, "sessions")
            is_v1 = bool(session_columns) and "provider" not in session_columns
            if is_v1:
                _migrate_v1(conn, db_path)
            else:
                conn.executescript(SCHEMA_DDL)
                now = _utcnow()
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES (2, 'provider-neutral-vault', ?)",
                    (now,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO config(key,value) VALUES('schema_version','2')"
                )
                conn.execute("PRAGMA user_version=2")
                conn.commit()
        finally:
            conn.close()

    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Return a thread-local SQLite connection configured for WAL readers."""
    db_path = Path(db_path)
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    key = str(db_path.resolve())
    conn = cache.get(key)
    if conn is None:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            conn.close()
            raise FutureSchemaError(
                f"vault schema v{version} is newer than supported v{SCHEMA_VERSION}"
            )
        _configure(conn)
        cache[key] = conn
        _local.conns = cache
    return conn


def close_all_connections() -> None:
    """Close thread-local connections (primarily test and restore cleanup)."""
    cache = getattr(_local, "conns", None) or {}
    for conn in cache.values():
        try:
            conn.close()
        except Exception:
            pass
    _local.conns = {}
