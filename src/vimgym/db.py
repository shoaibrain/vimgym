"""SQLite database init, schema, and connection management."""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    session_uuid    TEXT NOT NULL UNIQUE,
    slug            TEXT,

    source_path     TEXT NOT NULL,
    project_dir     TEXT NOT NULL,
    project_name    TEXT NOT NULL,

    cwd             TEXT,
    git_branch      TEXT,
    entrypoint      TEXT,
    claude_version  TEXT,
    permission_mode TEXT,

    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    duration_secs   INTEGER,

    message_count       INTEGER DEFAULT 0,
    user_message_count  INTEGER DEFAULT 0,
    asst_message_count  INTEGER DEFAULT 0,
    tool_use_count      INTEGER DEFAULT 0,
    has_subagents       INTEGER DEFAULT 0,

    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_write_tokens  INTEGER DEFAULT 0,

    ai_title            TEXT,
    summary             TEXT,
    tools_used          TEXT,
    files_modified      TEXT,

    backed_up_at    TEXT NOT NULL,
    file_hash       TEXT NOT NULL,
    file_size_bytes INTEGER,
    schema_version  INTEGER DEFAULT 1,
    source_id       TEXT DEFAULT 'claude_code'
);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_uuid UNINDEXED,
    project_name,
    git_branch,
    ai_title,
    summary,
    user_messages,
    asst_messages,
    tools_used,
    files_modified,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS sessions_raw (
    session_uuid    TEXT PRIMARY KEY REFERENCES sessions(session_uuid) ON DELETE CASCADE,
    raw_jsonl       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    session_uuid    TEXT NOT NULL REFERENCES sessions(session_uuid) ON DELETE CASCADE,
    parent_uuid     TEXT,
    type            TEXT NOT NULL,
    role            TEXT NOT NULL,
    timestamp       TEXT,
    has_tool_use    INTEGER DEFAULT 0,
    has_thinking    INTEGER DEFAULT 0,
    has_image       INTEGER DEFAULT 0,
    tool_names      TEXT,
    content_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_uuid);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);

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

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_name);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_branch  ON sessions(git_branch);
CREATE INDEX IF NOT EXISTS idx_sessions_hash    ON sessions(file_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_uuid    ON sessions(session_uuid);
CREATE INDEX IF NOT EXISTS idx_sessions_source  ON sessions(source_id);
"""


_local = threading.local()


def _check_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_test USING fts5(x)")
        conn.execute("DROP TABLE _fts5_test")
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            "SQLite FTS5 not available. Python was built without FTS5 support."
        ) from e


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row


def init_db(db_path: Path) -> None:
    """Create vault directory, initialize schema, set perms.

    Idempotent: safe to call multiple times.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(db_path.parent, 0o700)
    except OSError:
        pass

    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        _configure(conn)
        _check_fts5(conn)
        conn.executescript(SCHEMA_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Return a thread-local sqlite3 connection (WAL handles concurrency)."""
    db_path = Path(db_path)
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    key = str(db_path.resolve())
    conn = cache.get(key)
    if conn is None:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        _configure(conn)
        cache[key] = conn
        _local.conns = cache
    return conn


def close_all_connections() -> None:
    """Close any thread-local connections (test cleanup)."""
    cache = getattr(_local, "conns", None) or {}
    for conn in cache.values():
        try:
            conn.close()
        except Exception:
            pass
    _local.conns = {}
