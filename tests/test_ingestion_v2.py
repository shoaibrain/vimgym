from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from vimgym.backup import create_backup, restore_backup
from vimgym.config import AppConfig, SourceConfig, save_config
from vimgym.db import close_all_connections, get_connection, init_db
from vimgym.ingestion import candidate_for_path, ingest_artifact, reconcile_missing
from vimgym.pipeline.redact import RedactionEngine
from vimgym.providers import get_adapter
from vimgym.server import create_app
from vimgym.watcher import backfill


FIXTURES = Path(__file__).parent / "fixtures" / "providers"


def _config(vault: Path, sources: list[SourceConfig]) -> AppConfig:
    cfg = AppConfig(vault_dir=vault, sources=sources)
    init_db(cfg.db_path)
    save_config(cfg)
    return cfg


def test_backfill_ingests_claude_root_subagent_and_text_sidecar(tmp_path: Path) -> None:
    root = FIXTURES / "claude" / "projects"
    cfg = _config(
        tmp_path / "vault",
        [SourceConfig("claude_code", "Claude Code", "claude_code", str(root))],
    )
    assert backfill(cfg) == 3
    conn = get_connection(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    child = conn.execute("SELECT * FROM sessions WHERE kind='subagent'").fetchone()
    assert child is not None
    assert child["parent_session_id"] is not None
    assert (
        conn.execute(
            "SELECT has_subagents FROM sessions WHERE id=?", (child["parent_session_id"],)
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH 'whole'").fetchone()[
            0
        ]
        == 1
    )
    statuses = {row[0] for row in conn.execute("SELECT status FROM source_artifacts")}
    assert statuses == {"imported"}


def test_changed_artifact_replaces_one_revision_and_partial_tail_is_deferred(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    session_id = "55555555-5555-4555-8555-555555555555"
    path = root / f"{session_id}.jsonl"
    first = {
        "type": "user",
        "uuid": "m1",
        "sessionId": session_id,
        "message": {"role": "user", "content": [{"type": "text", "text": "first"}]},
    }
    path.write_text(json.dumps(first) + "\n" + '{"type":"assistant"', encoding="utf-8")
    source = SourceConfig("claude_code", "Claude", "claude_code", str(root))
    cfg = _config(tmp_path / "vault", [source])

    first_result = ingest_artifact(candidate_for_path(source, path), cfg)
    conn = get_connection(cfg.db_path)
    assert first_result.status == "degraded"
    assert conn.execute("SELECT message_count FROM sessions").fetchone()[0] == 1
    unchanged_partial = ingest_artifact(candidate_for_path(source, path), cfg)
    assert unchanged_partial.status == "degraded"
    assert unchanged_partial.changed is False
    assert conn.execute("SELECT status FROM source_artifacts").fetchone()[0] == "degraded"

    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            ',"uuid":"m2","sessionId":"55555555-5555-4555-8555-555555555555",'
            '"message":{"role":"assistant","content":[{"type":"text",'
            '"text":"second revision marker"}]}}\n'
        )
    second_result = ingest_artifact(candidate_for_path(source, path), cfg)
    row = conn.execute("SELECT message_count,revision FROM sessions").fetchone()
    assert second_result.status == "updated"
    assert tuple(row) == (2, 2)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH 'revision'"
        ).fetchone()[0]
        == 1
    )


def test_codex_active_to_archived_updates_one_identity(tmp_path: Path) -> None:
    source_root = FIXTURES / "codex"
    active = SourceConfig("codex_active", "Codex active", "codex", str(source_root / "sessions"))
    archived = SourceConfig(
        "codex_archived", "Codex archived", "codex", str(source_root / "archived_sessions")
    )
    cfg = _config(tmp_path / "vault", [active, archived])
    active_path = next(active.expanded_path.rglob("*22222222-2222-4222-8222-222222222222.jsonl"))
    archived_path = next(
        archived.expanded_path.rglob("*22222222-2222-4222-8222-222222222222.jsonl")
    )

    conn = get_connection(cfg.db_path)
    created = ingest_artifact(candidate_for_path(active, active_path), cfg)
    conn.execute("UPDATE source_artifacts SET status='degraded'")
    conn.execute("UPDATE sources SET health='degraded',diagnostic_count=1 WHERE id='codex_active'")
    conn.execute("UPDATE sessions SET health='degraded'")
    conn.commit()
    moved = ingest_artifact(candidate_for_path(archived, archived_path), cfg)
    assert created.session_id == moved.session_id
    assert moved.event_type == "session_archived"
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    session = conn.execute("SELECT lifecycle,message_count FROM sessions").fetchone()
    assert session["lifecycle"] == "archived"
    assert session["message_count"] == 3
    assert conn.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 1
    source_health = {
        row["id"]: (row["health"], row["diagnostic_count"])
        for row in conn.execute("SELECT id,health,diagnostic_count FROM sources")
    }
    assert source_health["codex_active"] == ("ok", 0)
    assert source_health["codex_archived"] == ("ok", 0)

    with TestClient(create_app(cfg), base_url=f"http://127.0.0.1:{cfg.server_port}") as client:
        search_result = client.get("/api/search?q=revision").json()["items"][0]
        assert search_result["provider"] == "codex"
        assert "session_uuid" not in search_result
        detail = client.get(f"/api/sessions/{created.session_id}").json()
        assert "session_uuid" not in detail


def test_parse_failure_keeps_last_valid_revision(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "source"
    root.mkdir()
    source_fixture = (
        FIXTURES
        / "claude"
        / "projects"
        / "-Users-example-repo"
        / "11111111-1111-4111-8111-111111111111.jsonl"
    )
    path = root / source_fixture.name
    shutil.copy(source_fixture, path)
    source = SourceConfig("claude_code", "Claude", "claude_code", str(root))
    cfg = _config(tmp_path / "vault", [source])
    assert ingest_artifact(candidate_for_path(source, path), cfg).status == "imported"
    conn = get_connection(cfg.db_path)
    before = tuple(conn.execute("SELECT revision,message_count FROM sessions").fetchone())

    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    adapter = get_adapter("claude_code")

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr(adapter, "parse", fail_parse)
    failed = ingest_artifact(candidate_for_path(source, path), cfg)
    assert failed.status == "failed"
    assert tuple(conn.execute("SELECT revision,message_count FROM sessions").fetchone()) == before
    assert conn.execute("SELECT status FROM source_artifacts").fetchone()[0] == "failed"


def test_metadata_only_touch_hash_skips_provider_parser(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "source"
    root.mkdir()
    source_fixture = (
        FIXTURES
        / "claude"
        / "projects"
        / "-Users-example-repo"
        / "11111111-1111-4111-8111-111111111111.jsonl"
    )
    path = root / source_fixture.name
    shutil.copy(source_fixture, path)
    source = SourceConfig("claude_code", "Claude", "claude_code", str(root))
    cfg = _config(tmp_path / "vault", [source])
    assert ingest_artifact(candidate_for_path(source, path), cfg).status == "imported"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    adapter = get_adapter("claude_code")

    def fail_parse(*_args, **_kwargs):
        raise AssertionError("metadata-only touch should skip parsing")

    monkeypatch.setattr(adapter, "parse", fail_parse)
    result = ingest_artifact(candidate_for_path(source, path), cfg)

    assert result.status == "unchanged"
    assert result.changed is False
    assert get_connection(cfg.db_path).execute("SELECT revision FROM sessions").fetchone()[0] == 1


def test_missing_artifact_retains_revision_and_recovers_health(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    source_fixture = (
        FIXTURES
        / "claude"
        / "projects"
        / "-Users-example-repo"
        / "11111111-1111-4111-8111-111111111111.jsonl"
    )
    path = root / source_fixture.name
    shutil.copy(source_fixture, path)
    source = SourceConfig("claude_code", "Claude", "claude_code", str(root))
    cfg = _config(tmp_path / "vault", [source])
    created = ingest_artifact(candidate_for_path(source, path), cfg)
    conn = get_connection(cfg.db_path)
    messages_before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    path.unlink()
    assert reconcile_missing(conn, source, set(), RedactionEngine(cfg.rules_path)) == 1
    assert conn.execute("SELECT status FROM source_artifacts").fetchone()[0] == "missing"
    assert conn.execute("SELECT health FROM sessions").fetchone()[0] == "degraded"
    assert conn.execute("SELECT health FROM sources").fetchone()[0] == "degraded"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == messages_before

    shutil.copy(source_fixture, path)
    recovered = ingest_artifact(candidate_for_path(source, path), cfg)
    assert recovered.session_id == created.session_id
    assert recovered.status == "updated"
    assert conn.execute("SELECT health FROM sessions").fetchone()[0] == "ok"
    assert conn.execute("SELECT health FROM sources").fetchone()[0] == "ok"


def test_session_health_aggregates_across_root_and_sidecar_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    session_id = "99999999-9999-4999-8999-999999999999"
    path = root / f"{session_id}.jsonl"
    first = {
        "type": "user",
        "uuid": "m1",
        "sessionId": session_id,
        "message": {"role": "user", "content": [{"type": "text", "text": "first"}]},
    }
    path.write_text(json.dumps(first) + "\n" + '{"type":', encoding="utf-8")
    source = SourceConfig("claude_code", "Claude", "claude_code", str(root))
    cfg = _config(tmp_path / "vault", [source])
    assert ingest_artifact(candidate_for_path(source, path), cfg).status == "degraded"

    sidecar = root / session_id / "tool-results" / "call-1.txt"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("sidecar remains searchable", encoding="utf-8")
    assert ingest_artifact(candidate_for_path(source, sidecar), cfg).status == "imported"
    conn = get_connection(cfg.db_path)
    session = conn.execute("SELECT health,source_path FROM sessions").fetchone()
    assert session["health"] == "degraded"
    assert session["source_path"] == path.name
    source_health = conn.execute("SELECT health,diagnostic_count FROM sources").fetchone()
    assert tuple(source_health) == ("degraded", 1)

    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            '"assistant","uuid":"m2","sessionId":"99999999-9999-4999-8999-999999999999",'
            '"message":{"role":"assistant","content":[{"type":"text",'
            '"text":"complete"}]}}\n'
        )
    assert ingest_artifact(candidate_for_path(source, path), cfg).status == "updated"
    assert conn.execute("SELECT health FROM sessions").fetchone()[0] == "ok"
    assert tuple(conn.execute("SELECT health,diagnostic_count FROM sources").fetchone()) == (
        "ok",
        0,
    )


def test_message_cursor_and_streamed_export_cover_root_plus_sidecar(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    path = root / f"{session_id}.jsonl"
    records = []
    for index in range(105):
        records.append(
            {
                "type": "user",
                "uuid": f"m-{index}",
                "sessionId": session_id,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": f"root message {index}"}],
                },
            }
        )
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    sidecar = root / session_id / "tool-results" / "result.txt"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("supplemental result", encoding="utf-8")
    source = SourceConfig("claude_code", "Claude", "claude_code", str(root))
    cfg = _config(tmp_path / "vault", [source])
    created = ingest_artifact(candidate_for_path(source, path), cfg)
    ingest_artifact(candidate_for_path(source, sidecar), cfg)

    with TestClient(create_app(cfg), base_url=f"http://127.0.0.1:{cfg.server_port}") as client:
        first = client.get(
            f"/api/sessions/{created.session_id}/messages", params={"limit": 100}
        ).json()
        second = client.get(
            f"/api/sessions/{created.session_id}/messages",
            params={"limit": 100, "cursor": first["next_cursor"]},
        ).json()
        message_ids = [item["id"] for item in first["items"] + second["items"]]
        assert len(message_ids) == 106
        assert len(set(message_ids)) == 106
        exported = client.get(
            f"/api/sessions/{created.session_id}/export?format=canonical-jsonl"
        ).text.splitlines()
        assert len(exported) == 107  # one session record plus every message


def test_sentinel_is_absent_from_db_api_export_backup_and_restore(tmp_path: Path) -> None:
    sentinel = "password=supersecret123"
    root = tmp_path / "source"
    root.mkdir()
    session_id = "66666666-6666-4666-8666-666666666666"
    path = root / f"{session_id}.jsonl"
    records = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": session_id,
            "cwd": f"/Users/example/{sentinel}",
            "gitBranch": sentinel,
            "message": {"role": "user", "content": [{"type": "text", "text": sentinel}]},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": session_id,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "Write",
                        "input": {"file_path": sentinel, "content": sentinel},
                    },
                    {"type": "text", "text": sentinel},
                ],
            },
        },
        {"type": "ai-title", "sessionId": session_id, "aiTitle": sentinel},
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    source = SourceConfig("claude_code", "Claude", "claude_code", str(root))
    cfg = _config(tmp_path / "vault", [source])
    result = ingest_artifact(candidate_for_path(source, path), cfg)
    assert result.status == "imported"

    with TestClient(create_app(cfg), base_url=f"http://127.0.0.1:{cfg.server_port}") as client:
        assert sentinel not in client.get(f"/api/sessions/{result.session_id}").text
        assert sentinel not in client.get(f"/api/sessions/{result.session_id}/messages").text
        assert (
            sentinel
            not in client.get(
                f"/api/sessions/{result.session_id}/export?format=canonical-jsonl"
            ).text
        )

    conn = get_connection(cfg.db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    close_all_connections()
    assert sentinel.encode() not in cfg.db_path.read_bytes()
    for sidecar in (cfg.db_path.with_name("vault.db-wal"), cfg.db_path.with_name("vault.db-shm")):
        if sidecar.exists():
            assert sentinel.encode() not in sidecar.read_bytes()

    backup = create_backup(cfg.vault_dir, tmp_path / "portable.vgbak")
    with zipfile.ZipFile(backup.path) as archive:
        assert all(sentinel.encode() not in archive.read(name) for name in archive.namelist())
    restored = restore_backup(backup.path, tmp_path / "restored")
    assert sentinel.encode() not in (restored.vault_dir / "vault.db").read_bytes()
