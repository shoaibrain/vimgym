"""Session summarization (heuristic, no external API)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vimgym.pipeline.parser import ParsedSession


def heuristic_summary(session: "ParsedSession") -> str:
    """Brief summary, max 280 chars: title + first prompt + files + tools."""
    parts: list[str] = []

    title = session.ai_title or "Untitled session"
    parts.append(title)

    for msg in session.messages:
        if msg.role == "user" and msg.content_json != "[]":
            try:
                content = json.loads(msg.content_json)
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            truncated = text[:120] + ("..." if len(text) > 120 else "")
                            parts.append(truncated)
                            break
            except Exception:
                pass
            break

    if session.files_modified:
        top = session.files_modified[:3]
        suffix = "..." if len(session.files_modified) > 3 else ""
        names = [Path(f).name for f in top]
        parts.append(f"Files: {', '.join(names)}{suffix}")

    if session.tools_used:
        parts.append(f"Tools: {', '.join(session.tools_used[:5])}")

    summary = ". ".join(parts)
    if len(summary) > 280:
        summary = summary[:277] + "..."
    return summary
