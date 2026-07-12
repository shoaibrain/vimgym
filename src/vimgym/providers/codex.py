"""Streaming adapter for Codex active and archived rollout JSONL."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from ._util import (
    candidate,
    first_json_objects,
    first_text,
    iter_complete_jsonl,
    nested_value,
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


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_MAX_DIAGNOSTICS = 100
_IGNORED_TOP_LEVEL_TYPES = {
    "world_state",
    "inter_agent_communication_metadata",
}
_IGNORED_EVENT_TYPES = {
    "token_count",
    "task_started",
    "task_complete",
    "turn_started",
    "turn_complete",
    "turn_aborted",
    "rate_limits",
    "rate_limit",
    "model_request_start",
    "model_request_end",
    "exec_command_begin",
    "exec_command_start",
    "mcp_tool_call_begin",
    "mcp_tool_call_end",
    "patch_apply_begin",
    "patch_apply_end",
    "sub_agent_activity",
    "thread_settings_applied",
    "item_completed",
}


class CodexAdapter:
    provider: Literal["codex"] = "codex"
    parser_version = "2.0.1"

    def default_sources(self, environment: Mapping[str, str]) -> list[SourceSpec]:
        home = Path(environment.get("HOME", str(Path.home()))).expanduser()
        codex_home = Path(environment.get("CODEX_HOME") or home / ".codex").expanduser()
        return [
            SourceSpec(
                id="codex:sessions",
                provider="codex",
                root=codex_home / "sessions",
                lifecycle="active",
            ),
            SourceSpec(
                id="codex:archived_sessions",
                provider="codex",
                root=codex_home / "archived_sessions",
                lifecycle="archived",
            ),
        ]

    def iter_artifacts(self, source: SourceSpec) -> Iterable[ArtifactCandidate]:
        if source.provider != self.provider or not source.enabled:
            return
        for path in walk_files(source.root):
            if path.suffix.lower() != ".jsonl":
                continue
            artifact = candidate(source, path, "session_jsonl")
            if artifact is not None:
                yield artifact

    def probe(self, artifact: ArtifactCandidate) -> ArtifactIdentity:
        external_id: str | None = None
        kind = "unknown"
        parent_id: str | None = None
        root_id: str | None = None
        for obj in first_json_objects(artifact.path):
            if obj.get("type") != "session_meta":
                continue
            payload = _mapping(obj.get("payload"))
            external_id = _string(payload.get("id")) or _string(payload.get("thread_id"))
            thread_source = payload.get("thread_source", payload.get("source"))
            kind = _classify_thread_source(thread_source)
            parent_id = _string(payload.get("parent_thread_id")) or _string(
                nested_value(thread_source, "parent_thread_id")
            )
            root_id = _string(payload.get("root_thread_id")) or _string(
                nested_value(thread_source, "root_thread_id")
            )
            break
        external_id = external_id or _id_from_filename(artifact.path)
        if not external_id:
            raise ValueError("Codex artifact has no session identity")
        if kind == "unknown" and parent_id:
            kind = "subagent"
        root_id = root_id or parent_id or external_id
        return ArtifactIdentity(
            provider_session_id=external_id,
            kind=kind,  # type: ignore[arg-type]
            lifecycle=artifact.source.lifecycle,
            parent_id=parent_id,
            root_id=root_id,
        )

    def parse(self, artifact: ArtifactCandidate, sink: RecordSink) -> ParseOutcome:
        identity = self.probe(artifact)
        content_hash, complete_lines, processed_bytes, partial = scan_file(artifact.path)
        authority = self._authority(artifact)
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
        emitted_native: set[str] = set()
        seen_summaries: set[str] = set()
        current_turn: str | None = None
        cwd: str | None = None
        branch: str | None = None
        originator: str | None = None
        client: str | None = None
        model: str | None = None
        provider_title: str | None = None
        fallback_title: str | None = None
        started_at: str | None = None
        ended_at: str | None = None
        usage: dict[str, int] = {}
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

            record_type = _string(obj.get("type")) or ""
            payload = _mapping(obj.get("payload"))
            timestamp = _string(obj.get("timestamp")) or _string(payload.get("timestamp"))
            if timestamp:
                started_at = started_at or timestamp
                ended_at = timestamp

            if record_type == "session_meta":
                cwd = cwd or _string(payload.get("cwd"))
                originator = originator or _string(payload.get("originator"))
                provider_title = provider_title or _string(payload.get("title"))
                metadata["cli_version"] = _string(payload.get("cli_version"))
                metadata["model_provider"] = _string(payload.get("model_provider"))
                thread_source = payload.get("thread_source", payload.get("source"))
                client = client or _source_label(thread_source)
                git = _mapping(payload.get("git"))
                branch = branch or _string(git.get("branch")) or _string(payload.get("git_branch"))
                ignored_records += 1
                continue

            if record_type == "turn_context":
                current_turn = _string(payload.get("turn_id")) or current_turn
                cwd = cwd or _string(payload.get("cwd"))
                model = _string(payload.get("model")) or model
                summary = _visible_summary(payload.get("summary"))
                if summary and _summary_key(summary) not in seen_summaries:
                    seen_summaries.add(_summary_key(summary))
                    self._emit_single_block_message(
                        sink,
                        session_id,
                        line_number,
                        None,
                        "system",
                        "compaction",
                        summary,
                        {},
                        current_turn,
                        timestamp,
                    )
                else:
                    ignored_records += 1
                continue

            if record_type == "response_item":
                item_type = _string(payload.get("type")) or ""
                if item_type == "message":
                    role = _string(payload.get("role")) or "assistant"
                    native_id = _string(payload.get("id"))
                    if native_id and f"message:{native_id}" in emitted_native:
                        ignored_records += 1
                        continue
                    if native_id:
                        emitted_native.add(f"message:{native_id}")
                    content = payload.get("content", [])
                    if role == "user" and fallback_title is None:
                        fallback_title = preview(first_text(content))
                    self._emit_response_message(
                        sink,
                        session_id,
                        line_number,
                        native_id,
                        role,
                        model,
                        current_turn,
                        timestamp,
                        content,
                        diagnostics,
                    )
                    continue
                if item_type in {
                    "function_call",
                    "custom_tool_call",
                    "web_search_call",
                    "computer_call",
                    "tool_search_call",
                }:
                    call_id = _string(payload.get("call_id")) or _string(payload.get("id"))
                    native_id = f"call:{call_id}" if call_id else None
                    if native_id and native_id in emitted_native:
                        ignored_records += 1
                        continue
                    if native_id:
                        emitted_native.add(native_id)
                    name = _string(payload.get("name")) or item_type.removesuffix("_call")
                    arguments = payload.get("arguments", payload.get("action", {}))
                    self._emit_single_block_message(
                        sink,
                        session_id,
                        line_number,
                        native_id,
                        "assistant",
                        "tool_call",
                        name,
                        {"name": name, "arguments": strip_opaque(arguments)},
                        current_turn,
                        timestamp,
                        call_id=call_id,
                    )
                    continue
                if item_type in {
                    "function_call_output",
                    "custom_tool_call_output",
                    "computer_call_output",
                    "tool_search_output",
                }:
                    call_id = _string(payload.get("call_id")) or _string(payload.get("id"))
                    native_id = f"result:{call_id}" if call_id else None
                    if native_id and native_id in emitted_native:
                        ignored_records += 1
                        continue
                    if native_id:
                        emitted_native.add(native_id)
                    output = payload.get("output", payload.get("tools"))
                    self._emit_single_block_message(
                        sink,
                        session_id,
                        line_number,
                        native_id,
                        "tool",
                        "tool_result",
                        _text_value(output),
                        {
                            key: strip_opaque(value)
                            for key, value in payload.items()
                            if key not in {"output"}
                        },
                        current_turn,
                        timestamp,
                        call_id=call_id,
                        is_error=_is_error_output(output, payload),
                    )
                    continue
                if item_type == "reasoning":
                    summary = _visible_summary(payload.get("summary"))
                    if summary and _summary_key(summary) not in seen_summaries:
                        seen_summaries.add(_summary_key(summary))
                        self._emit_single_block_message(
                            sink,
                            session_id,
                            line_number,
                            _string(payload.get("id")),
                            "assistant",
                            "reasoning_summary",
                            summary,
                            {},
                            current_turn,
                            timestamp,
                        )
                    else:
                        ignored_records += 1
                    continue
                if item_type in {"compaction", "compacted"}:
                    summary = _visible_summary(
                        payload.get("summary") or payload.get("content") or payload.get("message")
                    )
                    if summary and _summary_key(summary) not in seen_summaries:
                        seen_summaries.add(_summary_key(summary))
                        self._emit_single_block_message(
                            sink,
                            session_id,
                            line_number,
                            _string(payload.get("id")),
                            "system",
                            "compaction",
                            summary,
                            {},
                            current_turn,
                            timestamp,
                        )
                    else:
                        ignored_records += 1
                    continue
                if item_type in {"agent_message", "inter_agent_message"}:
                    # Collaboration transport is not part of the user-visible
                    # transcript and is explicitly excluded from the vault.
                    ignored_records += 1
                    continue

                if _has_content(payload):
                    unknown_records += 1
                    _append_diagnostic(
                        diagnostics,
                        bounded_diagnostic(
                            "unknown_response_item",
                            f"Unknown content-bearing Codex response item {item_type or '<empty>'}",
                            line=line_number,
                        ),
                    )
                    self._emit_unknown(
                        sink,
                        session_id,
                        line_number,
                        payload,
                        current_turn,
                        timestamp,
                    )
                else:
                    ignored_records += 1
                continue

            if record_type == "event_msg":
                event_type = _string(payload.get("type")) or ""
                if event_type == "token_count":
                    _merge_usage(usage, payload.get("info"))
                    ignored_records += 1
                    continue
                if event_type in {"user_message", "agent_message"}:
                    if authority["messages"]:
                        ignored_records += 1
                        continue
                    role = "user" if event_type == "user_message" else "assistant"
                    content = payload.get("message", payload.get("content", ""))
                    if role == "user" and fallback_title is None:
                        fallback_title = preview(first_text(content))
                    self._emit_response_message(
                        sink,
                        session_id,
                        line_number,
                        None,
                        role,
                        model,
                        current_turn,
                        timestamp,
                        content,
                        diagnostics,
                    )
                    continue
                if event_type == "agent_reasoning":
                    if authority["reasoning"]:
                        ignored_records += 1
                        continue
                    summary = _visible_summary(
                        payload.get("message") or payload.get("text") or payload.get("summary")
                    )
                    if summary and _summary_key(summary) not in seen_summaries:
                        seen_summaries.add(_summary_key(summary))
                        self._emit_single_block_message(
                            sink,
                            session_id,
                            line_number,
                            None,
                            "assistant",
                            "reasoning_summary",
                            summary,
                            {},
                            current_turn,
                            timestamp,
                        )
                    else:
                        ignored_records += 1
                    continue
                if event_type == "error":
                    error_text = _text_value(
                        payload.get("message") or payload.get("error") or payload.get("content")
                    )
                    self._emit_single_block_message(
                        sink,
                        session_id,
                        line_number,
                        None,
                        "system",
                        "text",
                        error_text,
                        {
                            "event_type": event_type,
                            "codex_error_info": strip_opaque(payload.get("codex_error_info")),
                        },
                        current_turn,
                        timestamp,
                        is_error=True,
                    )
                    continue
                if event_type in {
                    "exec_command_end",
                    "mcp_tool_call_end",
                    "patch_apply_end",
                    "web_search_end",
                }:
                    call_id = _string(payload.get("call_id")) or _string(payload.get("id"))
                    if call_id and (
                        call_id in authority["tool_outputs"]
                        or (event_type == "web_search_end" and call_id in authority["web_calls"])
                    ):
                        ignored_records += 1
                        continue
                    output = (
                        payload.get("aggregated_output")
                        or payload.get("formatted_output")
                        or payload.get("output")
                        or payload.get("stdout")
                        or payload.get("result")
                        or payload.get("changes")
                        or payload.get("action")
                        or payload.get("query")
                    )
                    self._emit_single_block_message(
                        sink,
                        session_id,
                        line_number,
                        f"event-result:{call_id}" if call_id else None,
                        "tool",
                        "tool_result",
                        _text_value(output),
                        {
                            "status": payload.get("status"),
                            "exit_code": payload.get("exit_code"),
                            "stderr": payload.get("stderr"),
                            "event_type": event_type,
                            "changes": strip_opaque(payload.get("changes")),
                            "action": strip_opaque(payload.get("action")),
                        },
                        current_turn,
                        timestamp,
                        call_id=call_id,
                        is_error=_is_error_output(output, payload),
                    )
                    continue
                if event_type in {"context_compacted", "compacted"}:
                    summary = _visible_summary(
                        payload.get("summary") or payload.get("message") or payload.get("content")
                    )
                    if summary and _summary_key(summary) not in seen_summaries:
                        seen_summaries.add(_summary_key(summary))
                        self._emit_single_block_message(
                            sink,
                            session_id,
                            line_number,
                            None,
                            "system",
                            "compaction",
                            summary,
                            {},
                            current_turn,
                            timestamp,
                        )
                    else:
                        ignored_records += 1
                    continue
                if event_type in _IGNORED_EVENT_TYPES or _looks_like_telemetry(event_type):
                    ignored_records += 1
                    continue
                if _has_content(payload):
                    unknown_records += 1
                    _append_diagnostic(
                        diagnostics,
                        bounded_diagnostic(
                            "unknown_event_msg",
                            f"Unknown content-bearing Codex event {event_type or '<empty>'}",
                            line=line_number,
                        ),
                    )
                    self._emit_unknown(
                        sink,
                        session_id,
                        line_number,
                        payload,
                        current_turn,
                        timestamp,
                    )
                else:
                    ignored_records += 1
                continue

            if record_type in {"compacted", "compaction"}:
                summary = _visible_summary(
                    payload.get("summary") or payload.get("message") or payload.get("content")
                )
                if summary and _summary_key(summary) not in seen_summaries:
                    seen_summaries.add(_summary_key(summary))
                    self._emit_single_block_message(
                        sink,
                        session_id,
                        line_number,
                        None,
                        "system",
                        "compaction",
                        summary,
                        {},
                        current_turn,
                        timestamp,
                    )
                else:
                    ignored_records += 1
                continue
            if record_type in _IGNORED_TOP_LEVEL_TYPES:
                ignored_records += 1
                continue
            if _has_content(payload or obj):
                unknown_records += 1
                _append_diagnostic(
                    diagnostics,
                    bounded_diagnostic(
                        "unknown_record",
                        f"Unknown content-bearing Codex record {record_type or '<empty>'}",
                        line=line_number,
                    ),
                )
                self._emit_unknown(
                    sink,
                    session_id,
                    line_number,
                    payload or obj,
                    current_turn,
                    timestamp,
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
                provider="codex",
                external_id=identity.provider_session_id,
                kind=identity.kind,
                lifecycle=identity.lifecycle,
                source_id=artifact.source.id,
                parent_id=parent_id,
                root_id=root_id,
                originator=originator or "Codex",
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

    def _authority(self, artifact: ArtifactCandidate) -> dict[str, Any]:
        authority: dict[str, Any] = {
            "messages": False,
            "reasoning": False,
            "tool_outputs": set(),
            "web_calls": set(),
        }
        for _, _, obj, _ in iter_complete_jsonl(artifact.path):
            if not obj or obj.get("type") != "response_item":
                continue
            payload = _mapping(obj.get("payload"))
            item_type = _string(payload.get("type")) or ""
            authority["messages"] = authority["messages"] or item_type == "message"
            authority["reasoning"] = authority["reasoning"] or item_type == "reasoning"
            call_id = _string(payload.get("call_id")) or _string(payload.get("id"))
            if (
                item_type
                in {
                    "function_call_output",
                    "custom_tool_call_output",
                    "computer_call_output",
                    "tool_search_output",
                }
                and call_id
            ):
                authority["tool_outputs"].add(call_id)
            if item_type == "web_search_call" and call_id:
                authority["web_calls"].add(call_id)
        return authority

    def _emit_response_message(
        self,
        sink: RecordSink,
        session_id: str,
        line_number: int,
        native_id: str | None,
        role: str,
        model: str | None,
        turn_id: str | None,
        timestamp: str | None,
        content: Any,
        diagnostics: list[Diagnostic],
    ) -> None:
        message_id = deterministic_message_id(session_id, native_id, source_line=line_number)
        sink.emit(
            CanonicalMessage(
                id=message_id,
                session_id=session_id,
                sequence=line_number,
                role=role,
                provider_message_id=native_id,
                model=model,
                turn_id=turn_id,
                timestamp=timestamp,
            )
        )
        blocks = content if isinstance(content, list) else [content]
        for index, raw_block in enumerate(blocks):
            block_type = "unknown_event"
            visibility = "visible"
            text: str | None = None
            data: dict[str, Any] = {}
            mime_type: str | None = None
            if isinstance(raw_block, str):
                block_type = "text"
                text = raw_block
            elif isinstance(raw_block, dict):
                native_type = _string(raw_block.get("type")) or ""
                if native_type in {"input_text", "output_text", "text", "refusal"}:
                    block_type = "text"
                    text = _string(raw_block.get("text")) or _text_value(raw_block.get("content"))
                elif native_type in {"input_image", "output_image", "image", "attachment"}:
                    block_type = "attachment"
                    mime_type = _string(raw_block.get("mime_type"))
                    data = _attachment_metadata(raw_block)
                else:
                    visibility = "hidden"
                    data = strip_opaque(raw_block)
                    _append_diagnostic(
                        diagnostics,
                        bounded_diagnostic(
                            "unknown_content_block",
                            f"Unknown Codex message block type {native_type or '<empty>'}",
                            line=line_number,
                        ),
                    )
            elif raw_block is not None:
                data = {"value": raw_block}
            sink.emit(
                CanonicalBlock(
                    id=deterministic_block_id(message_id, None, index),
                    message_id=message_id,
                    session_id=session_id,
                    sequence=index,
                    block_type=block_type,  # type: ignore[arg-type]
                    visibility=visibility,  # type: ignore[arg-type]
                    text=text,
                    data=data,
                    mime_type=mime_type,
                )
            )

    def _emit_single_block_message(
        self,
        sink: RecordSink,
        session_id: str,
        line_number: int,
        native_id: str | None,
        role: str,
        block_type: str,
        text: str | None,
        data: Mapping[str, Any],
        turn_id: str | None,
        timestamp: str | None,
        *,
        call_id: str | None = None,
        is_error: bool = False,
        visibility: str = "visible",
    ) -> None:
        message_id = deterministic_message_id(session_id, native_id, source_line=line_number)
        sink.emit(
            CanonicalMessage(
                id=message_id,
                session_id=session_id,
                sequence=line_number,
                role=role,
                provider_message_id=native_id,
                turn_id=turn_id,
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
                is_error=is_error,
            )
        )

    def _emit_unknown(
        self,
        sink: RecordSink,
        session_id: str,
        line_number: int,
        payload: Mapping[str, Any],
        turn_id: str | None,
        timestamp: str | None,
    ) -> None:
        self._emit_single_block_message(
            sink,
            session_id,
            line_number,
            None,
            "system",
            "unknown_event",
            None,
            strip_opaque(payload),
            turn_id,
            timestamp,
            visibility="hidden",
        )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _id_from_filename(path: Path) -> str | None:
    matches = _UUID_RE.findall(path.name)
    return matches[-1].lower() if matches else None


def _classify_thread_source(source: Any) -> str:
    def labels(value: Any) -> Iterable[str]:
        if isinstance(value, str):
            yield value.lower()
        elif isinstance(value, dict):
            for key, child in value.items():
                yield str(key).lower()
                yield from labels(child)
        elif isinstance(value, list):
            for child in value:
                yield from labels(child)

    observed = set(labels(source))
    if any("subagent" in label or "thread_spawn" in label for label in observed):
        return "subagent"
    if any("automation" in label or "scheduled" in label for label in observed):
        return "automation"
    if observed:
        return "user"
    return "unknown"


def _source_label(source: Any) -> str | None:
    if isinstance(source, str):
        return source
    if isinstance(source, dict) and source:
        return str(next(iter(source)))
    return None


def _visible_summary(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = _string(item.get("text")) or _string(item.get("summary_text"))
                if text:
                    parts.append(text)
        return "\n\n".join(parts) or None
    if isinstance(value, dict):
        return _string(value.get("text")) or _string(value.get("summary_text"))
    return None


def _summary_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


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
    result: dict[str, Any] = {"type": block.get("type"), "content_omitted": True}
    for key in ("name", "file_name", "mime_type", "detail"):
        if key in block:
            result[key] = block[key]
    for key in ("image_url", "url", "data"):
        value = block.get(key)
        if isinstance(value, str):
            result[f"{key}_size"] = len(value)
    return result


def _has_content(value: Mapping[str, Any]) -> bool:
    content_keys = {
        "message",
        "content",
        "text",
        "summary",
        "error",
        "output",
        "result",
        "formatted_output",
        "aggregated_output",
        "stderr",
        "stdout",
    }
    return any(key in value and value[key] not in (None, "", [], {}) for key in content_keys)


def _looks_like_telemetry(event_type: str) -> bool:
    lowered = event_type.lower()
    return any(token in lowered for token in ("rate_limit", "timing", "heartbeat", "usage_update"))


def _merge_usage(target: dict[str, int], value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, int) and not isinstance(child, bool) and "token" in key.lower():
                target[key] = max(target.get(key, 0), child)
            else:
                _merge_usage(target, child)
    elif isinstance(value, list):
        for child in value:
            _merge_usage(target, child)


def _is_error_output(output: Any, payload: Mapping[str, Any]) -> bool:
    if payload.get("is_error") is True:
        return True
    if payload.get("success") is False:
        return True
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    status = _string(payload.get("status"))
    return bool(status and status.lower() in {"failed", "error", "cancelled"})


def _append_diagnostic(diagnostics: list[Diagnostic], diagnostic: Diagnostic) -> None:
    if len(diagnostics) < _MAX_DIAGNOSTICS:
        diagnostics.append(diagnostic)


CODEX_ADAPTER = CodexAdapter()
