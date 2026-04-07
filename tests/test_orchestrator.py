from pathlib import Path

from vimgym.config import AppConfig
from vimgym.db import get_connection, init_db
from vimgym.pipeline.orchestrator import process_session

DATA_DIR = Path(__file__).parent / "fixtures" / "sessions" / "-Users-example-edforge"


def _cfg(tmp_path):
    cfg = AppConfig(vault_dir=tmp_path)
    init_db(cfg.db_path)
    return cfg


def test_full_pipeline_inserts_to_db(tmp_path):
    cfg = _cfg(tmp_path)
    result = process_session(DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl", cfg)
    assert result.error is None
    assert result.skipped is False
    assert result.session_uuid == "eaa3009a-c5ab-4015-a3e5-af26622652f9"
    assert result.project_name == "edforge"

    conn = get_connection(cfg.db_path)
    row = conn.execute(
        "SELECT ai_title, project_name FROM sessions WHERE session_uuid = ?",
        (result.session_uuid,),
    ).fetchone()
    assert row is not None
    assert "CloudFormation" in row["ai_title"]


def test_dedup_skips_second_call(tmp_path):
    cfg = _cfg(tmp_path)
    path = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    r1 = process_session(path, cfg)
    r2 = process_session(path, cfg)
    assert r1.skipped is False
    assert r2.skipped is True


def test_all_sessions_pipeline(tmp_path):
    cfg = _cfg(tmp_path)
    for path in sorted(DATA_DIR.glob("*.jsonl")):
        result = process_session(path, cfg)
        assert result.error is None, f"Pipeline error on {path.name}: {result.error}"


def test_fts_search_after_backup(tmp_path):
    cfg = _cfg(tmp_path)
    process_session(DATA_DIR / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl", cfg)
    conn = get_connection(cfg.db_path)
    results = conn.execute(
        "SELECT session_uuid FROM sessions_fts WHERE sessions_fts MATCH 'CORS'"
    ).fetchall()
    assert len(results) > 0
    assert any("3438c55b" in r["session_uuid"] for r in results)
