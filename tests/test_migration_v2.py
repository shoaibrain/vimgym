from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import vimgym.db as db_module
from vimgym.config import AppConfig, SourceConfig
from vimgym.db import FutureSchemaError, close_all_connections, get_connection, init_db
from vimgym.providers import deterministic_session_id
from vimgym.watcher import backfill


V1_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, session_uuid TEXT NOT NULL UNIQUE, slug TEXT,
    source_path TEXT NOT NULL, project_dir TEXT NOT NULL, project_name TEXT NOT NULL,
    cwd TEXT, git_branch TEXT, entrypoint TEXT, claude_version TEXT,
    permission_mode TEXT, started_at TEXT NOT NULL, ended_at TEXT,
    duration_secs INTEGER, message_count INTEGER DEFAULT 0,
    user_message_count INTEGER DEFAULT 0, asst_message_count INTEGER DEFAULT 0,
    tool_use_count INTEGER DEFAULT 0, has_subagents INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    ai_title TEXT, summary TEXT, tools_used TEXT, files_modified TEXT,
    backed_up_at TEXT NOT NULL, file_hash TEXT NOT NULL, file_size_bytes INTEGER,
    schema_version INTEGER DEFAULT 1, source_id TEXT DEFAULT 'claude_code'
);
CREATE VIRTUAL TABLE sessions_fts USING fts5(
    session_uuid UNINDEXED, project_name, git_branch, ai_title, summary,
    user_messages, asst_messages, tools_used, files_modified
);
CREATE TABLE sessions_raw(session_uuid TEXT PRIMARY KEY, raw_jsonl TEXT NOT NULL);
CREATE TABLE messages(
    id TEXT PRIMARY KEY, session_uuid TEXT NOT NULL, parent_uuid TEXT,
    type TEXT NOT NULL, role TEXT NOT NULL, timestamp TEXT,
    has_tool_use INTEGER DEFAULT 0, has_thinking INTEGER DEFAULT 0,
    has_image INTEGER DEFAULT 0, tool_names TEXT, content_json TEXT NOT NULL
);
CREATE TABLE projects(
    project_name TEXT PRIMARY KEY, project_dir TEXT NOT NULL, cwd TEXT,
    session_count INTEGER, last_active TEXT, total_duration_secs INTEGER,
    total_input_tokens INTEGER, total_output_tokens INTEGER
);
CREATE TABLE config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO config VALUES('schema_version','1');
"""


def _v1_vault(path: Path, sentinel: str = "password=supersecret123") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V1_DDL)
    session_id = "77777777-7777-4777-8777-777777777777"
    values = (
        "legacy-internal",
        session_id,
        "legacy",
        f"/Users/example/{session_id}.jsonl",
        "-Users-example-repo",
        "repo",
        f"/Users/example/{sentinel}",
        sentinel,
        "claude-cli",
        "1.0",
        "default",
        "2025-01-01T00:00:00+00:00",
        "2025-01-01T00:00:01+00:00",
        1,
        1,
        1,
        0,
        0,
        0,
        1,
        2,
        0,
        0,
        sentinel,
        "generated summary",
        json.dumps(["Read"]),
        json.dumps([sentinel]),
        "2025-01-01T00:00:01+00:00",
        "hash",
        42,
        1,
        "claude_code",
    )
    conn.execute(
        "INSERT INTO sessions VALUES(" + ",".join("?" for _ in values) + ")",
        values,
    )
    conn.execute(
        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{session_id}:message-1",
            session_id,
            None,
            "user",
            "user",
            "2025-01-01T00:00:00+00:00",
            0,
            0,
            0,
            "[]",
            json.dumps([{"type": "text", "text": sentinel}]),
        ),
    )
    conn.execute("INSERT INTO sessions_raw VALUES(?,?)", (session_id, sentinel))
    conn.commit()
    conn.close()


def test_v1_migration_is_redacted_searchable_and_idempotent(tmp_path: Path) -> None:
    sentinel = "password=supersecret123"
    db_path = tmp_path / "vault.db"
    _v1_vault(db_path, sentinel)

    init_db(db_path)
    conn = get_connection(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    session = conn.execute("SELECT * FROM sessions").fetchone()
    assert session["id"] == deterministic_session_id(
        "claude_code", "77777777-7777-4777-8777-777777777777"
    )
    assert session["legacy_id"] == "legacy-internal"
    assert session["summary"] is None
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM message_blocks").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH 'REDACTED'"
        ).fetchone()[0]
        == 1
    )
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sessions_raw" not in tables
    assert "sessions_fts" not in tables
    assert conn.execute("SELECT status FROM source_artifacts").fetchone()[0] == "needs_reindex"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    close_all_connections()
    assert sentinel.encode() not in db_path.read_bytes()
    assert list((tmp_path / "backups").glob("pre-migration-v1-*.db"))

    init_db(db_path)
    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1


def test_v1_migration_derives_redacted_fallback_title(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    _v1_vault(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sessions SET ai_title=NULL")
    conn.commit()
    conn.close()

    init_db(db_path)
    session = (
        get_connection(db_path)
        .execute("SELECT title,title_source,summary FROM sessions")
        .fetchone()
    )
    assert session["title"] == "password=[REDACTED]"
    assert session["title_source"] == "fallback"
    assert session["summary"] is None


def test_migrated_source_reindex_atomically_replaces_legacy_revision(tmp_path: Path) -> None:
    db_path = tmp_path / "vault" / "vault.db"
    db_path.parent.mkdir()
    _v1_vault(db_path)
    external_id = "77777777-7777-4777-8777-777777777777"
    source_root = tmp_path / "claude-projects"
    artifact = source_root / "-Users-example-repo" / f"{external_id}.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "message-current",
                "sessionId": external_id,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "current parser replacement marker"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sessions SET source_path=?", (str(artifact),))
    conn.commit()
    conn.close()

    init_db(db_path)
    cfg = AppConfig(
        vault_dir=db_path.parent,
        sources=[SourceConfig("claude_code", "Claude Code", "claude_code", str(source_root))],
    )
    assert backfill(cfg) == 1

    conn = get_connection(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM message_blocks").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH 'replacement'"
        ).fetchone()[0]
        == 1
    )
    message_artifact = conn.execute("SELECT artifact_id FROM messages").fetchone()[0]
    block_artifact = conn.execute("SELECT artifact_id FROM message_blocks").fetchone()[0]
    stored_artifact = conn.execute("SELECT id FROM source_artifacts").fetchone()[0]
    assert message_artifact == block_artifact == stored_artifact


def test_future_schema_is_rejected_without_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version=99")
    conn.execute("CREATE TABLE future_data(value TEXT)")
    conn.execute("INSERT INTO future_data VALUES('preserve me')")
    conn.commit()
    conn.close()
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    with pytest.raises(FutureSchemaError):
        init_db(db_path)

    after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert after == before


def test_future_schema_connection_is_rejected_before_wal_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version=99")
    conn.execute("CREATE TABLE future_data(value TEXT)")
    conn.execute("INSERT INTO future_data VALUES('preserve me')")
    conn.commit()
    conn.close()
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    with pytest.raises(FutureSchemaError):
        get_connection(db_path)

    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert not db_path.with_name(db_path.name + "-wal").exists()
    assert not db_path.with_name(db_path.name + "-shm").exists()


@pytest.mark.parametrize(
    "phase",
    [
        "snapshot_created",
        "schema_created",
        "sessions_copied",
        "messages_copied",
        "derived_built",
        "precommit_validated",
        "legacy_removed",
        "committed",
        "postcommit_validated",
    ],
)
def test_migration_faults_retain_or_restore_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    db_path = tmp_path / "vault.db"
    sentinel = "password=supersecret123"
    _v1_vault(db_path, sentinel)

    def inject(name: str) -> None:
        if name == phase:
            raise RuntimeError(f"injected migration fault at {phase}")

    monkeypatch.setattr(db_module, "_migration_checkpoint", inject)
    with pytest.raises(Exception, match="injected migration fault"):
        init_db(db_path)
    close_all_connections()

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "provider" not in columns
    assert {"sessions_raw", "sessions_fts", "messages"}.issubset(tables)
    assert conn.execute("SELECT raw_jsonl FROM sessions_raw").fetchone()[0] == sentinel
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    assert list((tmp_path / "backups").glob("pre-migration-v1-*.db"))
