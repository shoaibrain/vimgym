"""Streaming provider-neutral Markdown and canonical JSONL exports."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any


def _row(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _metadata_markdown(session: Any) -> str:
    title = _row(session, "title") or _row(session, "ai_title") or "Untitled session"
    lines = [f"# {title}", "", "## Metadata", ""]
    fields = (
        ("Provider", _row(session, "provider")),
        ("Originator", _row(session, "originator")),
        ("Session type", _row(session, "kind")),
        ("Lifecycle", _row(session, "lifecycle")),
        ("Project", _row(session, "project_name")),
        ("Branch", _row(session, "git_branch")),
        ("CWD", _row(session, "cwd")),
        ("Started", _row(session, "started_at")),
        ("Model", _row(session, "model")),
        ("Client", _row(session, "client_name")),
        ("Parent session", _row(session, "parent_session_id")),
        ("Root session", _row(session, "root_session_id")),
        (
            "External session ID",
            _row(session, "external_session_id") or _row(session, "session_uuid"),
        ),
    )
    for label, value in fields:
        if value not in (None, ""):
            lines.append(f"- **{label}:** `{value}`")
    lines.extend(["", "## Conversation", ""])
    return "\n".join(lines) + "\n"


def _block_markdown(block: dict[str, Any]) -> str:
    kind = block.get("kind") or block.get("type") or "unknown_event"
    text = block.get("text_content")
    if text is None:
        text = block.get("text")
    data = block.get("data")
    if data is None and block.get("data_json"):
        try:
            data = json.loads(str(block["data_json"]))
        except json.JSONDecodeError:
            data = block["data_json"]
    if kind == "text":
        return f"{text or ''}\n\n"
    if kind == "tool_call":
        name = block.get("name") or "tool"
        payload = data if data is not None else text
        if isinstance(payload, dict) and payload.get("input") is not None:
            payload = payload["input"]
        return f"**Tool call: {name}**\n\n```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```\n\n"
    if kind == "tool_result":
        return f"**Tool result**\n\n```text\n{text or _text_from_data(data)}\n```\n\n"
    if kind == "reasoning_summary":
        return f"> **Visible reasoning summary:** {text or _text_from_data(data)}\n\n"
    if kind == "compaction":
        return f"> **Compaction summary:** {text or _text_from_data(data)}\n\n"
    if kind == "attachment":
        mime = block.get("mime_type") or "attachment"
        return f"> _[{mime} metadata retained; attachment bytes omitted]_\n\n"
    if kind == "omitted":
        return "> _[provider content omitted by capture policy]_\n\n"
    if block.get("visibility") == "visible" and (text or data):
        return f"{text or _text_from_data(data)}\n\n"
    return ""


def _text_from_data(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _canonical_block_record(block: sqlite3.Row) -> dict[str, Any]:
    try:
        data: Any = json.loads(block["data_json"] or "{}")
    except json.JSONDecodeError:
        data = block["data_json"]
    return {
        "id": block["id"],
        "kind": block["kind"],
        "visibility": block["visibility"],
        "text": block["text_content"],
        "data": data,
        "name": block["name"],
        "call_id": block["call_id"],
        "mime_type": block["mime_type"],
        "is_error": bool(block["is_error"]),
        "truncated": bool(block["truncated"]),
        "original_bytes": int(block["original_bytes"] or 0),
    }


def iter_session_markdown(
    conn: sqlite3.Connection,
    session: sqlite3.Row,
    *,
    batch_size: int = 100,
) -> Iterator[str]:
    """Yield Markdown in bounded batches without materializing a transcript."""
    yield _metadata_markdown(session)
    after = 0
    while True:
        messages = conn.execute(
            """
            SELECT * FROM messages WHERE session_id=? AND sequence>?
            ORDER BY sequence,item_index LIMIT ?
            """,
            (session["id"], after, batch_size),
        ).fetchall()
        if not messages:
            return
        for message in messages:
            role = str(message["role"] or "unknown").capitalize()
            model = f" ({message['model']})" if message["model"] else ""
            timestamp = f" — {message['timestamp']}" if message["timestamp"] else ""
            yield f"### {role}{model}{timestamp}\n\n"
            blocks = conn.execute(
                "SELECT * FROM message_blocks WHERE message_id=? ORDER BY block_index",
                (message["id"],),
            )
            for block in blocks:
                yield _block_markdown(dict(block))
            yield "---\n\n"
            after = max(after, int(message["sequence"]))


def iter_session_canonical_jsonl(
    conn: sqlite3.Connection,
    session: sqlite3.Row,
    *,
    batch_size: int = 100,
) -> Iterator[str]:
    session_record = {
        "record_type": "session",
        **{
            key: session[key]
            for key in (
                "id",
                "provider",
                "external_session_id",
                "kind",
                "lifecycle",
                "parent_session_id",
                "root_session_id",
                "originator",
                "client_name",
                "client_version",
                "model",
                "title",
                "project_name",
                "cwd",
                "git_branch",
                "started_at",
                "updated_at",
                "ended_at",
                "revision",
                "health",
            )
        },
    }
    yield json.dumps(session_record, ensure_ascii=False, separators=(",", ":")) + "\n"
    after = 0
    while True:
        messages = conn.execute(
            """
            SELECT * FROM messages WHERE session_id=? AND sequence>?
            ORDER BY sequence,item_index LIMIT ?
            """,
            (session["id"], after, batch_size),
        ).fetchall()
        if not messages:
            return
        for message in messages:
            record = {
                "record_type": "message",
                "id": message["id"],
                "external_message_id": message["external_message_id"],
                "sequence": message["sequence"],
                "role": message["role"],
                "model": message["model"],
                "turn_id": message["turn_id"],
                "timestamp": message["timestamp"],
                "parent_message_id": message["parent_message_id"],
            }
            prefix = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            yield prefix[:-1] + ',"blocks":['
            first_block = True
            for block in conn.execute(
                "SELECT * FROM message_blocks WHERE message_id=? ORDER BY block_index",
                (message["id"],),
            ):
                if not first_block:
                    yield ","
                yield json.dumps(
                    _canonical_block_record(block),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                first_block = False
            yield "]}\n"
            after = max(after, int(message["sequence"]))


def render_session_markdown(session: sqlite3.Row, messages: Sequence[sqlite3.Row]) -> str:
    """Compatibility renderer for callers that already loaded legacy rows."""
    lines = [_metadata_markdown(session)]
    for message in messages:
        role = str(_row(message, "role") or "unknown").capitalize()
        timestamp = _row(message, "timestamp") or ""
        lines.append(f"### {role}{' — ' + timestamp if timestamp else ''}\n\n")
        try:
            blocks = json.loads(_row(message, "content_json") or "[]")
        except json.JSONDecodeError:
            blocks = []
        for block in blocks:
            if isinstance(block, dict):
                native = block.get("type")
                mapped = {
                    "tool_use": "tool_call",
                    "tool_result": "tool_result",
                    "thinking": "omitted",
                    "image": "attachment",
                }.get(str(native or ""), native)
                lines.append(_block_markdown({**block, "kind": mapped, "data": block}))
        lines.append("---\n\n")
    return "".join(lines)
