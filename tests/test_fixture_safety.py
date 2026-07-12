from __future__ import annotations

import json
import importlib.util
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
_SANITIZER = Path(__file__).parent / "_fixture_tools" / "sanitize.py"
_SPEC = importlib.util.spec_from_file_location("vimgym_fixture_sanitizer", _SANITIZER)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_scrub_obj = _MODULE._scrub_obj
FORBIDDEN = (
    re.compile(rb"shoaibrain", re.I),
    re.compile(rb"/Users/(?!example(?:/|\b))", re.I),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(rb'"signature"\s*:'),
    re.compile(rb"[A-Za-z0-9+/]{300,}={0,2}"),
)


def test_committed_fixtures_contain_no_private_or_opaque_payloads() -> None:
    for path in FIXTURES.rglob("*"):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        for pattern in FORBIDDEN:
            assert not pattern.search(payload), f"fixture leak in {path}: {pattern.pattern!r}"


def test_sanitizer_is_byte_deterministic() -> None:
    record = {
        "type": "assistant",
        "uuid": "message-1",
        "sessionId": "session-1",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private", "signature": "opaque"},
                {"type": "text", "text": "CORS details"},
            ],
        },
    }
    first = json.dumps(_scrub_obj(record, {}), sort_keys=True, separators=(",", ":"))
    second = json.dumps(_scrub_obj(record, {}), sort_keys=True, separators=(",", ":"))
    assert first.encode() == second.encode()
    assert "opaque" not in first
