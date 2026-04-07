"""Markdown export of a stored session."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def _row(row: Any, key: str, default: Any = None) -> Any:
    """sqlite3.Row supports __getitem__ but not .get()."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def render_session_markdown(session: sqlite3.Row, messages: list[sqlite3.Row]) -> str:
    """Render a session and its messages as a paste-friendly markdown document."""
    lines: list[str] = []

    title = _row(session, "ai_title") or "Untitled session"
    lines.append(f"# {title}")
    lines.append("")

    project = _row(session, "project_name") or ""
    branch = _row(session, "git_branch") or ""
    started = _row(session, "started_at") or ""
    duration = _row(session, "duration_secs") or 0
    model = _row(session, "claude_version") or ""
    uuid = _row(session, "session_uuid") or ""
    slug = _row(session, "slug") or ""
    cwd = _row(session, "cwd") or ""

    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- **Project:** `{project}`")
    if branch:
        lines.append(f"- **Branch:** `{branch}`")
    if cwd:
        lines.append(f"- **CWD:** `{cwd}`")
    if started:
        lines.append(f"- **Started:** {started}")
    if duration:
        h, rem = divmod(int(duration), 3600)
        m = rem // 60
        lines.append(f"- **Duration:** {h}h {m}m")
    if model:
        lines.append(f"- **Claude version:** {model}")
    if slug:
        lines.append(f"- **Slug:** `{slug}`")
    lines.append(f"- **Session UUID:** `{uuid}`")

    tools_raw = _row(session, "tools_used")
    if tools_raw:
        try:
            tools = json.loads(tools_raw)
            if tools:
                lines.append(f"- **Tools used:** {', '.join(f'`{t}`' for t in tools)}")
        except Exception:
            pass

    files_raw = _row(session, "files_modified")
    if files_raw:
        try:
            files = json.loads(files_raw)
            if files:
                lines.append("- **Files modified:**")
                for f in files[:50]:
                    lines.append(f"  - `{f}`")
        except Exception:
            pass

    lines.append("")
    lines.append("## Conversation")
    lines.append("")

    for msg in messages:
        role = _row(msg, "role") or "user"
        ts = _row(msg, "timestamp") or ""
        content_json = _row(msg, "content_json") or "[]"
        try:
            blocks = json.loads(content_json)
        except Exception:
            blocks = []

        header = "### 👤 User" if role == "user" else "### 🤖 Claude"
        if ts:
            header += f"  _{ts}_"
        lines.append(header)
        lines.append("")

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                lines.append(block.get("text", ""))
                lines.append("")
            elif btype == "thinking":
                lines.append("> _[thinking block omitted]_")
                lines.append("")
            elif btype == "image":
                lines.append("> _[image omitted]_")
                lines.append("")
            elif btype == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input", {}) or {}
                lines.append(f"**🔧 {name}**")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(inp, indent=2))
                lines.append("```")
                lines.append("")
            elif btype == "tool_result":
                content = block.get("content")
                text = _tool_result_text(content)
                if text:
                    lines.append("**↳ tool result**")
                    lines.append("")
                    lines.append("```")
                    lines.append(text[:4000])
                    lines.append("```")
                    lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or "")
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    return json.dumps(content)
