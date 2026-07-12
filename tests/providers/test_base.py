from __future__ import annotations

from pathlib import Path

import pytest

from vimgym.providers import (
    CollectingSink,
    RedactedRecord,
    default_sources,
    deterministic_block_id,
    deterministic_message_id,
    deterministic_session_id,
    deterministic_workspace_id,
    get_adapter,
)


def test_deterministic_ids_are_stable_and_provider_scoped(tmp_path: Path) -> None:
    claude = deterministic_session_id("claude_code", "native-1")
    codex = deterministic_session_id("codex", "native-1")
    assert claude == deterministic_session_id("claude_code", "native-1")
    assert claude != codex

    native_message = deterministic_message_id(claude, "message-1", source_line=1)
    assert native_message == deterministic_message_id(claude, "message-1", source_line=99)
    positional = deterministic_message_id(claude, None, source_line=3, item_index=2)
    assert positional != deterministic_message_id(claude, None, source_line=4, item_index=2)
    assert deterministic_block_id(native_message, "call-1", 0) == deterministic_block_id(
        native_message, "call-1", 0
    )

    workspace = tmp_path / "project"
    workspace.mkdir()
    assert deterministic_workspace_id("codex", str(workspace)) == deterministic_workspace_id(
        "codex", str(workspace / ".")
    )
    assert deterministic_workspace_id("claude_code", str(workspace)) != deterministic_workspace_id(
        "codex", str(workspace)
    )


def test_source_registry_is_builtin_only(tmp_path: Path) -> None:
    sources = default_sources({"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "cx")})
    assert [(source.provider, source.lifecycle) for source in sources] == [
        ("claude_code", "active"),
        ("codex", "active"),
        ("codex", "archived"),
    ]
    assert get_adapter("claude_code").provider == "claude_code"
    assert get_adapter("codex").provider == "codex"
    with pytest.raises(ValueError, match="Unsupported provider"):
        get_adapter("gemini")


def test_redacted_record_and_sink_fail_closed() -> None:
    with pytest.raises(ValueError, match="policy hash"):
        CollectingSink(policy_hash="")
    with pytest.raises(ValueError, match="policy hash"):
        RedactedRecord(value="not-used", policy_hash="")  # type: ignore[type-var]
