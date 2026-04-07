"""Metadata extraction from parsed sessions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vimgym.pipeline.parser import ParsedSession


@dataclass
class SessionMetadata:
    session_uuid: str
    project_name: str
    duration_secs: int | None
    message_count: int
    user_message_count: int
    asst_message_count: int
    tool_use_count: int
    files_modified_display: list[str]


def decode_project_name(project_dir: str, cwd: str | None) -> str:
    """Derive human-readable project name.

    Primary source: cwd field (ground truth from JSONL).
    Fallback: last segment of dash-encoded project_dir.
    """
    if cwd:
        return Path(cwd).name or "unknown"
    parts = project_dir.lstrip("-").split("-")
    return parts[-1] if parts else "unknown"


def extract_metadata(session: "ParsedSession") -> SessionMetadata:
    project_name = decode_project_name(session.project_dir, session.cwd)

    duration_secs: int | None = None
    if session.started_at and session.ended_at:
        try:
            start = datetime.fromisoformat(session.started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(session.ended_at.replace("Z", "+00:00"))
            duration_secs = max(0, int((end - start).total_seconds()))
        except (ValueError, TypeError):
            pass

    user_count = sum(1 for m in session.messages if m.role == "user")
    asst_count = sum(1 for m in session.messages if m.role == "assistant")
    tool_count = sum(len(m.tool_names) for m in session.messages)

    cwd_prefix = (session.cwd or "").rstrip("/") + "/"
    files_display = [
        f.replace(cwd_prefix, "") if cwd_prefix != "/" and f.startswith(cwd_prefix) else f
        for f in session.files_modified
    ]

    return SessionMetadata(
        session_uuid=session.session_uuid,
        project_name=project_name,
        duration_secs=duration_secs,
        message_count=len(session.messages),
        user_message_count=user_count,
        asst_message_count=asst_count,
        tool_use_count=tool_count,
        files_modified_display=files_display,
    )
