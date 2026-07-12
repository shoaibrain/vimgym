from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vimgym.config import AppConfig, SourceConfig
from vimgym.db import get_connection, init_db
from vimgym.ingestion import candidate_for_path, ingest_artifact
from vimgym.server import create_app
from vimgym.storage.queries import InvalidCursorError, list_sessions_page


def _malicious_vault(tmp_path: Path) -> tuple[AppConfig, str]:
    root = tmp_path / "source"
    root.mkdir()
    external_id = "88888888-8888-4888-8888-888888888888"
    path = root / f"{external_id}.jsonl"
    payload = '<img src=x onerror="window.pwned=1"> pwned'
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "m1",
                "sessionId": external_id,
                "message": {"role": "user", "content": [{"type": "text", "text": payload}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    source = SourceConfig("claude_code", "Claude", "claude_code", str(root))
    cfg = AppConfig(vault_dir=tmp_path / "vault", sources=[source])
    init_db(cfg.db_path)
    result = ingest_artifact(candidate_for_path(source, path), cfg)
    assert result.session_id is not None
    return cfg, result.session_id


def test_loopback_host_origin_and_csp_guards(tmp_path: Path) -> None:
    cfg, _ = _malicious_vault(tmp_path)
    with TestClient(create_app(cfg)) as client:
        rejected_host = client.get("/health", headers={"host": "evil.example"})
        assert rejected_host.status_code == 400
        assert "default-src 'self'" in rejected_host.headers["content-security-policy"]
        rejected_origin = client.get("/health", headers={"origin": "https://evil.example"})
        assert rejected_origin.status_code == 403
        assert rejected_origin.headers["cache-control"] == "no-store"
        response = client.get(
            "/health",
            headers={
                "host": f"127.0.0.1:{cfg.server_port}",
                "origin": f"http://127.0.0.1:{cfg.server_port}",
            },
        )
        assert response.status_code == 200
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "object-src 'none'" in csp
        assert "unsafe-inline" not in csp
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws", headers={"host": "evil.example", "origin": "https://evil.example"}
            ):
                pass

    with pytest.raises(ValueError, match="only bind"):
        create_app(AppConfig(vault_dir=tmp_path / "other", server_host="0.0.0.0"))


def test_search_returns_structured_text_snippets_not_html_markup(tmp_path: Path) -> None:
    cfg, session_id = _malicious_vault(tmp_path)
    with TestClient(create_app(cfg)) as client:
        response = client.get("/api/search?q=pwned")
        assert response.status_code == 200
        result = response.json()["items"][0]
        assert result["snippet_parts"]
        assert any(part["matched"] for part in result["snippet_parts"])
        assert "<mark>" not in response.text
        messages = client.get(f"/api/sessions/{session_id}/messages").json()["items"]
        assert "<img" in messages[0]["blocks"][0]["text"]


def test_export_filename_cannot_inject_response_headers(tmp_path: Path) -> None:
    cfg, session_id = _malicious_vault(tmp_path)
    conn = get_connection(cfg.db_path)
    conn.execute("UPDATE sessions SET slug=? WHERE id=?", ('evil"\r\nX-Evil: yes', session_id))
    conn.commit()

    with TestClient(create_app(cfg)) as client:
        response = client.get(f"/api/sessions/{session_id}/export?format=markdown")

    assert response.status_code == 200
    assert "x-evil" not in response.headers
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert 'filename="evil-X-Evil-yes-' in disposition


def test_session_keyset_cursor_has_no_duplicates_for_equal_timestamps(tmp_path: Path) -> None:
    cfg = AppConfig(vault_dir=tmp_path)
    init_db(cfg.db_path)
    conn = get_connection(cfg.db_path)
    now = "2026-01-01T00:00:00Z"
    conn.execute(
        """
        INSERT INTO sources(id,provider,name,root_path,created_at,updated_at)
        VALUES('codex_active','codex','Codex','/tmp/codex',?,?)
        """,
        (now, now),
    )
    for index in range(205):
        identity = f"00000000-0000-4000-8000-{index:012d}"
        conn.execute(
            """
            INSERT INTO sessions(
                id,provider,external_session_id,source_id,kind,lifecycle,
                source_path,started_at,redaction_policy_hash,backed_up_at
            ) VALUES(?, 'codex', ?, 'codex_active', 'user', 'active', ?, ?, 'policy', ?)
            """,
            (identity, identity, f"{identity}.jsonl", now, now),
        )
    conn.commit()

    seen: list[str] = []
    cursor = None
    while True:
        rows, cursor = list_sessions_page(conn, cursor=cursor, limit=37)
        seen.extend(row["id"] for row in rows)
        if cursor is None:
            break
    assert len(seen) == 205
    assert len(set(seen)) == 205

    _, cursor = list_sessions_page(conn, provider="codex", limit=1)
    assert cursor is not None
    with pytest.raises(InvalidCursorError):
        list_sessions_page(conn, provider="claude_code", cursor=cursor)
    padded = cursor + "=" * (-len(cursor) % 4)
    malformed_payload = json.loads(base64.urlsafe_b64decode(padded))
    malformed_payload.pop("i")
    malformed = (
        base64.urlsafe_b64encode(json.dumps(malformed_payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(InvalidCursorError):
        list_sessions_page(conn, provider="codex", cursor=malformed)
    with pytest.raises(InvalidCursorError):
        list_sessions_page(conn, provider="codex", cursor="A" * 5000)
