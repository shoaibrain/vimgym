"""Transactional canonical writer and v0.1 parser compatibility bridge."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vimgym.pipeline.redact import RedactionEngine
from vimgym.storage.fts import delete_fts_for_blocks, insert_block_fts

if TYPE_CHECKING:
    from vimgym.pipeline.metadata import SessionMetadata
    from vimgym.pipeline.parser import ParsedSession


PARSER_VERSION = 2
_IDENTITY_NAMESPACE = uuid.UUID("be5cb4ba-1681-50d5-bf58-b5e75cd1a87c")


def _id(*parts: str) -> str:
    return str(uuid.uuid5(_IDENTITY_NAMESPACE, "\0".join(parts)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_exists_by_hash(conn: sqlite3.Connection, file_hash: str) -> bool:
    if not file_hash:
        return False
    return (
        conn.execute("SELECT 1 FROM sessions WHERE file_hash=? LIMIT 1", (file_hash,)).fetchone()
        is not None
    )


def session_exists_by_uuid(conn: sqlite3.Connection, session_uuid: str) -> bool:
    if not session_uuid:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM sessions WHERE external_session_id=? LIMIT 1", (session_uuid,)
        ).fetchone()
        is not None
    )


def _engine() -> RedactionEngine:
    # A non-existent file selects bundled defaults. The orchestrator passes its
    # vault-specific engine, but direct callers remain fail-closed and scrubbed.
    return RedactionEngine(Path("/__vimgym_bundled_policy__"))


def _flatten(value: Any) -> str:
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


def _canonical_block(block: Any) -> tuple[str, str, str | None, dict[str, Any]]:
    if not isinstance(block, dict):
        block = {"type": "unknown", "value": block}
    native = str(block.get("type") or "unknown")
    visibility = "visible"
    if native == "text":
        kind = "text"
        text = str(block.get("text") or "")
    elif native == "tool_use":
        kind = "tool_call"
        text = _flatten(block.get("input"))
    elif native == "tool_result":
        kind = "tool_result"
        text = _flatten(block.get("content"))
    elif native == "image":
        kind = "attachment"
        text = None
    elif native == "thinking":
        kind = "omitted"
        visibility = "hidden"
        text = None
        block = {"type": "thinking", "omitted": True}
    else:
        kind = "unknown_event"
        visibility = "hidden"
        text = None
    return kind, visibility, text, block


def upsert_session(
    conn: sqlite3.Connection,
    session: "ParsedSession",
    metadata: "SessionMetadata",
    summary: str = "",
    *,
    redaction_engine: RedactionEngine | None = None,
) -> str:
    """Atomically replace one session revision with redacted canonical rows.

    This function accepts the legacy ``ParsedSession`` only as an internal
    compatibility adapter. It recursively redacts every content-bearing value;
    storage never persists ``session.raw_jsonl`` or the generated v0.1 summary.
    """
    del summary  # generated summaries are intentionally removed in schema v2
    engine = redaction_engine or _engine()
    provider = "claude_code"
    external_id = str(session.session_uuid)
    internal_id = _id("session", provider, external_id)
    now = _now()
    started_at = str(session.started_at or now)
    source_id = str(getattr(session, "source_id", None) or provider)
    raw_source_path = str(session.source_path)
    source_root = str(Path(raw_source_path).parent)
    redacted_source_path = engine.redact_text(raw_source_path)
    cwd = engine.redact_text(str(session.cwd or ""))
    project_name = engine.redact_text(str(metadata.project_name or ""))
    workspace_id = _id("workspace", provider, cwd)

    title = engine.redact_text(str(session.ai_title or "")).strip()
    user_text = engine.redact_text(session.user_messages_text or "")
    if not title:
        first = next((line.strip() for line in user_text.splitlines() if line.strip()), "")
        title = first[:120] or "Untitled session"

    tools = engine.redact_value(list(session.tools_used))
    files = engine.redact_value(list(session.files_modified))
    existing = conn.execute("SELECT revision FROM sessions WHERE id=?", (internal_id,)).fetchone()
    revision = (int(existing["revision"]) + 1) if existing else 1

    stat_size = 0
    stat_mtime = 0
    try:
        stat = Path(raw_source_path).stat()
        stat_size = stat.st_size
        stat_mtime = stat.st_mtime_ns
    except OSError:
        pass

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO sources(
                id, provider, name, root_path, enabled, health,
                created_at, updated_at, last_seen_at
            ) VALUES (?, ?, 'Claude Code', ?, 1, 'ok', ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                provider=excluded.provider, root_path=excluded.root_path,
                health='ok', updated_at=excluded.updated_at,
                last_seen_at=excluded.last_seen_at
            """,
            (source_id, provider, source_root, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO workspaces(id,provider,canonical_cwd,display_name,created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name
            """,
            (workspace_id, provider, cwd, project_name or Path(cwd).name or "Unknown", now),
        )

        # FTS5 has no foreign-key cascades, so clear its rows before replacing
        # relational messages and blocks.
        delete_fts_for_blocks(conn, "session_id=?", (internal_id,))
        conn.execute("DELETE FROM messages WHERE session_id=?", (internal_id,))
        conn.execute("DELETE FROM session_tools WHERE session_id=?", (internal_id,))
        conn.execute("DELETE FROM session_files WHERE session_id=?", (internal_id,))

        diagnostics = [engine.redact_text(item) for item in session.parse_errors[:100]]
        health = "degraded" if diagnostics else "ok"
        conn.execute(
            """
            INSERT INTO sessions(
                id, provider, external_session_id, session_uuid, source_id, kind,
                lifecycle, originator, client_name, client_version, title,
                title_source, workspace_id, slug, source_path, project_dir,
                project_name, cwd, git_branch, entrypoint, claude_version,
                permission_mode, started_at, updated_at, ended_at, duration_secs,
                message_count, user_message_count, asst_message_count,
                tool_use_count, has_subagents, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, ai_title, summary,
                tools_used, files_modified, parser_version,
                redaction_policy_hash, revision, health, diagnostics_json,
                backed_up_at, last_seen_at, indexed_at, file_hash,
                file_size_bytes, schema_version
            ) VALUES (
                ?, ?, ?, ?, ?, 'user', 'active', 'Claude Code', 'claude_code', ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2
            )
            ON CONFLICT(id) DO UPDATE SET
                source_id=excluded.source_id, lifecycle=excluded.lifecycle,
                client_version=excluded.client_version, title=excluded.title,
                title_source=excluded.title_source, workspace_id=excluded.workspace_id,
                slug=excluded.slug, source_path=excluded.source_path,
                project_dir=excluded.project_dir, project_name=excluded.project_name,
                cwd=excluded.cwd, git_branch=excluded.git_branch,
                entrypoint=excluded.entrypoint, claude_version=excluded.claude_version,
                permission_mode=excluded.permission_mode, started_at=excluded.started_at,
                updated_at=excluded.updated_at, ended_at=excluded.ended_at,
                duration_secs=excluded.duration_secs, message_count=excluded.message_count,
                user_message_count=excluded.user_message_count,
                asst_message_count=excluded.asst_message_count,
                tool_use_count=excluded.tool_use_count,
                has_subagents=excluded.has_subagents, input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens,
                cache_read_tokens=excluded.cache_read_tokens,
                cache_write_tokens=excluded.cache_write_tokens,
                ai_title=excluded.ai_title, summary=NULL,
                tools_used=excluded.tools_used, files_modified=excluded.files_modified,
                parser_version=excluded.parser_version,
                redaction_policy_hash=excluded.redaction_policy_hash,
                revision=excluded.revision, health=excluded.health,
                diagnostics_json=excluded.diagnostics_json,
                backed_up_at=excluded.backed_up_at, last_seen_at=excluded.last_seen_at,
                indexed_at=excluded.indexed_at, file_hash=excluded.file_hash,
                file_size_bytes=excluded.file_size_bytes, schema_version=2
            """,
            (
                internal_id,
                provider,
                external_id,
                external_id,
                source_id,
                engine.redact_text(str(session.claude_version or "")) or None,
                title,
                "provider" if session.ai_title else "fallback",
                workspace_id,
                engine.redact_text(str(session.slug or "")) or None,
                redacted_source_path,
                engine.redact_text(str(session.project_dir or "")),
                project_name,
                cwd or None,
                engine.redact_text(str(session.git_branch or "")) or None,
                engine.redact_text(str(session.entrypoint or "")) or None,
                engine.redact_text(str(session.claude_version or "")) or None,
                engine.redact_text(str(session.permission_mode or "")) or None,
                started_at,
                now,
                session.ended_at,
                metadata.duration_secs,
                metadata.message_count,
                metadata.user_message_count,
                metadata.asst_message_count,
                metadata.tool_use_count,
                1 if session.has_subagents else 0,
                session.input_tokens,
                session.output_tokens,
                session.cache_read_tokens,
                session.cache_write_tokens,
                title,
                json.dumps(tools, ensure_ascii=False),
                json.dumps(files, ensure_ascii=False),
                PARSER_VERSION,
                engine.policy_hash,
                revision,
                health,
                json.dumps(diagnostics, ensure_ascii=False),
                now,
                now,
                now,
                session.file_hash,
                stat_size,
            ),
        )

        for sequence, message in enumerate(session.messages, 1):
            external_message = str(message.uuid or f"line-{sequence}")
            message_id = _id("message", internal_id, external_message)
            try:
                native_blocks = json.loads(message.content_json or "[]")
            except json.JSONDecodeError:
                native_blocks = []
            if not isinstance(native_blocks, list):
                native_blocks = []
            redacted_blocks = engine.redact_value(native_blocks)
            redacted_tools = engine.redact_value(list(message.tool_names))
            conn.execute(
                """
                INSERT INTO messages(
                    id, session_id, session_uuid, external_message_id, sequence,
                    source_line, type, role, timestamp, parent_uuid, has_tool_use,
                    has_thinking, has_image, tool_names, content_json, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    internal_id,
                    external_id,
                    external_message,
                    sequence,
                    sequence,
                    message.type,
                    message.role,
                    message.timestamp,
                    engine.redact_text(str(message.parent_uuid or "")) or None,
                    1 if message.has_tool_use else 0,
                    1 if message.has_thinking else 0,
                    1 if message.has_image else 0,
                    json.dumps(redacted_tools, ensure_ascii=False),
                    json.dumps(redacted_blocks, ensure_ascii=False),
                    revision,
                ),
            )

            for block_index, native_block in enumerate(redacted_blocks):
                kind, visibility, text, block = _canonical_block(native_block)
                block_id = _id("block", message_id, str(block_index))
                encoded = json.dumps(block, ensure_ascii=False)
                cursor = conn.execute(
                    """
                    INSERT INTO message_blocks(
                        id, message_id, session_id, sequence, block_index, kind,
                        visibility, text_content, data_json, name, call_id,
                        mime_type, is_error, original_bytes, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        block_id,
                        message_id,
                        internal_id,
                        sequence,
                        block_index,
                        kind,
                        visibility,
                        text,
                        encoded,
                        block.get("name"),
                        block.get("id") or block.get("tool_use_id"),
                        block.get("mime_type") or block.get("media_type"),
                        1 if block.get("is_error") else 0,
                        len(encoded.encode("utf-8")),
                        revision,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("message block insert did not return a row id")
                insert_block_fts(
                    conn,
                    int(cursor.lastrowid),
                    text,
                    encoded,
                    visible=visibility == "visible",
                )

        for tool in tools:
            conn.execute(
                "INSERT INTO session_tools(session_id,tool_name,use_count) VALUES(?,?,1)",
                (internal_id, str(tool)),
            )
        for file_path in files:
            conn.execute(
                "INSERT INTO session_files(session_id,path,operation) VALUES(?,?,'modified')",
                (internal_id, str(file_path)),
            )

        artifact_id = _id("artifact", source_id, raw_source_path)
        status = "degraded" if diagnostics else "imported"
        conn.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, session_id, relative_path, lifecycle, size_bytes,
                mtime_ns, content_sha256, parser_version, redaction_policy_hash,
                processed_bytes, processed_lines, status, diagnostics_json,
                last_seen_at, last_indexed_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id,relative_path) DO UPDATE SET
                session_id=excluded.session_id, size_bytes=excluded.size_bytes,
                mtime_ns=excluded.mtime_ns, content_sha256=excluded.content_sha256,
                parser_version=excluded.parser_version,
                redaction_policy_hash=excluded.redaction_policy_hash,
                processed_bytes=excluded.processed_bytes,
                processed_lines=excluded.processed_lines, status=excluded.status,
                diagnostics_json=excluded.diagnostics_json,
                last_seen_at=excluded.last_seen_at,
                last_indexed_at=excluded.last_indexed_at
            """,
            (
                artifact_id,
                source_id,
                internal_id,
                engine.redact_text(
                    str(Path(Path(raw_source_path).parent.name) / Path(raw_source_path).name)
                ),
                stat_size,
                stat_mtime,
                session.file_hash,
                PARSER_VERSION,
                engine.policy_hash,
                stat_size,
                len(session.messages),
                status,
                json.dumps(diagnostics, ensure_ascii=False),
                now,
                now,
            ),
        )

        _upsert_project(conn, session, metadata, cwd=cwd, project_name=project_name)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return internal_id


def _upsert_project(
    conn: sqlite3.Connection,
    session: "ParsedSession",
    metadata: "SessionMetadata",
    *,
    cwd: str | None = None,
    project_name: str | None = None,
) -> None:
    name = project_name if project_name is not None else metadata.project_name
    row = conn.execute(
        """
        SELECT COUNT(*) session_count, MAX(started_at) last_active,
               COALESCE(SUM(duration_secs),0) total_duration_secs,
               COALESCE(SUM(input_tokens),0) total_input_tokens,
               COALESCE(SUM(output_tokens),0) total_output_tokens
        FROM sessions WHERE project_name=?
        """,
        (name,),
    ).fetchone()
    conn.execute(
        """
        INSERT OR REPLACE INTO projects(
            project_name,project_dir,cwd,session_count,last_active,
            total_duration_secs,total_input_tokens,total_output_tokens
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            name,
            session.project_dir,
            cwd if cwd is not None else session.cwd,
            row["session_count"],
            row["last_active"],
            row["total_duration_secs"],
            row["total_input_tokens"],
            row["total_output_tokens"],
        ),
    )
