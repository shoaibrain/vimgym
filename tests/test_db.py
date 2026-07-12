import os


from vimgym.db import init_db, get_connection


def test_init_creates_file_with_perms(tmp_path):
    db = tmp_path / "vault.db"
    init_db(db)
    assert db.exists()
    mode = os.stat(db).st_mode & 0o777
    assert mode == 0o600


def test_all_tables_exist(tmp_path):
    db = tmp_path / "vault.db"
    init_db(db)
    conn = get_connection(db)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')").fetchall()
    names = {r["name"] for r in rows}
    for required in (
        "schema_migrations",
        "sources",
        "workspaces",
        "sessions",
        "source_artifacts",
        "messages",
        "message_blocks",
        "message_fts",
        "session_tools",
        "session_files",
        "projects",
        "config",
    ):
        assert required in names, f"missing table {required}"


def test_wal_mode(tmp_path):
    db = tmp_path / "vault.db"
    init_db(db)
    conn = get_connection(db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_idempotent(tmp_path):
    db = tmp_path / "vault.db"
    init_db(db)
    init_db(db)  # second call must not raise
    conn = get_connection(db)
    n = conn.execute("SELECT COUNT(*) FROM config WHERE key='schema_version'").fetchone()[0]
    assert n == 1


def test_schema_version_seeded(tmp_path):
    db = tmp_path / "vault.db"
    init_db(db)
    conn = get_connection(db)
    v = conn.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()[0]
    assert v == "2"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
