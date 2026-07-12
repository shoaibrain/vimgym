from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vimgym.providers import (
    ArtifactCandidate,
    CanonicalBlock,
    CanonicalSession,
    CodexAdapter,
    CollectingSink,
    SourceSpec,
    deterministic_session_id,
)


def _write_jsonl(path: Path, records: list[dict[str, Any]], trailing: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records) + trailing,
        encoding="utf-8",
    )


def _candidate(source: SourceSpec, path: Path) -> ArtifactCandidate:
    stat = path.stat()
    return ArtifactCandidate(
        source=source,
        path=path,
        relative_path=path.relative_to(source.root).as_posix(),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def test_codex_maps_authoritative_response_items_and_ignores_duplicates(tmp_path: Path) -> None:
    source = SourceSpec("codex:test", "codex", tmp_path / "sessions")
    path = (
        source.root
        / "2026"
        / "01"
        / "01"
        / ("rollout-2026-01-01T00-00-00-019abcdef-0000-7000-8000-000000000001.jsonl")
    )
    thread_id = "019abcde-0000-7000-8000-000000000001"
    records = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": "2026-01-01T00:00:00Z",
                "cwd": "/Users/example/repo",
                "originator": "Claude Cowork",
                "source": "vscode",
                "cli_version": "1.2.3",
                "git": {"branch": "main"},
            },
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-test", "cwd": "/Users/example/repo"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "authoritative hello"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "authoritative hello"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call-1",
                "name": "shell",
                "arguments": '{"cmd":"pwd"}',
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "exec_command_end",
                "call_id": "call-1",
                "aggregated_output": "duplicate output",
                "exit_code": 0,
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call-1", "output": "result"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "call_id": "call-2",
                "changes": [{"path": "/private/changed.py", "kind": "update"}],
                "success": True,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "encrypted_content": "must-not-persist",
                "summary": [{"type": "summary_text", "text": "visible reasoning summary"}],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_reasoning", "text": "duplicate reasoning stream"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "author": "agent-a",
                "recipient": "agent-b",
                "content": [{"type": "input_text", "text": "transport-only content"}],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 42, "output_tokens": 9}},
            },
        },
        {"type": "world_state", "payload": {"timing": "ignored telemetry"}},
        {
            "type": "future_record",
            "payload": {"message": "future visible content"},
        },
    ]
    _write_jsonl(path, records, trailing='{"type":"response_item","payload":')

    sink = CollectingSink()
    outcome = CodexAdapter().parse(_candidate(source, path), sink)
    sessions = sink.values(CanonicalSession)
    blocks = sink.values(CanonicalBlock)

    assert outcome.status == "degraded"
    assert outcome.partial is True
    assert len(sessions) == 1
    session = sessions[0]
    assert session.id == deterministic_session_id("codex", thread_id)
    assert session.kind == "user"
    assert session.originator == "Claude Cowork"
    assert session.model == "gpt-test"
    assert session.title == "authoritative hello"
    assert session.usage["input_tokens"] == 42
    assert [block.text for block in blocks].count("authoritative hello") == 1
    assert "duplicate output" not in {block.text for block in blocks}
    assert "result" in {block.text for block in blocks}
    assert any("changed.py" in (block.text or "") for block in blocks)
    assert "visible reasoning summary" in {block.text for block in blocks}
    serialized = json.dumps([record.value.__dict__ for record in sink.records], default=str)
    assert "must-not-persist" not in serialized
    assert "ignored telemetry" not in serialized
    assert "duplicate reasoning stream" not in serialized
    assert "transport-only content" not in serialized
    assert any(
        block.block_type == "unknown_event" and block.visibility == "hidden" for block in blocks
    )


def test_codex_structured_subagent_and_archive_keep_one_identity(tmp_path: Path) -> None:
    thread_id = "019abcde-0000-7000-8000-000000000002"
    parent_id = "019abcde-0000-7000-8000-000000000001"
    session_meta = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "originator": "Codex Desktop",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}},
        },
    }
    active_source = SourceSpec("codex:active", "codex", tmp_path / "sessions", "active")
    archived_source = SourceSpec(
        "codex:archived", "codex", tmp_path / "archived_sessions", "archived"
    )
    active_path = active_source.root / f"rollout-{thread_id}.jsonl"
    archived_path = archived_source.root / f"rollout-{thread_id}.jsonl"
    _write_jsonl(active_path, [session_meta])
    _write_jsonl(archived_path, [session_meta])

    adapter = CodexAdapter()
    active_identity = adapter.probe(_candidate(active_source, active_path))
    archived_identity = adapter.probe(_candidate(archived_source, archived_path))
    assert active_identity.kind == "subagent"
    assert active_identity.parent_id == parent_id
    assert active_identity.provider_session_id == archived_identity.provider_session_id
    assert active_identity.lifecycle == "active"
    assert archived_identity.lifecycle == "archived"
    assert deterministic_session_id(
        "codex", active_identity.provider_session_id
    ) == deterministic_session_id("codex", archived_identity.provider_session_id)


def test_codex_classifies_automation_and_uses_event_fallback(tmp_path: Path) -> None:
    source = SourceSpec("codex:test", "codex", tmp_path / "sessions")
    thread_id = "019abcde-0000-7000-8000-000000000003"
    path = source.root / f"rollout-{thread_id}.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": {"id": thread_id, "source": {"automation": {"schedule": "daily"}}},
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": "scheduled task"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "finished"}},
        ],
    )
    adapter = CodexAdapter()
    artifact = _candidate(source, path)
    assert adapter.probe(artifact).kind == "automation"
    sink = CollectingSink()
    outcome = adapter.parse(artifact, sink)
    assert outcome.status == "imported"
    assert {block.text for block in sink.values(CanonicalBlock)} == {"scheduled task", "finished"}
    assert sink.values(CanonicalSession)[0].title == "scheduled task"


def test_codex_maps_visible_error_event(tmp_path: Path) -> None:
    source = SourceSpec("codex:test", "codex", tmp_path / "sessions")
    thread_id = "019abcde-0000-7000-8000-000000000004"
    path = source.root / f"rollout-{thread_id}.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": {"id": thread_id, "source": "vscode"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "error",
                    "message": "Command failed during local verification",
                    "codex_error_info": "other",
                },
            },
        ],
    )

    sink = CollectingSink()
    outcome = CodexAdapter().parse(_candidate(source, path), sink)
    blocks = sink.values(CanonicalBlock)

    assert outcome.status == "imported"
    assert len(blocks) == 1
    assert blocks[0].block_type == "text"
    assert blocks[0].visibility == "visible"
    assert blocks[0].is_error is True
    assert blocks[0].text == "Command failed during local verification"
    assert blocks[0].data == {"event_type": "error", "codex_error_info": "other"}
