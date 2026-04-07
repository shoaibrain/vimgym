from pathlib import Path

from vimgym.db import get_connection, init_db
from vimgym.pipeline.metadata import extract_metadata
from vimgym.pipeline.parser import parse_session
from vimgym.pipeline.summary import heuristic_summary
from vimgym.storage.writer import (
    session_exists_by_hash,
    session_exists_by_uuid,
    upsert_session,
)

DATA_DIR = Path(__file__).parent / "fixtures" / "sessions" / "-Users-example-edforge"


def _back_up(tmp_path, name):
    db = tmp_path / "vault.db"
    init_db(db)
    conn = get_connection(db)
    s = parse_session(DATA_DIR / name)
    meta = extract_metadata(s)
    summary = heuristic_summary(s)
    upsert_session(conn, s, meta, summary)
    conn.commit()
    return conn, s


def test_insert_populates_all_tables(tmp_path):
    conn, s = _back_up(tmp_path, "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl")
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions_raw").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions_fts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_dedup_helpers(tmp_path):
    conn, s = _back_up(tmp_path, "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl")
    assert session_exists_by_hash(conn, s.file_hash) is True
    assert session_exists_by_uuid(conn, s.session_uuid) is True
    assert session_exists_by_hash(conn, "deadbeef") is False
    assert session_exists_by_uuid(conn, "no-such-uuid") is False


def test_reinsert_idempotent(tmp_path):
    conn, s = _back_up(tmp_path, "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl")
    meta = extract_metadata(s)
    upsert_session(conn, s, meta, heuristic_summary(s))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions_fts").fetchone()[0] == 1


def test_fts_searchable_by_project(tmp_path):
    conn, _ = _back_up(tmp_path, "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl")
    rows = conn.execute(
        "SELECT session_uuid FROM sessions_fts WHERE sessions_fts MATCH ?",
        ("edforge",),
    ).fetchall()
    assert len(rows) == 1


def test_project_aggregates(tmp_path):
    conn, _ = _back_up(tmp_path, "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl")
    row = conn.execute("SELECT * FROM projects WHERE project_name='edforge'").fetchone()
    assert row is not None
    assert row["session_count"] == 1
    assert row["last_active"] is not None
