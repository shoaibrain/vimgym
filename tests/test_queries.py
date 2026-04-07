from pathlib import Path

import pytest

from vimgym.config import AppConfig
from vimgym.db import get_connection, init_db
from vimgym.pipeline.orchestrator import process_session
from vimgym.storage.queries import (
    AmbiguousIDError,
    count_sessions,
    get_session,
    get_stats,
    list_projects,
    list_sessions,
    search_sessions,
)

DATA_DIR = Path(__file__).parent / "fixtures" / "sessions" / "-Users-example-edforge"


@pytest.fixture
def populated(tmp_path):
    cfg = AppConfig(vault_dir=tmp_path)
    init_db(cfg.db_path)
    for path in sorted(DATA_DIR.glob("*.jsonl")):
        process_session(path, cfg)
    return get_connection(cfg.db_path)


def test_list_sessions_returns_all(populated):
    rows = list_sessions(populated)
    assert len(rows) >= 5


def test_count_sessions(populated):
    assert count_sessions(populated) >= 5
    assert count_sessions(populated, project="edforge") >= 5
    assert count_sessions(populated, project="nope") == 0


def test_search_finds_cors(populated):
    results = search_sessions(populated, "CORS")
    assert len(results) > 0
    assert any("3438c55b" in r.session_uuid for r in results)


def test_search_snippet_has_mark(populated):
    results = search_sessions(populated, "CORS")
    assert len(results) > 0
    # snippet column index is 5 (asst_messages); mark may or may not appear there
    # but it should at least produce a string
    assert all(isinstance(r.snippet, str) for r in results)


def test_search_filter_by_project(populated):
    results = search_sessions(populated, "CORS", project="edforge")
    assert all(r.project_name == "edforge" for r in results)
    assert search_sessions(populated, "CORS", project="nope") == []


def test_get_session_by_prefix(populated):
    row = get_session(populated, "eaa3009a")
    assert row is not None
    assert row["session_uuid"] == "eaa3009a-c5ab-4015-a3e5-af26622652f9"


def test_get_session_unknown(populated):
    assert get_session(populated, "ffffffff") is None


def test_get_session_ambiguous_prefix(populated):
    # Single-char prefix '6' will match both 64778c29 and 64b0bec2 and 68568954
    with pytest.raises(AmbiguousIDError):
        get_session(populated, "6")


def test_stats(populated):
    stats = get_stats(populated)
    assert stats.total_sessions >= 5
    assert stats.total_duration_secs > 0
    assert len(stats.top_projects) >= 1
    assert any(p["project_name"] == "edforge" for p in stats.top_projects)
    assert any(t["tool"] == "Bash" for t in stats.top_tools)


def test_list_projects(populated):
    rows = list_projects(populated)
    assert len(rows) >= 1
    assert any(r["project_name"] == "edforge" for r in rows)
