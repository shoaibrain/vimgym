"""Provider-neutral capture contracts used by Vimgym's built-in adapters.

Adapters are deliberately small and read-only: they discover provider artifacts,
identify the session an artifact belongs to, and stream canonical records into a
``RecordSink``.  The sink is the privacy boundary.  A production sink must
recursively redact a record before wrapping it in ``RedactedRecord`` or handing
it to storage.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Iterable, Literal, Mapping, Protocol, TypeVar, Union


ProviderName = Literal["claude_code", "codex"]
SessionKind = Literal["user", "automation", "subagent", "unknown"]
Lifecycle = Literal["active", "archived"]
ArtifactStatus = Literal["imported", "unchanged", "updated", "degraded", "failed"]
BlockType = Literal[
    "text",
    "tool_call",
    "tool_result",
    "attachment",
    "reasoning_summary",
    "compaction",
    "omitted",
    "unknown_event",
]
Visibility = Literal["visible", "hidden"]


# A fixed application namespace makes identities reproducible across machines,
# vault restores, and parser versions.  Never replace this value after release.
VIMGYM_ID_NAMESPACE = uuid.UUID("be5cb4ba-1681-50d5-bf58-b5e75cd1a87c")


def deterministic_session_id(provider: str, external_session_id: str) -> str:
    """Return the canonical UUIDv5 for a provider-native session identity."""

    if not provider or not external_session_id:
        raise ValueError("provider and external_session_id must be non-empty")
    return str(uuid.uuid5(VIMGYM_ID_NAMESPACE, f"session\0{provider}\0{external_session_id}"))


def deterministic_workspace_id(provider: str, cwd: str | None) -> str | None:
    """Return a provider-scoped workspace identity for a canonical working dir."""

    if not cwd:
        return None
    canonical = os.path.normcase(os.path.realpath(os.path.expanduser(cwd)))
    return str(uuid.uuid5(VIMGYM_ID_NAMESPACE, f"workspace\0{provider}\0{canonical}"))


def deterministic_message_id(
    session_id: str,
    provider_native_id: str | None,
    *,
    source_line: int,
    item_index: int = 0,
) -> str:
    """Return a message UUID using native identity or stable source position."""

    namespace = uuid.UUID(session_id)
    identity = (
        f"native\0{provider_native_id}"
        if provider_native_id
        else f"source\0{source_line}\0{item_index}"
    )
    return str(uuid.uuid5(namespace, f"message\0{identity}"))


def deterministic_block_id(message_id: str, provider_native_id: str | None, index: int) -> str:
    """Return a deterministic block UUID within a canonical message."""

    namespace = uuid.UUID(message_id)
    identity = provider_native_id or str(index)
    return str(uuid.uuid5(namespace, f"block\0{identity}\0{index}"))


@dataclass(frozen=True)
class SourceSpec:
    """A configured provider root.

    ``root`` is operational metadata and is intentionally not redacted.  Any
    provider-derived display path still passes through the sink.
    """

    id: str
    provider: ProviderName
    root: Path
    lifecycle: Lifecycle = "active"
    enabled: bool = True


@dataclass(frozen=True)
class ArtifactCandidate:
    """A file discovered below a configured provider source."""

    source: SourceSpec
    path: Path
    relative_path: str
    size: int
    mtime_ns: int
    artifact_type: Literal["session_jsonl", "tool_result"] = "session_jsonl"


@dataclass(frozen=True)
class ArtifactIdentity:
    """The provider session and lifecycle an artifact contributes to."""

    provider_session_id: str
    kind: SessionKind
    lifecycle: Lifecycle
    parent_id: str | None = None
    root_id: str | None = None


@dataclass(frozen=True)
class Diagnostic:
    """A bounded, content-free parser diagnostic safe to redact and persist."""

    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"
    line: int | None = None


@dataclass(frozen=True)
class CanonicalSession:
    id: str
    provider: ProviderName
    external_id: str
    kind: SessionKind
    lifecycle: Lifecycle
    source_id: str
    parent_id: str | None = None
    root_id: str | None = None
    originator: str | None = None
    client: str | None = None
    model: str | None = None
    title: str | None = None
    workspace_id: str | None = None
    cwd: str | None = None
    branch: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalMessage:
    id: str
    session_id: str
    sequence: int
    role: str
    provider_message_id: str | None = None
    model: str | None = None
    turn_id: str | None = None
    timestamp: str | None = None
    parent_message_id: str | None = None


@dataclass(frozen=True)
class CanonicalBlock:
    id: str
    message_id: str
    session_id: str
    sequence: int
    block_type: BlockType
    visibility: Visibility = "visible"
    text: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    mime_type: str | None = None
    is_error: bool = False
    truncated: bool = False
    original_size: int | None = None


CanonicalRecord = Union[CanonicalSession, CanonicalMessage, CanonicalBlock]
RecordT = TypeVar("RecordT", bound=CanonicalRecord)


@dataclass(frozen=True)
class RedactedRecord(Generic[RecordT]):
    """Proof-carrying wrapper accepted by the v0.2 persistence layer."""

    value: RecordT
    policy_hash: str

    def __post_init__(self) -> None:
        if not self.policy_hash:
            raise ValueError("redaction policy hash must be non-empty")


class RecordSink(Protocol):
    """Privacy boundary between provider-native parsing and persistence.

    Implementations must recursively redact all content-bearing strings before
    creating a ``RedactedRecord``.  Adapters never write to storage directly.
    """

    def emit(self, record: CanonicalRecord) -> None: ...


@dataclass(frozen=True)
class ParseOutcome:
    content_hash: str
    processed_lines: int
    processed_bytes: int
    status: ArtifactStatus
    diagnostics: tuple[Diagnostic, ...] = ()
    ignored_records: int = 0
    unknown_records: int = 0
    partial: bool = False


class ProviderAdapter(Protocol):
    @property
    def provider(self) -> ProviderName: ...

    parser_version: str

    def default_sources(self, environment: Mapping[str, str]) -> list[SourceSpec]: ...

    def iter_artifacts(self, source: SourceSpec) -> Iterable[ArtifactCandidate]: ...

    def probe(self, artifact: ArtifactCandidate) -> ArtifactIdentity: ...

    def parse(self, artifact: ArtifactCandidate, sink: RecordSink) -> ParseOutcome: ...


class CollectingSink:
    """In-memory sink for adapter contract tests and staging integrations.

    This sink is intentionally not a production privacy boundary.  Callers may
    pass a record transform (normally recursive redaction) and a non-empty
    policy hash; collected values are then exposed only as ``RedactedRecord``.
    """

    def __init__(self, transform: Any = None, *, policy_hash: str = "test-policy") -> None:
        if not policy_hash:
            raise ValueError("redaction policy hash must be non-empty")
        self._transform = transform or (lambda value: value)
        self.policy_hash = policy_hash
        self.records: list[RedactedRecord[CanonicalRecord]] = []

    def emit(self, record: CanonicalRecord) -> None:
        value = self._transform(record)
        self.records.append(RedactedRecord(value=value, policy_hash=self.policy_hash))

    def values(self, record_type: type[RecordT]) -> list[RecordT]:
        return [item.value for item in self.records if isinstance(item.value, record_type)]


def bounded_diagnostic(
    code: str,
    message: str,
    *,
    severity: Literal["info", "warning", "error"] = "warning",
    line: int | None = None,
) -> Diagnostic:
    """Create a diagnostic with a deliberately bounded, single-line message."""

    clean = " ".join(message.split())[:500]
    return Diagnostic(code=code[:80], message=clean, severity=severity, line=line)
