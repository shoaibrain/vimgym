from pathlib import Path

from vimgym.pipeline.redact import RedactionEngine

RULES = Path(__file__).parent.parent / "defaults" / "redaction-rules.json"


def engine() -> RedactionEngine:
    return RedactionEngine(RULES)


def test_anthropic_key_redacted():
    e = engine()
    text = "key=sk-ant-" + "a" * 80 + " end"
    out = e.redact_text(text)
    assert "sk-ant-" not in out
    assert "REDACTED_ANTHROPIC_KEY" in out


def test_aws_access_key_redacted():
    e = engine()
    out = e.redact_text("AKIA" + "B" * 16)
    assert "REDACTED_AWS_KEY" in out


def test_bearer_token_redacted():
    e = engine()
    out = e.redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123")
    assert "Bearer [REDACTED]" in out
    assert "abcdefghijklmnopqrstuvwxyz123" not in out


def test_github_token_redacted():
    e = engine()
    out = e.redact_text("ghp_" + "x" * 40)
    assert "REDACTED_GITHUB_TOKEN" in out


def test_jwt_redacted():
    e = engine()
    jwt = "eyJabc.eyJdef.signaturepart"
    out = e.redact_text(f"token={jwt}")
    assert "REDACTED_JWT" in out


def test_database_url_redacted():
    e = engine()
    out = e.redact_text("postgres://user:passwordlongenough@host/db")
    assert "REDACTED_DB_URL" in out


def test_pem_block_multiline_redacted():
    e = engine()
    pem = "-----BEGIN PRIVATE KEY-----\nABCDEFG\nHIJKLMN\n-----END PRIVATE KEY-----"
    out = e.redact_text(pem)
    assert "REDACTED_PEM_BLOCK" in out
    assert "ABCDEFG" not in out


def test_normal_code_unchanged():
    e = engine()
    code = "def add(a, b):\n    return a + b\n"
    assert e.redact_text(code) == code


def test_redact_session_raw_handles_jsonl(tmp_path):
    e = engine()
    line1 = '{"type":"user","msg":"hello"}'
    line2 = '{"key":"sk-ant-' + "x" * 80 + '"}'
    raw = "\n".join([line1, line2])
    out = e.redact_session_raw(raw)
    assert "hello" in out
    assert "sk-ant-" not in out


def test_redact_session_raw_skips_blank_and_malformed():
    e = engine()
    raw = '\n{"good": "ok"}\nnot json\n'
    out = e.redact_session_raw(raw)
    assert "good" in out
