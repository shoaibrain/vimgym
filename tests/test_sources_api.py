"""Sprint 5 — /api/config and /api/config/sources endpoints."""

import pytest
from fastapi.testclient import TestClient

from vimgym.config import AppConfig, SourceConfig
from vimgym.db import init_db
from vimgym.server import create_app


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig(
        vault_dir=tmp_path,
        sources=[
            SourceConfig(id="claude_code", name="Claude Code", type="claude_code",
                         path=str(tmp_path / "fake-claude"), enabled=True, auto_detected=True),
            SourceConfig(id="cursor", name="Cursor", type="unknown",
                         path=str(tmp_path / "fake-cursor"), enabled=False, auto_detected=True),
        ],
    )
    (tmp_path / "fake-claude").mkdir()
    init_db(cfg.db_path)
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c, cfg


def test_get_config(client):
    c, _ = client
    r = c.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["server_host"] == "127.0.0.1"
    assert body["schema_version"] == 1


def test_get_sources(client):
    c, _ = client
    r = c.get("/api/config/sources")
    assert r.status_code == 200
    sources = r.json()["sources"]
    assert len(sources) == 2

    by_id = {s["id"]: s for s in sources}
    assert by_id["claude_code"]["enabled"] is True
    assert by_id["claude_code"]["parser_available"] is True
    assert by_id["claude_code"]["exists"] is True
    assert by_id["cursor"]["enabled"] is False
    assert by_id["cursor"]["parser_available"] is False


def test_patch_source_toggle(client):
    c, cfg = client
    r = c.patch("/api/config/sources/claude_code", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    # In-memory config object updated
    assert next(s for s in cfg.sources if s.id == "claude_code").enabled is False

    # Re-enable
    r = c.patch("/api/config/sources/claude_code", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_patch_unknown_source_404(client):
    c, _ = client
    r = c.patch("/api/config/sources/nope", json={"enabled": True})
    assert r.status_code == 404
