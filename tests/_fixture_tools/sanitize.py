#!/usr/bin/env python3
"""One-shot sanitizer: turn private dev sessions into committable test fixtures.

This script reads the gitignored developer-local sessions under
``data/-Users-shoaibrain-edforge/`` and writes sanitized copies to
``tests/fixtures/sessions/-Users-example-edforge/``. The sanitized files
preserve every structural property the test suite asserts on (UUIDs,
session counts, branch names, slugs, token totals, tool names, file
counts) but replace every piece of free-form prose (user prompts,
assistant text, AI titles, file contents in tool inputs, file paths
in snapshots) with deterministic placeholder strings.

The script is committed alongside the fixtures so the sanitization is
reproducible: if a developer later refreshes ``data/`` with new sessions,
running this script regenerates the fixtures byte-for-byte.

USAGE
    python3 tests/_fixture_tools/sanitize.py

DESIGN NOTES
- Project name preservation: the sanitized ``cwd`` is rewritten to
  ``/Users/example/edforge`` so ``decode_project_name`` still returns
  ``"edforge"``. The fixture directory is named ``-Users-example-edforge``
  to match the dash-encoded form.
- Search keyword preservation: any time the original user/assistant text
  contained one of the assertion keywords (``CORS``, ``edforge``,
  ``CloudFormation``, ``auth``), that keyword is preserved in the
  sanitized placeholder so FTS5 searches still find the right session.
- Token counts: copied through unchanged from ``message.usage``.
- File paths: tool ``input.file_path`` and snapshot tracked-file keys
  are rewritten to neutral ``src/example_<n>.py`` paths, preserving
  count and uniqueness so tests asserting "X files modified" still pass.
- Determinism: the same input always produces the same output, so re-runs
  are no-ops in git.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parents[2]
SOURCE_DIR = REPO / "data" / "-Users-shoaibrain-edforge"
TARGET_DIR = REPO / "tests" / "fixtures" / "sessions" / "-Users-example-edforge"

# ── Substitution rules ────────────────────────────────────────────────────
ORIGINAL_USER = "shoaibrain"
SAFE_USER = "example"

ORIGINAL_CWD_PREFIX = f"/Users/{ORIGINAL_USER}/"
SAFE_CWD_PREFIX = f"/Users/{SAFE_USER}/"

# Keywords the test suite searches for or asserts on. If a sanitized text
# originally contained one of these, the placeholder retains it so FTS5
# searches and substring assertions still work.
SEARCH_KEYWORDS = (
    "CORS",
    "CloudFormation",
    "edforge",
    "auth",
)

# Stable text used in placeholders. Deliberately bland; no real prose.
PLACEHOLDER_USER_PROMPT = "[sanitized user prompt #{n}]"
PLACEHOLDER_ASST_TEXT = "[sanitized assistant response #{n}]"
PLACEHOLDER_THINKING = "[sanitized thinking block]"
PLACEHOLDER_TOOL_TEXT = "[sanitized tool content]"
PLACEHOLDER_TITLE = "Sanitized session title"
PLACEHOLDER_LAST_PROMPT = "[sanitized last prompt]"


def _scrub_path(p: str, counter: dict) -> str:
    """Rewrite a real file path to a deterministic safe path.

    Same input → same output (per file), so the count of unique
    files is preserved.
    """
    if p in counter:
        return counter[p]
    n = len(counter) + 1
    safe = f"src/example_{n:02d}.py"
    counter[p] = safe
    return safe


def _scrub_text(s: str) -> str:
    """Rewrite a body of free-form text, preserving search keywords."""
    if not isinstance(s, str) or not s:
        return s
    kept = sorted({kw for kw in SEARCH_KEYWORDS if kw in s})
    if kept:
        return f"[sanitized text; keywords: {', '.join(kept)}]"
    return "[sanitized text]"


def _scrub_user_content_blocks(content: list, file_counter: dict) -> list:
    """Sanitize content blocks of a user message in place-style.

    Returns a NEW list. Preserves block types so the parser still sees the
    same structure (text/image/tool_result counts must be unchanged).
    """
    out: list = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype == "text":
            new = dict(block)
            new["text"] = _scrub_text(block.get("text", ""))
            out.append(new)
        elif btype == "image":
            # Keep the structural marker; the parser will strip the source
            # blob anyway. Use a small known media_type so determinism holds.
            out.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": ""
            }})
        elif btype == "tool_result":
            new = dict(block)
            inner = block.get("content")
            if isinstance(inner, list):
                new_inner = []
                for sub in inner:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        new_sub = dict(sub)
                        new_sub["text"] = _scrub_text(sub.get("text", ""))
                        new_inner.append(new_sub)
                    else:
                        new_inner.append(sub)
                new["content"] = new_inner
            elif isinstance(inner, str):
                new["content"] = _scrub_text(inner)
            out.append(new)
        else:
            out.append(block)
    return out


def _scrub_tool_use_input(tool_name: str, inp, file_counter: dict):
    """Sanitize a tool_use ``input`` payload by tool name.

    Preserves field shape so the parser's tool/file tracking still works.
    """
    if not isinstance(inp, dict):
        return inp

    out = dict(inp)

    # File-targeting tools: keep file_path key, rewrite the value to a safe path.
    if tool_name in ("Write", "Edit"):
        for key in ("file_path", "path"):
            if key in out and isinstance(out[key], str):
                out[key] = _scrub_path(out[key], file_counter)
        for key in ("content", "new_string", "old_string"):
            if key in out and isinstance(out[key], str):
                out[key] = PLACEHOLDER_TOOL_TEXT
        return out

    if tool_name == "Read":
        if "file_path" in out and isinstance(out["file_path"], str):
            out["file_path"] = _scrub_path(out["file_path"], file_counter)
        return out

    if tool_name == "Bash":
        if "command" in out:
            out["command"] = "echo sanitized"
        if "description" in out:
            out["description"] = "sanitized command"
        return out

    if tool_name in ("Grep", "Glob"):
        for key in ("pattern", "path"):
            if key in out and isinstance(out[key], str):
                out[key] = "sanitized"
        return out

    if tool_name == "Agent":
        for key in ("description", "prompt"):
            if key in out and isinstance(out[key], str):
                out[key] = PLACEHOLDER_TOOL_TEXT
        return out

    if tool_name == "TodoWrite":
        out["todos"] = []
        return out

    if tool_name == "ToolSearch":
        for key in ("query", "max_results"):
            if key in out and isinstance(out[key], str):
                out[key] = "sanitized"
        return out

    if tool_name == "AskUserQuestion":
        out["questions"] = []
        return out

    if tool_name == "ExitPlanMode":
        for key in ("plan", "planFilePath"):
            if key in out:
                out[key] = "[sanitized]"
        return out

    # Unknown tool: stringify every leaf to a placeholder.
    return {k: ("[sanitized]" if isinstance(v, str) else v) for k, v in out.items()}


def _scrub_assistant_content_blocks(content: list, file_counter: dict) -> list:
    out: list = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype == "text":
            new = dict(block)
            new["text"] = _scrub_text(block.get("text", ""))
            out.append(new)
        elif btype == "thinking":
            new = dict(block)
            # Parser keeps `type` and stores the cleaned form; the original
            # text never reaches the FTS index, but we still scrub it for
            # belt-and-braces safety.
            new["thinking"] = PLACEHOLDER_THINKING
            out.append(new)
        elif btype == "tool_use":
            new = dict(block)
            new["input"] = _scrub_tool_use_input(
                block.get("name", ""), block.get("input"), file_counter
            )
            out.append(new)
        elif btype == "image":
            out.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": ""
            }})
        else:
            out.append(block)
    return out


def _scrub_cwd(cwd: str | None) -> str | None:
    if not isinstance(cwd, str):
        return cwd
    if cwd.startswith(ORIGINAL_CWD_PREFIX):
        return SAFE_CWD_PREFIX + cwd[len(ORIGINAL_CWD_PREFIX):]
    return cwd


# Token-count keys the parser reads from message.usage.
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _scrub_obj(obj: dict, file_counter: dict) -> dict:
    """Sanitize one parsed JSONL line.

    Whitelist-only: returns a NEW dict containing exactly the fields the
    vimgym parser reads ([src/vimgym/pipeline/parser.py](src/vimgym/pipeline/parser.py)).
    Anything else (messageId, requestId, promptId, isSidechain, inference_geo,
    service_tier, etc.) is dropped to keep test fixtures small.
    """
    msg_type = obj.get("type", "")
    out: dict = {"type": msg_type}

    # Common identity / time fields the parser may read on multiple types.
    for key in ("uuid", "parentUuid", "sessionId", "timestamp"):
        if key in obj:
            out[key] = obj[key]

    if msg_type == "queue-operation":
        if "operation" in obj:
            out["operation"] = obj["operation"]
        return out

    if msg_type == "user":
        # Pass through structural fields the parser reads.
        for key in (
            "isMeta",
            "entrypoint",
            "version",
            "slug",
            "permissionMode",
            "gitBranch",
        ):
            if key in obj:
                out[key] = obj[key]
        if "cwd" in obj:
            out["cwd"] = _scrub_cwd(obj["cwd"])

        if obj.get("isMeta"):
            # Parser ignores meta messages; emit a minimal stub.
            return out

        msg = obj.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            new_content: list = []
            if isinstance(content, list):
                new_content = _scrub_user_content_blocks(content, file_counter)
            elif isinstance(content, str):
                new_content = [{"type": "text", "text": _scrub_text(content)}]
            out["message"] = {"role": "user", "content": new_content}
        return out

    if msg_type == "assistant":
        msg = obj.get("message")
        if isinstance(msg, dict):
            new_msg: dict = {"role": "assistant"}
            content = msg.get("content")
            if isinstance(content, list):
                new_msg["content"] = _scrub_assistant_content_blocks(content, file_counter)
            else:
                new_msg["content"] = []
            usage = msg.get("usage") or {}
            if isinstance(usage, dict):
                trimmed = {k: usage[k] for k in USAGE_KEYS if k in usage}
                if trimmed:
                    new_msg["usage"] = trimmed
            out["message"] = new_msg
        return out

    if msg_type == "file-history-snapshot":
        snap = obj.get("snapshot")
        new_snap: dict = {}
        if isinstance(snap, dict):
            tfb = snap.get("trackedFileBackups")
            if isinstance(tfb, dict) and tfb:
                new_snap["trackedFileBackups"] = {
                    _scrub_path(path, file_counter): {"version": 1}
                    for path in tfb.keys()
                }
            else:
                new_snap["trackedFileBackups"] = {}
        out["snapshot"] = new_snap
        return out

    if msg_type == "ai-title":
        # Preserve search keywords from the original title so substring
        # assertions like `"CloudFormation" in row["ai_title"]` still pass.
        original = obj.get("aiTitle") or ""
        kept = sorted({kw for kw in SEARCH_KEYWORDS if kw in original})
        if kept:
            out["aiTitle"] = f"Sanitized title — {' '.join(kept)}"
        else:
            out["aiTitle"] = PLACEHOLDER_TITLE
        return out

    if msg_type == "last-prompt":
        out["lastPrompt"] = PLACEHOLDER_LAST_PROMPT
        return out

    # Unknown type — keep the type so the parser still records it as
    # "unknown type" in parse_errors (matching the original session shape).
    return out


def sanitize_file(src: Path, dst: Path) -> None:
    """Read one JSONL file, write its sanitized counterpart."""
    file_counter: dict = {}
    out_lines: list[str] = []
    raw = src.read_text(encoding="utf-8")
    for line in raw.splitlines():
        if not line.strip():
            out_lines.append(line)
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # Drop the bad line — its content is unparseable, so we can't
            # safely scrub it.
            continue
        scrubbed = _scrub_obj(obj, file_counter)
        out_lines.append(json.dumps(scrubbed, ensure_ascii=False))

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def main() -> int:
    if not SOURCE_DIR.exists():
        print(f"error: source dir not found: {SOURCE_DIR}", file=sys.stderr)
        print("       (this script must be run on the developer machine where", file=sys.stderr)
        print("        the gitignored data/ directory exists)", file=sys.stderr)
        return 1

    TARGET_DIR.mkdir(parents=True, exist_ok=True)

    sources = sorted(SOURCE_DIR.glob("*.jsonl"))
    if not sources:
        print(f"error: no .jsonl files in {SOURCE_DIR}", file=sys.stderr)
        return 1

    print(f"sanitizing {len(sources)} session files")
    print(f"  src: {SOURCE_DIR}")
    print(f"  dst: {TARGET_DIR}")
    print()

    for src in sources:
        dst = TARGET_DIR / src.name
        sanitize_file(src, dst)
        print(f"  ✓ {src.name}  ({src.stat().st_size:>6d}B → {dst.stat().st_size:>6d}B)")

    print()
    print(f"done. {len(sources)} fixture(s) written.")
    print(f"verify: ls {TARGET_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
