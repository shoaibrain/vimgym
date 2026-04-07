"""Claude Code JSONL parser — converts raw session files to structured data."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedMessage:
    uuid: str
    parent_uuid: str | None
    type: str                     # 'user' | 'assistant'
    role: str
    timestamp: str | None
    has_tool_use: bool = False
    has_thinking: bool = False
    has_image: bool = False
    tool_names: list[str] = field(default_factory=list)
    content_json: str = "[]"      # full content array as JSON, base64 images stripped


@dataclass
class ParsedSession:
    # Identity
    session_uuid: str
    slug: str | None
    ai_title: str | None
    last_prompt: str | None

    # Source
    source_path: str
    project_dir: str              # raw encoded: -Users-shoaibrain-edforge
    cwd: str | None               # /Users/shoaibrain/edforge
    git_branch: str | None
    entrypoint: str | None        # claude-vscode
    claude_version: str | None    # 2.1.89
    permission_mode: str | None   # default | plan

    # Time (ISO8601 strings)
    started_at: str | None
    ended_at: str | None

    # Messages
    messages: list[ParsedMessage] = field(default_factory=list)
    user_messages_text: str = ""
    asst_messages_text: str = ""

    # Derived
    tools_used: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    has_subagents: bool = False

    # Token accounting
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    # Storage
    raw_jsonl: str = ""
    file_hash: str = ""
    parse_errors: list[str] = field(default_factory=list)

    # Provenance — which configured source this came from. Set by the
    # orchestrator just before upsert_session(); defaults to "claude_code"
    # for v1 since that's the only parser type.
    source_id: str = "claude_code"


def parse_session(filepath: Path) -> ParsedSession:
    """Parse a Claude Code JSONL session file into structured data.

    Streams line by line. Never raises on malformed JSON — appends to parse_errors.
    Strips base64 image data from both raw_jsonl and content_json. Skips
    user messages with isMeta=true. Computes file_hash from raw bytes before
    any modification.
    """
    raw_bytes = filepath.read_bytes()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    raw_lines = raw_bytes.decode("utf-8", errors="replace").splitlines()

    project_dir = filepath.parent.name

    session = ParsedSession(
        session_uuid="",
        slug=None,
        ai_title=None,
        last_prompt=None,
        source_path=str(filepath.resolve()),
        project_dir=project_dir,
        cwd=None,
        git_branch=None,
        entrypoint=None,
        claude_version=None,
        permission_mode=None,
        started_at=None,
        ended_at=None,
        file_hash=file_hash,
    )

    tools_used: set[str] = set()
    files_modified: set[str] = set()
    user_texts: list[str] = []
    asst_texts: list[str] = []
    redacted_lines: list[str] = []

    for line_num, raw_line in enumerate(raw_lines, 1):
        line = raw_line.strip()
        if not line:
            redacted_lines.append(raw_line)
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            session.parse_errors.append(f"line {line_num}: {e}")
            redacted_lines.append(raw_line)
            continue

        msg_type = obj.get("type", "")

        if msg_type == "queue-operation":
            if obj.get("operation") == "enqueue" and session.started_at is None:
                session.started_at = obj.get("timestamp")
            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]
            redacted_lines.append(line)

        elif msg_type == "user":
            if obj.get("isMeta"):
                redacted_lines.append(line)
                continue

            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]
            if not session.cwd and obj.get("cwd"):
                session.cwd = obj["cwd"]
            if not session.git_branch and obj.get("gitBranch"):
                session.git_branch = obj["gitBranch"]
            if not session.entrypoint and obj.get("entrypoint"):
                session.entrypoint = obj["entrypoint"]
            if not session.claude_version and obj.get("version"):
                session.claude_version = obj["version"]
            if not session.slug and obj.get("slug"):
                session.slug = obj["slug"]
            if not session.permission_mode and obj.get("permissionMode"):
                session.permission_mode = obj["permissionMode"]

            timestamp = obj.get("timestamp")
            if timestamp:
                if session.started_at is None:
                    session.started_at = timestamp
                session.ended_at = timestamp

            content = obj.get("message", {}).get("content", [])
            if not isinstance(content, list):
                content = []
            cleaned_content, has_image, text_parts = _process_content_blocks(content, for_user=True)
            user_texts.extend(text_parts)

            msg = ParsedMessage(
                uuid=obj.get("uuid", f"_line_{line_num}"),
                parent_uuid=obj.get("parentUuid"),
                type="user",
                role="user",
                timestamp=timestamp,
                has_image=has_image,
                content_json=json.dumps(cleaned_content),
            )
            session.messages.append(msg)

            obj_copy = dict(obj)
            if isinstance(obj_copy.get("message"), dict):
                obj_copy["message"] = dict(obj_copy["message"])
                obj_copy["message"]["content"] = cleaned_content
            redacted_lines.append(json.dumps(obj_copy))

        elif msg_type == "assistant":
            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]

            timestamp = obj.get("timestamp")
            if timestamp:
                if session.started_at is None:
                    session.started_at = timestamp
                session.ended_at = timestamp

            usage = obj.get("message", {}).get("usage", {}) or {}
            session.input_tokens += usage.get("input_tokens", 0) or 0
            session.output_tokens += usage.get("output_tokens", 0) or 0
            session.cache_read_tokens += usage.get("cache_read_input_tokens", 0) or 0
            session.cache_write_tokens += usage.get("cache_creation_input_tokens", 0) or 0

            content = obj.get("message", {}).get("content", [])
            if not isinstance(content, list):
                content = []
            cleaned_content, has_image, _ = _process_content_blocks(content, for_user=False)

            has_tool_use = False
            has_thinking = False
            msg_tools: list[str] = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    has_tool_use = True
                    tool_name = block.get("name", "")
                    tools_used.add(tool_name)
                    msg_tools.append(tool_name)
                    if tool_name in ("Write", "Edit"):
                        # Real Claude Code uses `file_path`, fallback to `path`.
                        inp = block.get("input", {}) or {}
                        path = inp.get("file_path") or inp.get("path") or ""
                        if path:
                            files_modified.add(path)
                    if tool_name == "Agent":
                        session.has_subagents = True
                elif btype == "thinking":
                    has_thinking = True
                elif btype == "text":
                    asst_texts.append(block.get("text", ""))

            msg = ParsedMessage(
                uuid=obj.get("uuid", f"_line_{line_num}"),
                parent_uuid=obj.get("parentUuid"),
                type="assistant",
                role="assistant",
                timestamp=timestamp,
                has_tool_use=has_tool_use,
                has_thinking=has_thinking,
                has_image=has_image,
                tool_names=msg_tools,
                content_json=json.dumps(cleaned_content),
            )
            session.messages.append(msg)

            obj_copy = dict(obj)
            if isinstance(obj_copy.get("message"), dict):
                obj_copy["message"] = dict(obj_copy["message"])
                obj_copy["message"]["content"] = cleaned_content
            redacted_lines.append(json.dumps(obj_copy))

        elif msg_type == "file-history-snapshot":
            snapshot = obj.get("snapshot", {}) or {}
            for file_path in (snapshot.get("trackedFileBackups", {}) or {}).keys():
                files_modified.add(file_path)
            redacted_lines.append(line)

        elif msg_type == "ai-title":
            session.ai_title = obj.get("aiTitle")
            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]
            redacted_lines.append(line)

        elif msg_type == "last-prompt":
            session.last_prompt = obj.get("lastPrompt")
            if not session.session_uuid and obj.get("sessionId"):
                session.session_uuid = obj["sessionId"]
            redacted_lines.append(line)

        else:
            session.parse_errors.append(f"line {line_num}: unknown type '{msg_type}'")
            redacted_lines.append(line)

    session.raw_jsonl = "\n".join(redacted_lines)
    session.user_messages_text = "\n\n".join(user_texts)
    session.asst_messages_text = "\n\n".join(asst_texts)
    session.tools_used = sorted(tools_used)
    session.files_modified = sorted(files_modified)[:50]

    return session


def _process_content_blocks(
    content: list, for_user: bool
) -> tuple[list, bool, list[str]]:
    """Strip base64 image data; collect text parts for FTS.

    Returns (cleaned_content, has_image, text_parts).
    """
    cleaned: list = []
    has_image = False
    text_parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            cleaned.append(block)
            continue

        btype = block.get("type")

        if btype == "image":
            has_image = True
            source = block.get("source", {}) or {}
            media_type = source.get("media_type", "image/unknown")
            cleaned.append({"type": "image", "omitted": True, "media_type": media_type})

        elif btype == "text":
            text = block.get("text", "")
            text_parts.append(text)
            cleaned.append(block)

        elif btype == "thinking":
            cleaned.append({"type": "thinking", "omitted": True})

        else:
            cleaned.append(block)

    return cleaned, has_image, text_parts
