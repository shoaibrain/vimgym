"""Streaming adapter for Claude Code root, subagent, and tool-result artifacts."""

from __future__ import annotations

import codecs
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from ._util import (
    READ_CHUNK_SIZE,
    candidate,
    first_json_objects,
    first_text,
    iter_complete_jsonl,
    preview,
    scan_file,
    strip_opaque,
    walk_files,
)
from .base import (
    ArtifactCandidate,
    ArtifactIdentity,
    CanonicalBlock,
    CanonicalMessage,
    CanonicalSession,
    Diagnostic,
    ParseOutcome,
    RecordSink,
    SourceSpec,
    bounded_diagnostic,
    deterministic_block_id,
    deterministic_message_id,
    deterministic_session_id,
    deterministic_workspace_id,
)


_TEXT_SIDECAR_SUFFIXES = {"", ".txt", ".log", ".md", ".json", ".xml", ".csv"}
_MAX_DIAGNOSTICS = 100


class ClaudeAdapter:
    provider: Literal["claude_code"] = "claude_code"
    parser_version = "2.0.0"

    def default_sources(self, environment: Mapping[str, str]) -> list[SourceSpec]:
        home = Path(environment.get("HOME", str(Path.home()))).expanduser()
        claude_home = Path(
            environment.get("CLAUDE_HOME")
            or environment.get("CLAUDE_CONFIG_DIR")
            or home / ".claude"
        ).expanduser()
        return [
            SourceSpec(
                id="claude_code:projects",
                provider="claude_code",
                root=claude_home / "projects",
            )
        ]

    def iter_artifacts(self, source: SourceSpec) -> Iterable[ArtifactCandidate]:
        if source.provider != self.provider or not source.enabled:
            return
        for path in walk_files(source.root):
            relative_parts = path.relative_to(source.root).parts
            artifact = None
            if path.suffix.lower() == ".jsonl":
                # Workflow journals multiplex many agentIds in one coordination
                # stream and duplicate the real per-agent JSONL beside them.
                # They are transport state, not a session artifact.
                if path.name == "journal.jsonl" and "workflows" in relative_parts:
                    continue
                artifact = candidate(source, path, "session_jsonl")
            elif "tool-results" in relative_parts and path.suffix.lower() in _TEXT_SIDECAR_SUFFIXES:
                artifact = candidate(source, path, "tool_result")
            if artifact is not None:
                yield artifact

    def probe(self, artifact: ArtifactCandidate) -> ArtifactIdentity:
        path_identity = _identity_from_path(artifact)
        if artifact.artifact_type == "tool_result":
            return path_identity

        session_id: str | None = None
        agent_id: str | None = None
        for obj in first_json_objects(artifact.path):
            native_session = obj.get("sessionId") or obj.get("session_id")
            native_agent = obj.get("agentId") or obj.get("agent_id")
            if isinstance(native_session, str) and native_session:
                session_id = native_session
            if isinstance(native_agent, str) and native_agent:
                agent_id = _strip_agent_prefix(native_agent)
            if session_id and (path_identity.kind != "subagent" or agent_id):
                break

        root_external = session_id or path_identity.root_id or path_identity.provider_session_id
        if path_identity.kind == "subagent":
            agent = agent_id or _agent_from_filename(artifact.path)
            external = _subagent_external_id(root_external, agent)
            return ArtifactIdentity(
                provider_session_id=external,
                kind="subagent",
                lifecycle=artifact.source.lifecycle,
                parent_id=root_external,
                root_id=root_external,
            )
        return ArtifactIdentity(
            provider_session_id=root_external,
            kind="user",
            lifecycle=artifact.source.lifecycle,
            root_id=root_external,
        )

    def parse(self, artifact: ArtifactCandidate, sink: RecordSink) -> ParseOutcome:
        if artifact.artifact_type == "tool_result":
            return self._parse_tool_result(artifact, sink)

        identity = self.probe(artifact)
        content_hash, complete_lines, processed_bytes, partial = scan_file(artifact.path)
        session_id = deterministic_session_id(self.provider, identity.provider_session_id)
        parent_id = (
            deterministic_session_id(self.provider, identity.parent_id)
            if identity.parent_id
            else None
        )
        root_id = deterministic_session_id(
            self.provider, identity.root_id or identity.provider_session_id
        )

        diagnostics: list[Diagnostic] = []
        ignored_records = 0
        unknown_records = 0
        cwd: str | None = None
        branch: str | None = None
        originator: str | None = "Claude Code"
        client: str | None = None
        model: str | None = None
        provider_title: str | None = None
        fallback_title: str | None = None
        started_at: str | None = None
        ended_at: str | None = None
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        metadata: dict[str, Any] = {"parser_version": self.parser_version}

        for line_number, _, obj, error in iter_complete_jsonl(artifact.path):
            if error:
                _append_diagnostic(
                    diagnostics,
                    bounded_diagnostic(
                        "malformed_jsonl", "Malformed JSONL record was skipped", line=line_number
                    ),
                )
                continue
            if obj is None:
                ignored_records += 1
                continue

            event_type = str(obj.get("type") or "")
            timestamp = _string(obj.get("timestamp"))
            if timestamp:
                started_at = started_at or timestamp
                ended_at = timestamp
            cwd = cwd or _string(obj.get("cwd"))
            branch = branch or _string(obj.get("gitBranch"))
            client = client or _string(obj.get("entrypoint"))
            metadata["claude_version"] = metadata.get("claude_version") or _string(
                obj.get("version")
            )
            metadata["permission_mode"] = metadata.get("permission_mode") or _string(
                obj.get("permissionMode")
            )

            if event_type in {"ai-title", "title", "session-title", "custom-title"}:
                provider_title = (
                    _string(obj.get("aiTitle"))
                    or _string(obj.get("title"))
                    or _string(obj.get("customTitle"))
                    or provider_title
                )
                continue
            if event_type in {"last-prompt", "prompt"}:
                prompt = _string(obj.get("lastPrompt")) or _string(obj.get("prompt"))
                fallback_title = fallback_title or preview(prompt)
                continue
            if event_type == "queue-operation":
                ignored_records += 1
                continue
            if event_type in {"progress", "hook_progress"}:
                ignored_records += 1
                continue
            if event_type in {"user", "assistant"}:
                message_obj = obj.get("message")
                message_data = message_obj if isinstance(message_obj, dict) else {}
                role = _string(message_data.get("role")) or event_type
                if obj.get("isMeta"):
                    role = "system"
                message_model = _string(message_data.get("model"))
                model = model or message_model
                native_message_id = _string(obj.get("uuid")) or _string(message_data.get("id"))
                parent_native_id = _string(obj.get("parentUuid"))
                content = message_data.get("content", "")

                if role == "user" and fallback_title is None:
                    fallback_title = preview(first_text(content))

                native_usage = message_data.get("usage")
                if isinstance(native_usage, dict):
                    usage["input_tokens"] += _integer(native_usage.get("input_tokens"))
                    usage["output_tokens"] += _integer(native_usage.get("output_tokens"))
                    usage["cache_read_tokens"] += _integer(
                        native_usage.get("cache_read_input_tokens")
                    )
                    usage["cache_write_tokens"] += _integer(
                        native_usage.get("cache_creation_input_tokens")
                    )

                self._emit_content_message(
                    sink=sink,
                    session_id=session_id,
                    line_number=line_number,
                    native_message_id=native_message_id,
                    parent_native_id=parent_native_id,
                    role=role,
                    model=message_model,
                    timestamp=timestamp,
                    content=content,
                    default_visibility="hidden" if obj.get("isMeta") else "visible",
                    diagnostics=diagnostics,
                )
                continue
            if event_type == "attachment":
                self._emit_structured_event(
                    sink,
                    session_id,
                    line_number,
                    obj,
                    role="user",
                    block_type="attachment",
                    visibility="visible",
                )
                continue
            if event_type == "pr-link":
                self._emit_structured_event(
                    sink,
                    session_id,
                    line_number,
                    obj,
                    role="system",
                    block_type="attachment",
                    visibility="visible",
                )
                continue
            if event_type == "file-history-snapshot":
                self._emit_structured_event(
                    sink,
                    session_id,
                    line_number,
                    obj,
                    role="system",
                    block_type="attachment",
                    visibility="hidden",
                )
                continue
            if event_type == "system":
                content = obj.get("message") or obj.get("content") or obj.get("text") or ""
                self._emit_content_message(
                    sink=sink,
                    session_id=session_id,
                    line_number=line_number,
                    native_message_id=_string(obj.get("uuid")),
                    parent_native_id=_string(obj.get("parentUuid")),
                    role="system",
                    model=None,
                    timestamp=timestamp,
                    content=content,
                    default_visibility="visible",
                    diagnostics=diagnostics,
                )
                continue
            if event_type in {"summary", "compact", "compaction"}:
                self._emit_single_block_message(
                    sink,
                    session_id,
                    line_number,
                    _string(obj.get("uuid")),
                    "system",
                    "compaction",
                    first_text(obj.get("summary") or obj.get("content") or obj.get("message")),
                    {},
                    timestamp=timestamp,
                )
                continue
            if event_type == "mode":
                self._emit_structured_event(
                    sink,
                    session_id,
                    line_number,
                    obj,
                    role="system",
                    block_type="unknown_event",
                    visibility="hidden",
                )
                continue

            if _has_content(obj):
                unknown_records += 1
                _append_diagnostic(
                    diagnostics,
                    bounded_diagnostic(
                        "unknown_content_event",
                        f"Unknown content-bearing Claude record type {event_type or '<empty>'}",
                        line=line_number,
                    ),
                )
                self._emit_structured_event(
                    sink,
                    session_id,
                    line_number,
                    obj,
                    role="system",
                    block_type="unknown_event",
                    visibility="hidden",
                )
            else:
                ignored_records += 1

        if partial:
            _append_diagnostic(
                diagnostics,
                bounded_diagnostic(
                    "partial_trailing_line",
                    "Incomplete trailing JSONL line was deferred until the next revision",
                ),
            )

        sink.emit(
            CanonicalSession(
                id=session_id,
                provider="claude_code",
                external_id=identity.provider_session_id,
                kind=identity.kind,
                lifecycle=identity.lifecycle,
                source_id=artifact.source.id,
                parent_id=parent_id,
                root_id=root_id,
                originator=originator,
                client=client,
                model=model,
                title=provider_title or fallback_title,
                workspace_id=deterministic_workspace_id(self.provider, cwd),
                cwd=cwd,
                branch=branch,
                started_at=started_at,
                ended_at=ended_at,
                usage=usage,
                metadata={key: value for key, value in metadata.items() if value is not None},
            )
        )
        degraded = bool(diagnostics or unknown_records)
        return ParseOutcome(
            content_hash=content_hash,
            processed_lines=complete_lines,
            processed_bytes=processed_bytes,
            status="degraded" if degraded else "imported",
            diagnostics=tuple(diagnostics),
            ignored_records=ignored_records,
            unknown_records=unknown_records,
            partial=partial,
        )

    def _emit_content_message(
        self,
        *,
        sink: RecordSink,
        session_id: str,
        line_number: int,
        native_message_id: str | None,
        parent_native_id: str | None,
        role: str,
        model: str | None,
        timestamp: str | None,
        content: Any,
        default_visibility: str,
        diagnostics: list[Diagnostic],
    ) -> None:
        message_id = deterministic_message_id(
            session_id, native_message_id, source_line=line_number
        )
        parent_message_id = (
            deterministic_message_id(session_id, parent_native_id, source_line=0)
            if parent_native_id
            else None
        )
        sink.emit(
            CanonicalMessage(
                id=message_id,
                session_id=session_id,
                sequence=line_number,
                role=role,
                provider_message_id=native_message_id,
                model=model,
                timestamp=timestamp,
                parent_message_id=parent_message_id,
            )
        )

        blocks = content if isinstance(content, list) else [content]
        for index, raw_block in enumerate(blocks):
            block_type = "unknown_event"
            visibility = default_visibility
            text: str | None = None
            data: dict[str, Any] = {}
            call_id: str | None = None
            mime_type: str | None = None
            is_error = False

            if isinstance(raw_block, str):
                block_type = "text"
                text = raw_block
            elif isinstance(raw_block, dict):
                native_type = _string(raw_block.get("type")) or ""
                if native_type in {"text", "input_text", "output_text"}:
                    block_type = "text"
                    text = _string(raw_block.get("text")) or _text_value(raw_block.get("content"))
                elif native_type in {"tool_use", "tool_call"}:
                    block_type = "tool_call"
                    call_id = _string(raw_block.get("id")) or _string(raw_block.get("call_id"))
                    data = {
                        "name": raw_block.get("name"),
                        "arguments": strip_opaque(
                            raw_block.get("input", raw_block.get("arguments", {}))
                        ),
                    }
                    text = _string(raw_block.get("name"))
                elif native_type == "tool_result":
                    block_type = "tool_result"
                    call_id = _string(raw_block.get("tool_use_id")) or _string(
                        raw_block.get("call_id")
                    )
                    result = raw_block.get("content", raw_block.get("output"))
                    text = _text_value(result)
                    data = {
                        key: strip_opaque(value)
                        for key, value in raw_block.items()
                        if key not in {"content", "output"}
                    }
                    is_error = bool(raw_block.get("is_error"))
                elif native_type in {"image", "document", "attachment"}:
                    block_type = "attachment"
                    source = raw_block.get("source")
                    source_data = source if isinstance(source, dict) else {}
                    mime_type = _string(source_data.get("media_type")) or _string(
                        raw_block.get("mime_type")
                    )
                    data = _attachment_metadata(raw_block)
                elif native_type in {"thinking", "redacted_thinking"}:
                    block_type = "omitted"
                    visibility = "hidden"
                    data = {"reason": "hidden_reasoning"}
                elif native_type == "fallback":
                    block_type = "omitted"
                    visibility = "hidden"
                    data = {
                        "reason": "model_fallback",
                        "from": strip_opaque(raw_block.get("from")),
                        "to": strip_opaque(raw_block.get("to")),
                    }
                else:
                    block_type = "unknown_event"
                    visibility = "hidden"
                    data = strip_opaque(raw_block)
                    _append_diagnostic(
                        diagnostics,
                        bounded_diagnostic(
                            "unknown_content_block",
                            f"Unknown Claude message block type {native_type or '<empty>'}",
                            line=line_number,
                        ),
                    )
            elif raw_block is not None:
                data = {"value": raw_block}

            sink.emit(
                CanonicalBlock(
                    id=deterministic_block_id(message_id, call_id, index),
                    message_id=message_id,
                    session_id=session_id,
                    sequence=index,
                    block_type=block_type,  # type: ignore[arg-type]
                    visibility=visibility,  # type: ignore[arg-type]
                    text=text,
                    data=data,
                    call_id=call_id,
                    mime_type=mime_type,
                    is_error=is_error,
                )
            )

    def _emit_structured_event(
        self,
        sink: RecordSink,
        session_id: str,
        line_number: int,
        obj: dict[str, Any],
        *,
        role: str,
        block_type: str,
        visibility: str,
    ) -> None:
        native_message_id = _string(obj.get("uuid"))
        self._emit_single_block_message(
            sink,
            session_id,
            line_number,
            native_message_id,
            role,
            block_type,
            None,
            strip_opaque(obj),
            visibility=visibility,
            timestamp=_string(obj.get("timestamp")),
        )

    def _emit_single_block_message(
        self,
        sink: RecordSink,
        session_id: str,
        line_number: int,
        native_message_id: str | None,
        role: str,
        block_type: str,
        text: str | None,
        data: Mapping[str, Any],
        *,
        visibility: str = "visible",
        timestamp: str | None = None,
        call_id: str | None = None,
    ) -> None:
        message_id = deterministic_message_id(
            session_id, native_message_id, source_line=line_number
        )
        sink.emit(
            CanonicalMessage(
                id=message_id,
                session_id=session_id,
                sequence=line_number,
                role=role,
                provider_message_id=native_message_id,
                timestamp=timestamp,
            )
        )
        sink.emit(
            CanonicalBlock(
                id=deterministic_block_id(message_id, call_id, 0),
                message_id=message_id,
                session_id=session_id,
                sequence=0,
                block_type=block_type,  # type: ignore[arg-type]
                visibility=visibility,  # type: ignore[arg-type]
                text=text,
                data=data,
                call_id=call_id,
            )
        )

    def _parse_tool_result(self, artifact: ArtifactCandidate, sink: RecordSink) -> ParseOutcome:
        identity = self.probe(artifact)
        session_id = deterministic_session_id(self.provider, identity.provider_session_id)
        digest = hashlib.sha256()
        binary = False
        total_bytes = 0
        line_count = 0
        ends_with_newline = False
        with artifact.path.open("rb") as handle:
            while chunk := handle.read(READ_CHUNK_SIZE):
                digest.update(chunk)
                total_bytes += len(chunk)
                binary = binary or b"\x00" in chunk
                line_count += chunk.count(b"\n")
                ends_with_newline = chunk.endswith(b"\n")
        if total_bytes and not ends_with_newline and not binary:
            line_count += 1

        native_id = f"tool-result:{artifact.relative_path}"
        message_id = deterministic_message_id(session_id, native_id, source_line=1)
        call_id = artifact.path.stem
        sink.emit(
            CanonicalMessage(
                id=message_id,
                session_id=session_id,
                sequence=1,
                role="tool",
                provider_message_id=native_id,
            )
        )
        if binary:
            sink.emit(
                CanonicalBlock(
                    id=deterministic_block_id(message_id, call_id, 0),
                    message_id=message_id,
                    session_id=session_id,
                    sequence=0,
                    block_type="attachment",
                    data={"name": artifact.path.name, "binary": True},
                    call_id=call_id,
                    original_size=total_bytes,
                )
            )
        else:
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            index = 0
            with artifact.path.open("rb") as handle:
                while chunk := handle.read(READ_CHUNK_SIZE):
                    text = decoder.decode(chunk, final=False)
                    if text:
                        sink.emit(
                            CanonicalBlock(
                                id=deterministic_block_id(message_id, call_id, index),
                                message_id=message_id,
                                session_id=session_id,
                                sequence=index,
                                block_type="tool_result",
                                text=text,
                                data={"chunk": index},
                                call_id=call_id,
                            )
                        )
                        index += 1
                tail = decoder.decode(b"", final=True)
                if tail:
                    sink.emit(
                        CanonicalBlock(
                            id=deterministic_block_id(message_id, call_id, index),
                            message_id=message_id,
                            session_id=session_id,
                            sequence=index,
                            block_type="tool_result",
                            text=tail,
                            data={"chunk": index},
                            call_id=call_id,
                        )
                    )

        sink.emit(
            CanonicalSession(
                id=session_id,
                provider="claude_code",
                external_id=identity.provider_session_id,
                kind=identity.kind,
                lifecycle=identity.lifecycle,
                source_id=artifact.source.id,
                parent_id=(
                    deterministic_session_id(self.provider, identity.parent_id)
                    if identity.parent_id
                    else None
                ),
                root_id=deterministic_session_id(
                    self.provider, identity.root_id or identity.provider_session_id
                ),
                originator="Claude Code",
                metadata={"parser_version": self.parser_version, "sidecar": True},
            )
        )
        return ParseOutcome(
            content_hash=digest.hexdigest(),
            processed_lines=line_count,
            processed_bytes=total_bytes,
            status="imported",
        )


def _identity_from_path(artifact: ArtifactCandidate) -> ArtifactIdentity:
    parts = Path(artifact.relative_path).parts
    if artifact.artifact_type == "tool_result" and "tool-results" in parts:
        index = parts.index("tool-results")
        root = parts[index - 1] if index > 0 else artifact.path.parent.parent.name
        return ArtifactIdentity(root, "user", artifact.source.lifecycle, root_id=root)
    if "subagents" in parts:
        index = parts.index("subagents")
        root = parts[index - 1] if index > 0 else artifact.path.parent.parent.name
        agent = _agent_from_filename(artifact.path)
        return ArtifactIdentity(
            _subagent_external_id(root, agent),
            "subagent",
            artifact.source.lifecycle,
            parent_id=root,
            root_id=root,
        )
    root = artifact.path.stem
    return ArtifactIdentity(root, "user", artifact.source.lifecycle, root_id=root)


def _agent_from_filename(path: Path) -> str:
    return _strip_agent_prefix(path.stem)


def _strip_agent_prefix(value: str) -> str:
    return value.removeprefix("agent-")


def _subagent_external_id(root_session_id: str, agent_id: str) -> str:
    return f"{root_session_id}:agent:{agent_id}"


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _attachment_metadata(block: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"type": block.get("type")}
    for key in ("name", "file_name", "path", "file_path", "mime_type"):
        if key in block:
            result[key] = block[key]
    source = block.get("source")
    if isinstance(source, dict):
        for key in ("type", "media_type", "name", "path"):
            if key in source:
                result[key] = source[key]
    result["content_omitted"] = True
    return result


def _has_content(obj: Mapping[str, Any]) -> bool:
    content_keys = {
        "message",
        "content",
        "text",
        "summary",
        "error",
        "attachment",
        "prompt",
        "result",
        "output",
    }
    return any(key in obj and obj[key] not in (None, "", [], {}) for key in content_keys)


def _append_diagnostic(diagnostics: list[Diagnostic], diagnostic: Diagnostic) -> None:
    if len(diagnostics) < _MAX_DIAGNOSTICS:
        diagnostics.append(diagnostic)


CLAUDE_ADAPTER = ClaudeAdapter()
