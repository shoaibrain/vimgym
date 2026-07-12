"""Provider-neutral capture API and Vimgym's built-in Claude/Codex adapters."""

from .base import (
    ArtifactCandidate,
    ArtifactIdentity,
    CanonicalBlock,
    CanonicalMessage,
    CanonicalRecord,
    CanonicalSession,
    CollectingSink,
    Diagnostic,
    ParseOutcome,
    ProviderAdapter,
    RecordSink,
    RedactedRecord,
    SourceSpec,
    deterministic_block_id,
    deterministic_message_id,
    deterministic_session_id,
    deterministic_workspace_id,
)
from .claude import CLAUDE_ADAPTER, ClaudeAdapter
from .codex import CODEX_ADAPTER, CodexAdapter
from .registry import BUILTIN_ADAPTERS, default_sources, get_adapter, iter_adapters

__all__ = [
    "ArtifactCandidate",
    "ArtifactIdentity",
    "BUILTIN_ADAPTERS",
    "CLAUDE_ADAPTER",
    "CODEX_ADAPTER",
    "CanonicalBlock",
    "CanonicalMessage",
    "CanonicalRecord",
    "CanonicalSession",
    "ClaudeAdapter",
    "CodexAdapter",
    "CollectingSink",
    "Diagnostic",
    "ParseOutcome",
    "ProviderAdapter",
    "RecordSink",
    "RedactedRecord",
    "SourceSpec",
    "default_sources",
    "deterministic_block_id",
    "deterministic_message_id",
    "deterministic_session_id",
    "deterministic_workspace_id",
    "get_adapter",
    "iter_adapters",
]
