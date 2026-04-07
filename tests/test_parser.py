import re
from pathlib import Path

from vimgym.pipeline.parser import ParsedMessage, ParsedSession, parse_session

DATA_DIR = Path(__file__).parent / "fixtures" / "sessions" / "-Users-example-edforge"


def test_parsed_message_defaults():
    msg = ParsedMessage(
        uuid="abc", parent_uuid=None, type="user",
        role="user", timestamp=None,
    )
    assert msg.has_tool_use is False
    assert msg.tool_names == []
    assert msg.content_json == "[]"


def test_parsed_session_defaults():
    s = ParsedSession(
        session_uuid="abc", slug=None, ai_title=None, last_prompt=None,
        source_path="/tmp/x.jsonl", project_dir="-Users-test",
        cwd=None, git_branch=None, entrypoint=None,
        claude_version=None, permission_mode=None,
        started_at=None, ended_at=None,
    )
    assert s.tools_used == []
    assert s.has_subagents is False
    assert s.input_tokens == 0


def test_parse_simple_session():
    path = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    s = parse_session(path)
    assert s.session_uuid == "eaa3009a-c5ab-4015-a3e5-af26622652f9"
    assert s.ai_title is not None and "CloudFormation" in s.ai_title
    assert s.slug == "wise-purring-flute"
    assert s.cwd is not None
    assert s.git_branch is not None
    assert len(s.messages) > 0
    assert s.parse_errors == []


def test_parse_agents_session():
    path = DATA_DIR / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl"
    s = parse_session(path)
    assert s.session_uuid == "3438c55b-0df0-4bc0-811e-561afcf19350"
    assert s.has_subagents is True
    assert "Agent" in s.tools_used
    assert "Bash" in s.tools_used
    assert "Edit" in s.tools_used
    assert len(s.tools_used) >= 8


def test_parse_minimal_session():
    path = DATA_DIR / "1fb8b1b8-6cb3-4e34-8446-fa60ba5df626.jsonl"
    s = parse_session(path)
    assert s.session_uuid != ""
    assert s.parse_errors == []


def test_all_sessions_parse_without_crash():
    for path in DATA_DIR.glob("*.jsonl"):
        s = parse_session(path)
        assert s.session_uuid != "", f"No session UUID extracted from {path.name}"


def test_image_base64_not_in_raw_jsonl():
    for path in DATA_DIR.glob("*.jsonl"):
        s = parse_session(path)
        if s.messages and any(m.has_image for m in s.messages):
            assert "omitted" in s.raw_jsonl
            assert not re.search(r'"data":\s*"[A-Za-z0-9+/]{1000,}', s.raw_jsonl)


def test_file_hash_is_stable():
    path = DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    s1 = parse_session(path)
    s2 = parse_session(path)
    assert s1.file_hash == s2.file_hash
    assert len(s1.file_hash) == 64


def test_token_counts_nonzero_for_large_session():
    path = DATA_DIR / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl"
    s = parse_session(path)
    assert s.output_tokens > 0
    assert s.cache_read_tokens > 0
