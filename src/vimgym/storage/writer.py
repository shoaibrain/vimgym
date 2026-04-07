"""Session writer — bulk inserts a parsed session into all relevant tables."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vimgym.pipeline.metadata import SessionMetadata
    from vimgym.pipeline.parser import ParsedSession


def session_exists_by_hash(conn: sqlite3.Connection, file_hash: str) -> bool:
    if not file_hash:
        return False
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE file_hash = ? LIMIT 1", (file_hash,)
    ).fetchone()
    return row is not None


def session_exists_by_uuid(conn: sqlite3.Connection, session_uuid: str) -> bool:
    if not session_uuid:
        return False
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE session_uuid = ? LIMIT 1", (session_uuid,)
    ).fetchone()
    return row is not None


def _composite_id(session_uuid: str, started_at: str | None) -> str:
    h = hashlib.sha256()
    h.update(session_uuid.encode("utf-8"))
    h.update((started_at or "").encode("utf-8"))
    return h.hexdigest()


def upsert_session(
    conn: sqlite3.Connection,
    session: "ParsedSession",
    metadata: "SessionMetadata",
    summary: str,
) -> str:
    """Insert/replace a session and all related rows in a single transaction.

    Returns the session's composite id.
    """
    composite_id = _composite_id(session.session_uuid, session.started_at)
    now = datetime.now(timezone.utc).isoformat()
    started_at = session.started_at or now

    file_size = 0
    try:
        file_size = Path(session.source_path).stat().st_size
    except OSError:
        pass

    tools_json = json.dumps(session.tools_used)
    files_json = json.dumps(session.files_modified)
    source_id = getattr(session, "source_id", None) or "claude_code"

    # Single transaction for atomicity.
    try:
        conn.execute("BEGIN")

        conn.execute(
            """
            INSERT OR REPLACE INTO sessions (
                id, session_uuid, slug,
                source_path, project_dir, project_name,
                cwd, git_branch, entrypoint, claude_version, permission_mode,
                started_at, ended_at, duration_secs,
                message_count, user_message_count, asst_message_count,
                tool_use_count, has_subagents,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                ai_title, summary, tools_used, files_modified,
                backed_up_at, file_hash, file_size_bytes, schema_version,
                source_id
            ) VALUES (
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, 1,
                ?
            )
            """,
            (
                composite_id, session.session_uuid, session.slug,
                session.source_path, session.project_dir, metadata.project_name,
                session.cwd, session.git_branch, session.entrypoint,
                session.claude_version, session.permission_mode,
                started_at, session.ended_at, metadata.duration_secs,
                metadata.message_count, metadata.user_message_count,
                metadata.asst_message_count,
                metadata.tool_use_count, 1 if session.has_subagents else 0,
                session.input_tokens, session.output_tokens,
                session.cache_read_tokens, session.cache_write_tokens,
                session.ai_title, summary, tools_json, files_json,
                now, session.file_hash, file_size,
                source_id,
            ),
        )

        conn.execute(
            "INSERT OR REPLACE INTO sessions_raw (session_uuid, raw_jsonl) VALUES (?, ?)",
            (session.session_uuid, session.raw_jsonl),
        )

        conn.execute(
            "DELETE FROM sessions_fts WHERE session_uuid = ?", (session.session_uuid,)
        )
        conn.execute(
            """
            INSERT INTO sessions_fts (
                session_uuid, project_name, git_branch, ai_title, summary,
                user_messages, asst_messages, tools_used, files_modified
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_uuid,
                metadata.project_name,
                session.git_branch or "",
                session.ai_title or "",
                summary or "",
                session.user_messages_text or "",
                session.asst_messages_text or "",
                " ".join(session.tools_used),
                " ".join(session.files_modified),
            ),
        )

        conn.execute(
            "DELETE FROM messages WHERE session_uuid = ?", (session.session_uuid,)
        )
        if session.messages:
            conn.executemany(
                """
                INSERT INTO messages (
                    id, session_uuid, parent_uuid, type, role, timestamp,
                    has_tool_use, has_thinking, has_image, tool_names, content_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"{session.session_uuid}:{m.uuid}",
                        session.session_uuid,
                        m.parent_uuid,
                        m.type,
                        m.role,
                        m.timestamp,
                        1 if m.has_tool_use else 0,
                        1 if m.has_thinking else 0,
                        1 if m.has_image else 0,
                        json.dumps(m.tool_names),
                        m.content_json,
                    )
                    for m in session.messages
                ],
            )

        _upsert_project(conn, session, metadata)

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return composite_id


def _upsert_project(
    conn: sqlite3.Connection,
    session: "ParsedSession",
    metadata: "SessionMetadata",
) -> None:
    """Recompute project aggregates from sessions table (correct under updates)."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS session_count,
            MAX(started_at) AS last_active,
            COALESCE(SUM(duration_secs), 0) AS total_duration_secs,
            COALESCE(SUM(input_tokens), 0)  AS total_input_tokens,
            COALESCE(SUM(output_tokens), 0) AS total_output_tokens
        FROM sessions
        WHERE project_name = ?
        """,
        (metadata.project_name,),
    ).fetchone()

    conn.execute(
        """
        INSERT OR REPLACE INTO projects (
            project_name, project_dir, cwd,
            session_count, last_active,
            total_duration_secs, total_input_tokens, total_output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metadata.project_name,
            session.project_dir,
            session.cwd,
            row["session_count"],
            row["last_active"],
            row["total_duration_secs"],
            row["total_input_tokens"],
            row["total_output_tokens"],
        ),
    )
