from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vimgym.config import AppConfig
from vimgym.db import init_db
from vimgym.pipeline.orchestrator import process_session
from vimgym.server import create_app

DATA_DIR = Path(__file__).parent / "fixtures" / "sessions" / "-Users-example-edforge"


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(vault_dir=tmp_path)
    init_db(cfg.db_path)
    for path in sorted(DATA_DIR.glob("*.jsonl")):
        process_session(path, cfg)
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["sessions"] >= 5


def test_list_sessions(client):
    r = client.get("/api/sessions?limit=3")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 5
    assert len(body["sessions"]) <= 3
    assert "session_uuid" in body["sessions"][0]
    # tools_used should be parsed as JSON list
    assert isinstance(body["sessions"][0].get("tools_used"), list)


def test_session_detail(client):
    r = client.get("/api/sessions/eaa3009a")
    assert r.status_code == 200
    body = r.json()
    assert body["session_uuid"] == "eaa3009a-c5ab-4015-a3e5-af26622652f9"
    assert body["ai_title"] is not None
    assert "CloudFormation" in body["ai_title"]
    assert isinstance(body["messages"], list)
    assert len(body["messages"]) > 0
    # content should be parsed JSON, not raw string
    assert isinstance(body["messages"][0]["content"], list)


def test_session_detail_404(client):
    r = client.get("/api/sessions/ffffffff")
    assert r.status_code == 404


def test_session_detail_ambiguous(client):
    r = client.get("/api/sessions/6")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "ambiguous_id"


def test_search_cors(client):
    r = client.get("/api/search?q=CORS")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] > 0
    assert any("3438c55b" in res["session_uuid"] for res in body["results"])


def test_search_hyphenated_query(client):
    """Hyphenated branch name must not break FTS5 (the escape we added)."""
    r = client.get("/api/search?q=fee-to-enrollment")
    assert r.status_code == 200
    assert isinstance(r.json()["results"], list)


def test_search_filter_by_project(client):
    r = client.get("/api/search?q=CORS&project=edforge")
    assert r.status_code == 200
    assert all(res["project_name"] == "edforge" for res in r.json()["results"])


def test_projects_endpoint(client):
    r = client.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    assert any(p["project_name"] == "edforge" for p in body)


def test_stats_endpoint(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_sessions"] >= 5
    assert body["total_duration_secs"] > 0
    assert any(t["tool"] == "Bash" for t in body["top_tools"])


def test_session_raw(client):
    r = client.get("/api/sessions/eaa3009a/raw")
    assert r.status_code == 200
    assert "session" in r.text.lower() or len(r.text) > 100
