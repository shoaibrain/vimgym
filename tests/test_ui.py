"""Sprint 4 web UI tests — static file serving + new endpoints."""
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


# ── Static files ────────────────────────────────────────────────────────


def test_index_html_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "vimgym" in r.text.lower()


def test_style_css_served(client):
    r = client.get("/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert "--matrix" in r.text


def test_app_js_served(client):
    r = client.get("/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"].lower()
    assert "State" in r.text
    assert "connectWebSocket" in r.text
    assert "openCommandPalette" in r.text


def test_highlight_js_served(client):
    r = client.get("/vendor/highlight.min.js")
    assert r.status_code == 200
    # highlight.js sources include the keyword "hljs"
    assert "hljs" in r.text


# ── Design tokens & no-CDN guarantees ───────────────────────────────────


def test_design_tokens_in_style_css(client):
    r = client.get("/style.css")
    for token in ["--void-0", "--matrix", "--pink", "--cyan", "--amber", "--purple"]:
        assert token in r.text, f"missing design token: {token}"


def test_no_external_urls_in_app_js(client):
    r = client.get("/app.js")
    for cdn in ("cdnjs", "jsdelivr", "unpkg"):
        assert cdn not in r.text, f"external CDN reference found: {cdn}"


def test_no_cdn_in_index_html_except_fonts(client):
    """Only Google Fonts CDN allowed in index.html."""
    import re

    r = client.get("/")
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', r.text)
    for url in external:
        assert "fonts.googleapis.com" in url or "fonts.gstatic.com" in url, \
            f"unexpected external URL in index.html: {url}"


# ── New API endpoints ───────────────────────────────────────────────────


def test_stats_timeline_endpoint(client):
    r = client.get("/api/stats/timeline?since=365d")
    assert r.status_code == 200
    body = r.json()
    assert "days" in body
    assert isinstance(body["days"], list)
    if body["days"]:
        assert "date" in body["days"][0]
        assert "count" in body["days"][0]


def test_export_endpoint_returns_markdown(client):
    # Get any session UUID from the inbox.
    sessions = client.get("/api/sessions?limit=1").json()["sessions"]
    assert len(sessions) > 0
    uuid = sessions[0]["session_uuid"]

    r = client.get(f"/api/sessions/{uuid[:8]}/export?format=markdown")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "Content-Disposition" in r.headers
    assert "attachment" in r.headers["Content-Disposition"]
    assert ".md" in r.headers["Content-Disposition"]
    assert "# " in r.text  # has a markdown heading
    assert "## Conversation" in r.text


def test_export_404_for_unknown_session(client):
    r = client.get("/api/sessions/ffffffff/export")
    assert r.status_code == 404


def test_export_409_on_ambiguous_prefix(client):
    r = client.get("/api/sessions/6/export")
    assert r.status_code == 409
