from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vimgym.providers import (
    ArtifactCandidate,
    CanonicalBlock,
    CanonicalMessage,
    CanonicalSession,
    ClaudeAdapter,
    CollectingSink,
    SourceSpec,
    deterministic_session_id,
)


def _write_jsonl(path: Path, records: list[dict[str, Any]], trailing: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(record) + "\n" for record in records) + trailing
    path.write_text(text, encoding="utf-8")


def _candidate(
    source: SourceSpec, path: Path, artifact_type: str = "session_jsonl"
) -> ArtifactCandidate:
    stat = path.stat()
    return ArtifactCandidate(
        source=source,
        path=path,
        relative_path=path.relative_to(source.root).as_posix(),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        artifact_type=artifact_type,  # type: ignore[arg-type]
    )


def test_claude_streams_root_session_into_canonical_records(tmp_path: Path) -> None:
    source = SourceSpec("claude:test", "claude_code", tmp_path / "projects")
    session_path = source.root / "-Users-example-repo" / "root-123.jsonl"
    _write_jsonl(
        session_path,
        [
            {
                "type": "queue-operation",
                "sessionId": "root-123",
                "timestamp": "2026-01-01T00:00:00Z",
                "operation": "enqueue",
            },
            {
                "type": "user",
                "sessionId": "root-123",
                "uuid": "user-1",
                "cwd": "/Users/example/repo",
                "gitBranch": "feature/private-name",
                "entrypoint": "claude-vscode",
                "version": "2.1.0",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {"role": "user", "content": "fallback user request"},
            },
            {
                "type": "assistant",
                "sessionId": "root-123",
                "uuid": "assistant-1",
                "parentUuid": "user-1",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-test",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 2,
                    },
                    "content": [
                        {"type": "text", "text": "done"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Read",
                            "input": {"file_path": "/private/file.txt"},
                        },
                        {
                            "type": "image",
                            "source": {"media_type": "image/png", "data": "secret-image"},
                        },
                        {"type": "thinking", "thinking": "hidden chain"},
                    ],
                },
            },
            {"type": "ai-title", "sessionId": "root-123", "aiTitle": "Provider title"},
            {
                "type": "file-history-snapshot",
                "sessionId": "root-123",
                "snapshot": {"trackedFileBackups": {"/private/file.txt": {}}},
            },
        ],
    )

    sink = CollectingSink()
    outcome = ClaudeAdapter().parse(_candidate(source, session_path), sink)
    sessions = sink.values(CanonicalSession)
    messages = sink.values(CanonicalMessage)
    blocks = sink.values(CanonicalBlock)

    assert outcome.status == "imported"
    assert outcome.partial is False
    assert len(sessions) == 1
    session = sessions[0]
    assert session.id == deterministic_session_id("claude_code", "root-123")
    assert session.kind == "user"
    assert session.title == "Provider title"
    assert session.model == "claude-test"
    assert session.usage["input_tokens"] == 10
    assert session.usage["cache_read_tokens"] == 2
    assert session.cwd == "/Users/example/repo"
    assert any(message.parent_message_id for message in messages)
    assert {block.block_type for block in blocks} >= {
        "text",
        "tool_call",
        "attachment",
        "omitted",
    }
    image = next(block for block in blocks if block.mime_type == "image/png")
    assert "secret-image" not in json.dumps(image.data)
    omitted = next(block for block in blocks if block.block_type == "omitted")
    assert omitted.text is None
    assert "hidden chain" not in json.dumps(omitted.data)


def test_claude_subagent_identity_uses_root_and_agent_id(tmp_path: Path) -> None:
    source = SourceSpec("claude:test", "claude_code", tmp_path / "projects")
    path = source.root / "-Users-example-repo" / "root-abc" / "subagents" / "agent-agent42.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "sessionId": "root-abc",
                "agentId": "agent42",
                "uuid": "sub-user",
                "message": {"role": "user", "content": "delegated task"},
            }
        ],
    )
    adapter = ClaudeAdapter()
    artifact = _candidate(source, path)
    identity = adapter.probe(artifact)
    assert identity.provider_session_id == "root-abc:agent:agent42"
    assert identity.parent_id == "root-abc"
    assert identity.kind == "subagent"

    sink = CollectingSink()
    adapter.parse(artifact, sink)
    session = sink.values(CanonicalSession)[0]
    assert session.parent_id == deterministic_session_id("claude_code", "root-abc")
    assert session.root_id == session.parent_id


def test_claude_discovers_and_streams_text_tool_result_sidecar(tmp_path: Path) -> None:
    source = SourceSpec("claude:test", "claude_code", tmp_path / "projects")
    sidecar = source.root / "-Users-example-repo" / "root-xyz" / "tool-results" / "toolu_abc.txt"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("tool result text\nwith details", encoding="utf-8")

    artifacts = list(ClaudeAdapter().iter_artifacts(source))
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == "tool_result"
    assert ClaudeAdapter().probe(artifacts[0]).provider_session_id == "root-xyz"

    sink = CollectingSink()
    outcome = ClaudeAdapter().parse(artifacts[0], sink)
    assert outcome.processed_bytes == sidecar.stat().st_size
    blocks = sink.values(CanonicalBlock)
    assert "".join(block.text or "" for block in blocks) == "tool result text\nwith details"
    assert all(block.block_type == "tool_result" for block in blocks)


def test_claude_excludes_multiplexed_workflow_journal_but_keeps_agent_jsonl(
    tmp_path: Path,
) -> None:
    source = SourceSpec("claude:test", "claude_code", tmp_path / "projects")
    workflow = source.root / "project" / "root-id" / "subagents" / "workflows" / "wf-1"
    journal = workflow / "journal.jsonl"
    agent = workflow / "agent-child.jsonl"
    _write_jsonl(journal, [{"type": "started", "agentId": "one", "key": "task"}])
    _write_jsonl(
        agent,
        [
            {
                "type": "user",
                "sessionId": "root-id",
                "agentId": "child",
                "message": {"role": "user", "content": "work"},
            }
        ],
    )
    artifacts = list(ClaudeAdapter().iter_artifacts(source))
    assert [artifact.path for artifact in artifacts] == [agent]
    assert ClaudeAdapter().probe(artifacts[0]).provider_session_id == "root-id:agent:child"


def test_claude_defers_incomplete_tail_and_degrades_unknown_content(tmp_path: Path) -> None:
    source = SourceSpec("claude:test", "claude_code", tmp_path / "projects")
    path = source.root / "project" / "root-tail.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "sessionId": "root-tail",
                "message": {"role": "user", "content": "complete"},
            },
            {"type": "future-visible-event", "message": "preserve after redaction"},
        ],
        trailing='{"type":"assistant","message":{"content":"unfinished secret',
    )
    sink = CollectingSink()
    outcome = ClaudeAdapter().parse(_candidate(source, path), sink)
    blocks = sink.values(CanonicalBlock)
    assert outcome.partial is True
    assert outcome.status == "degraded"
    assert {diagnostic.code for diagnostic in outcome.diagnostics} >= {
        "partial_trailing_line",
        "unknown_content_event",
    }
    assert any(
        block.block_type == "unknown_event" and block.visibility == "hidden" for block in blocks
    )
    assert all("unfinished secret" not in (block.text or "") for block in blocks)
