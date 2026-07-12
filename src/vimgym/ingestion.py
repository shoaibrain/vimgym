"""Revision-aware provider ingestion with a redacting SQLite staging sink."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, cast

from vimgym.config import AppConfig, SourceConfig
from vimgym.db import get_connection
from vimgym.events import publish
from vimgym.pipeline.redact import RedactionEngine
from vimgym.providers import (
    ArtifactCandidate,
    CanonicalBlock,
    CanonicalMessage,
    CanonicalRecord,
    CanonicalSession,
    RedactedRecord,
    SourceSpec,
    get_adapter,
)
from vimgym.storage.fts import delete_fts_for_blocks, insert_block_fts

logger = logging.getLogger(__name__)
_ID_NAMESPACE = uuid.UUID("be5cb4ba-1681-50d5-bf58-b5e75cd1a87c")
_MAX_DIAGNOSTICS = 100


class IngestionError(RuntimeError):
    """An artifact failed before a validated revision could be committed."""


class StagingInvariantError(IngestionError):
    """The adapter emitted an invalid or incomplete canonical record graph."""


class IngestResult:
    def __init__(
        self,
        *,
        session_id: str | None = None,
        external_session_id: str | None = None,
        status: str,
        event_type: str | None = None,
        revision: int = 0,
        diagnostics: list[dict[str, Any]] | None = None,
        changed: bool | None = None,
    ) -> None:
        self.session_id = session_id
        self.external_session_id = external_session_id
        self.status = status
        self.event_type = event_type
        self.revision = revision
        self.diagnostics = diagnostics or []
        self._changed = changed

    @property
    def changed(self) -> bool:
        if self._changed is not None:
            return self._changed
        return self.status in {"imported", "updated", "degraded"}


def _stored_diagnostics(row: sqlite3.Row) -> list[dict[str, Any]]:
    try:
        value = json.loads(row["diagnostics_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(*parts: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, "\0".join(parts)))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_spec(source: SourceConfig) -> SourceSpec:
    if source.type not in {"claude_code", "codex"}:
        raise ValueError(f"unsupported source provider: {source.type}")
    lifecycle = cast(Any, "archived" if source.id.endswith("archived") else "active")
    return SourceSpec(
        id=source.id,
        provider=source.type,  # type: ignore[arg-type]
        root=source.expanded_path,
        lifecycle=lifecycle,
        enabled=source.enabled,
    )


def iter_source_artifacts(source: SourceConfig) -> Iterable[ArtifactCandidate]:
    spec = source_spec(source)
    yield from get_adapter(spec.provider).iter_artifacts(spec)


def candidate_for_path(source: SourceConfig, path: Path) -> ArtifactCandidate:
    spec = source_spec(source)
    resolved_root = spec.root.resolve()
    resolved_path = path.resolve()
    try:
        relative = resolved_path.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise IngestionError("artifact is outside its configured source root") from exc
    if resolved_path.is_symlink() or not resolved_path.is_file():
        raise IngestionError("artifact is not a regular provider file")
    artifact_type = "session_jsonl"
    if spec.provider == "claude_code" and "tool-results" in Path(relative).parts:
        artifact_type = "tool_result"
    stat = resolved_path.stat()
    return ArtifactCandidate(
        source=spec,
        path=resolved_path,
        relative_path=relative,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        artifact_type=artifact_type,  # type: ignore[arg-type]
    )


class RedactingStagingSink:
    """Production privacy boundary: redact, wrap, then stage each record."""

    def __init__(self, conn: sqlite3.Connection, engine: RedactionEngine) -> None:
        self.conn = conn
        self.engine = engine
        self._prepare()

    def _prepare(self) -> None:
        self.conn.executescript(
            """
            CREATE TEMP TABLE IF NOT EXISTS ingest_session(
                id TEXT PRIMARY KEY, provider TEXT, external_id TEXT, kind TEXT,
                lifecycle TEXT, source_id TEXT, parent_id TEXT, root_id TEXT,
                originator TEXT, client TEXT, model TEXT, title TEXT,
                workspace_id TEXT, cwd TEXT, branch TEXT, started_at TEXT,
                ended_at TEXT, usage_json TEXT, metadata_json TEXT
            );
            CREATE TEMP TABLE IF NOT EXISTS ingest_messages(
                id TEXT PRIMARY KEY, session_id TEXT, sequence INTEGER,
                role TEXT, provider_message_id TEXT, model TEXT, turn_id TEXT,
                timestamp TEXT, parent_message_id TEXT
            );
            CREATE TEMP TABLE IF NOT EXISTS ingest_blocks(
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                block_index INTEGER, kind TEXT, visibility TEXT, text_content TEXT,
                data_json TEXT, call_id TEXT, mime_type TEXT, is_error INTEGER,
                truncated INTEGER, original_size INTEGER
            );
            DELETE FROM ingest_session;
            DELETE FROM ingest_messages;
            DELETE FROM ingest_blocks;
            """
        )
        self.conn.commit()

    def clear(self) -> None:
        self.conn.executescript(
            "DELETE FROM ingest_session; DELETE FROM ingest_messages; DELETE FROM ingest_blocks;"
        )
        self.conn.commit()

    def emit(self, record: CanonicalRecord) -> None:
        redacted = RedactedRecord(value=self._redact(record), policy_hash=self.engine.policy_hash)
        self._stage(redacted)

    def _redact(self, record: CanonicalRecord) -> CanonicalRecord:
        if isinstance(record, CanonicalSession):

            def bounded(value: str | None, limit: int) -> str | None:
                redacted = self.engine.redact_text(value or "")
                return redacted[:limit] or None

            return replace(
                record,
                originator=bounded(record.originator, 500),
                client=bounded(record.client, 500),
                model=bounded(record.model, 500),
                title=bounded(record.title, 500),
                cwd=bounded(record.cwd, 16_384),
                branch=bounded(record.branch, 4096),
                started_at=(record.started_at or "")[:100] or None,
                ended_at=(record.ended_at or "")[:100] or None,
                metadata=self.engine.redact_value(dict(record.metadata)),
            )
        if isinstance(record, CanonicalMessage):
            return replace(
                record,
                role=self.engine.redact_text(record.role)[:100],
                provider_message_id=(record.provider_message_id or "")[:4096] or None,
                model=self.engine.redact_text(record.model or "")[:500] or None,
                turn_id=(record.turn_id or "")[:4096] or None,
                timestamp=(record.timestamp or "")[:100] or None,
                parent_message_id=(record.parent_message_id or "")[:4096] or None,
            )
        return replace(
            record,
            text=self.engine.redact_text(record.text or "") if record.text is not None else None,
            data=self.engine.redact_value(dict(record.data)),
            call_id=(record.call_id or "")[:4096] or None,
            mime_type=self.engine.redact_text(record.mime_type or "") or None,
        )

    def _stage(self, wrapped: RedactedRecord[CanonicalRecord]) -> None:
        if not isinstance(wrapped, RedactedRecord) or not wrapped.policy_hash:
            raise StagingInvariantError("storage requires a RedactedRecord")
        record = wrapped.value
        if isinstance(record, CanonicalSession):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO ingest_session VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    record.id,
                    record.provider,
                    record.external_id,
                    record.kind,
                    record.lifecycle,
                    record.source_id,
                    record.parent_id,
                    record.root_id,
                    record.originator,
                    record.client,
                    record.model,
                    record.title,
                    record.workspace_id,
                    record.cwd,
                    record.branch,
                    record.started_at,
                    record.ended_at,
                    json.dumps(dict(record.usage)),
                    json.dumps(dict(record.metadata), ensure_ascii=False),
                ),
            )
        elif isinstance(record, CanonicalMessage):
            self.conn.execute(
                "INSERT OR REPLACE INTO ingest_messages VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.session_id,
                    record.sequence,
                    record.role,
                    record.provider_message_id,
                    record.model,
                    record.turn_id,
                    record.timestamp,
                    record.parent_message_id,
                ),
            )
        elif isinstance(record, CanonicalBlock):
            self.conn.execute(
                "INSERT OR REPLACE INTO ingest_blocks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.message_id,
                    record.session_id,
                    record.sequence,
                    record.block_type,
                    record.visibility,
                    record.text,
                    json.dumps(dict(record.data), ensure_ascii=False),
                    record.call_id,
                    record.mime_type,
                    1 if record.is_error else 0,
                    1 if record.truncated else 0,
                    record.original_size or 0,
                ),
            )
        else:  # pragma: no cover - union exhaustiveness guard
            raise StagingInvariantError("unsupported canonical record")


def _safe_diagnostics(outcome: Any, engine: RedactionEngine) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for item in outcome.diagnostics[:_MAX_DIAGNOSTICS]:
        diagnostics.append(
            {
                "code": str(item.code)[:80],
                "message": engine.redact_text(str(item.message))[:500],
                "severity": item.severity,
                "line": item.line,
            }
        )
    if outcome.ignored_records:
        diagnostics.append(
            {
                "code": "ignored_records",
                "message": "Telemetry or duplicate records ignored",
                "severity": "info",
                "count": outcome.ignored_records,
            }
        )
    if outcome.unknown_records:
        diagnostics.append(
            {
                "code": "unknown_records",
                "message": "Unknown content records retained as hidden blocks",
                "severity": "warning",
                "count": outcome.unknown_records,
            }
        )
    return diagnostics[:_MAX_DIAGNOSTICS]


def _artifact_id(artifact: ArtifactCandidate, session_id: str) -> str:
    if artifact.artifact_type == "session_jsonl":
        # A Codex move between active and archived roots updates this row.
        return _id("artifact", artifact.source.provider, session_id, "session_jsonl")
    return _id("artifact", artifact.source.id, artifact.relative_path)


def _canonical_sequence(artifact: ArtifactCandidate, source_sequence: int) -> int:
    """Map per-artifact source order into one deterministic session order."""

    if artifact.artifact_type == "session_jsonl":
        return max(1, int(source_sequence))
    # Supplemental Claude sidecars have no shared provider timeline. Place
    # them after the root transcript in a stable path-derived range while
    # preserving their own source order. Nine hex digits with this stride stay
    # below JavaScript's exact-integer limit because the browser returns this
    # sequence cursor on subsequent API requests.
    artifact_order = int(hashlib.sha256(artifact.relative_path.encode()).hexdigest()[:9], 16)
    return 1_000_000_000 + artifact_order * 100_000 + max(1, int(source_sequence))


def _ensure_source(conn: sqlite3.Connection, artifact: ArtifactCandidate) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO sources(
            id,provider,name,root_path,enabled,health,created_at,updated_at,last_seen_at
        ) VALUES (?,?,?,?,1,'unknown',?,?,?)
        ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,
            root_path=excluded.root_path, enabled=1, updated_at=excluded.updated_at,
            last_seen_at=excluded.last_seen_at
        """,
        (
            artifact.source.id,
            artifact.source.provider,
            artifact.source.id.replace("_", " ").replace(":", " ").title(),
            str(artifact.source.root),
            now,
            now,
            now,
        ),
    )


def _duration(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        return max(
            0,
            int(
                (
                    datetime.fromisoformat(end.replace("Z", "+00:00"))
                    - datetime.fromisoformat(start.replace("Z", "+00:00"))
                ).total_seconds()
            ),
        )
    except (ValueError, TypeError):
        return None


def _refresh_derived(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM session_tools WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM session_files WHERE session_id=?", (session_id,))
    tool_counts: dict[str, int] = {}
    files: set[tuple[str, str]] = set()
    for block in conn.execute(
        "SELECT kind,text_content,data_json FROM message_blocks WHERE session_id=?",
        (session_id,),
    ):
        try:
            data = json.loads(block["data_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        if block["kind"] == "tool_call":
            name = data.get("name") if isinstance(data, dict) else None
            name = name or block["text_content"]
            if name:
                bounded_name = str(name)[:256]
                tool_counts[bounded_name] = tool_counts.get(bounded_name, 0) + 1

        def find_paths(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"file_path", "path", "filename"} and isinstance(child, str):
                        operation = (
                            "modified"
                            if str(block["text_content"] or "").lower()
                            in {"write", "edit", "apply_patch"}
                            else "referenced"
                        )
                        files.add((child[:4096], operation))
                    else:
                        find_paths(child)
            elif isinstance(value, list):
                for child in value:
                    find_paths(child)

        find_paths(data)

    for name, count in tool_counts.items():
        conn.execute("INSERT INTO session_tools VALUES(?,?,?)", (session_id, name, count))
    for path, operation in sorted(files):
        conn.execute("INSERT INTO session_files VALUES(?,?,?)", (session_id, path, operation))

    counts = conn.execute(
        """
        SELECT COUNT(*) messages,
               SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) users,
               SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) assistants
        FROM messages WHERE session_id=?
        """,
        (session_id,),
    ).fetchone()
    file_values = [
        row["path"]
        for row in conn.execute(
            "SELECT path FROM session_files WHERE session_id=? ORDER BY path", (session_id,)
        )
    ]
    tools = [
        row["tool_name"]
        for row in conn.execute(
            "SELECT tool_name FROM session_tools WHERE session_id=? ORDER BY tool_name",
            (session_id,),
        )
    ]
    conn.execute(
        """
        UPDATE sessions SET message_count=?, user_message_count=?,
            asst_message_count=?, tool_use_count=?, tools_used=?, files_modified=?,
            has_subagents=EXISTS(SELECT 1 FROM sessions child WHERE child.parent_session_id=sessions.id)
        WHERE id=?
        """,
        (
            int(counts["messages"] or 0),
            int(counts["users"] or 0),
            int(counts["assistants"] or 0),
            sum(tool_counts.values()),
            json.dumps(tools),
            json.dumps(file_values),
            session_id,
        ),
    )

    session = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if session["parent_session_id"]:
        conn.execute(
            "UPDATE sessions SET has_subagents=1 WHERE id=?",
            (session["parent_session_id"],),
        )
    project = session["project_name"] or "Unknown"
    aggregate = conn.execute(
        """
        SELECT COUNT(*) n, MAX(started_at) last_active,
               COALESCE(SUM(duration_secs),0) duration,
               COALESCE(SUM(input_tokens),0) inputs,
               COALESCE(SUM(output_tokens),0) outputs
        FROM sessions WHERE project_name=?
        """,
        (project,),
    ).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO projects VALUES(?,?,?,?,?,?,?,?)",
        (
            project,
            project,
            session["cwd"],
            aggregate["n"],
            aggregate["last_active"],
            aggregate["duration"],
            aggregate["inputs"],
            aggregate["outputs"],
        ),
    )


def _commit_staged(
    conn: sqlite3.Connection,
    artifact: ArtifactCandidate,
    outcome: Any,
    engine: RedactionEngine,
) -> IngestResult:
    staged_session = conn.execute("SELECT * FROM ingest_session").fetchall()
    if len(staged_session) != 1:
        raise StagingInvariantError(
            f"adapter emitted {len(staged_session)} session records; expected exactly one"
        )
    session = staged_session[0]
    try:
        uuid.UUID(str(session["id"]))
    except ValueError as exc:
        raise StagingInvariantError("adapter emitted an invalid canonical session id") from exc
    if not session["external_id"] or len(str(session["external_id"])) > 4096:
        raise StagingInvariantError("adapter emitted an invalid external session identity")
    orphan_messages = conn.execute(
        """
        SELECT COUNT(*) FROM ingest_messages m
        WHERE m.session_id!=? OR NOT EXISTS(
            SELECT 1 FROM ingest_session s WHERE s.id=m.session_id
        )
        """,
        (session["id"],),
    ).fetchone()[0]
    orphan_blocks = conn.execute(
        """
        SELECT COUNT(*) FROM ingest_blocks b
        WHERE b.session_id!=? OR NOT EXISTS(
            SELECT 1 FROM ingest_messages m WHERE m.id=b.message_id
        )
        """,
        (session["id"],),
    ).fetchone()[0]
    if orphan_messages or orphan_blocks:
        raise StagingInvariantError("adapter emitted orphaned canonical records")

    diagnostics = _safe_diagnostics(outcome, engine)
    now = _now()
    artifact_id = _artifact_id(artifact, session["id"])
    artifact_before = conn.execute(
        "SELECT * FROM source_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    previous_artifact_source = (
        str(artifact_before["source_id"]) if artifact_before is not None else None
    )
    old_source_state: tuple[str, str, int] | None = None
    existing = conn.execute("SELECT * FROM sessions WHERE id=?", (session["id"],)).fetchone()
    source_before = conn.execute(
        "SELECT health FROM sources WHERE id=?", (artifact.source.id,)
    ).fetchone()
    previous_source_health = str(source_before["health"]) if source_before else None
    previous_lifecycle = existing["lifecycle"] if existing else None
    revision = int(existing["revision"] or 0) + 1 if existing else 1
    metadata = json.loads(session["metadata_json"] or "{}")
    usage = json.loads(session["usage_json"] or "{}")
    cwd = session["cwd"]
    project = Path(cwd).name if cwd else "Unknown"
    title = session["title"]
    if not title:
        title_row = conn.execute(
            """
            SELECT b.text_content FROM ingest_blocks b
            JOIN ingest_messages m ON m.id=b.message_id
            WHERE m.role='user' AND b.visibility='visible' AND b.text_content!=''
            ORDER BY m.sequence,b.block_index LIMIT 1
            """
        ).fetchone()
        title = str(title_row[0])[:120] if title_row else None
    elif title:
        title = str(title).strip()[:500] or None
    client_version = (
        metadata.get("client_version") or metadata.get("claude_version") or metadata.get("version")
    )
    client_version = str(client_version)[:500] if client_version is not None else None
    has_problem = outcome.status == "degraded" or any(
        item.get("severity") in {"warning", "error"} for item in diagnostics
    )
    health = "degraded" if has_problem else "ok"
    artifact_status = (
        "degraded" if health == "degraded" else "updated" if artifact_before else "imported"
    )
    redacted_artifact_path = engine.redact_text(artifact.relative_path)
    if artifact.artifact_type == "session_jsonl" or existing is None:
        session_source_path = redacted_artifact_path
        session_file_hash = outcome.content_hash
        session_file_size = artifact.size
    else:
        session_source_path = str(existing["source_path"])
        session_file_hash = str(existing["file_hash"])
        session_file_size = int(existing["file_size_bytes"] or 0)

    conn.commit()  # finish TEMP staging writes before the durable transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_source(conn, artifact)
        if session["workspace_id"] and cwd:
            conn.execute(
                """
                INSERT INTO workspaces(id,provider,canonical_cwd,display_name,created_at)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    canonical_cwd=excluded.canonical_cwd,
                    display_name=excluded.display_name
                """,
                (session["workspace_id"], session["provider"], cwd, project, now),
            )

        started = session["started_at"] or (existing["started_at"] if existing else now)
        ended = session["ended_at"] or (existing["ended_at"] if existing else None)
        conn.execute(
            """
            INSERT INTO sessions(
                id,provider,external_session_id,session_uuid,source_id,kind,lifecycle,
                parent_session_id,root_session_id,originator,client_name,
                client_version,model,title,title_source,workspace_id,source_path,
                project_dir,project_name,cwd,git_branch,started_at,updated_at,ended_at,
                duration_secs,input_tokens,output_tokens,cache_read_tokens,
                cache_write_tokens,ai_title,summary,parser_version,
                redaction_policy_hash,revision,health,diagnostics_json,backed_up_at,
                last_seen_at,indexed_at,file_hash,file_size_bytes,schema_version
            ) VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,
                ?,?,?,?,?,?,?,?,?,?,2
            )
            ON CONFLICT(id) DO UPDATE SET
                source_id=excluded.source_id, kind=excluded.kind,
                lifecycle=excluded.lifecycle,
                parent_session_id=COALESCE(excluded.parent_session_id,sessions.parent_session_id),
                root_session_id=COALESCE(excluded.root_session_id,sessions.root_session_id),
                originator=COALESCE(excluded.originator,sessions.originator),
                client_name=COALESCE(excluded.client_name,sessions.client_name),
                client_version=COALESCE(excluded.client_version,sessions.client_version),
                model=COALESCE(excluded.model,sessions.model),
                title=COALESCE(excluded.title,sessions.title),
                title_source=CASE WHEN excluded.title IS NULL THEN sessions.title_source ELSE excluded.title_source END,
                workspace_id=COALESCE(excluded.workspace_id,sessions.workspace_id),
                source_path=excluded.source_path,
                project_dir=CASE WHEN excluded.project_dir='Unknown' THEN sessions.project_dir ELSE excluded.project_dir END,
                project_name=CASE WHEN excluded.project_name='Unknown' THEN sessions.project_name ELSE excluded.project_name END,
                cwd=COALESCE(excluded.cwd,sessions.cwd),
                git_branch=COALESCE(excluded.git_branch,sessions.git_branch),
                started_at=CASE WHEN sessions.started_at<excluded.started_at THEN sessions.started_at ELSE excluded.started_at END,
                updated_at=excluded.updated_at,
                ended_at=CASE WHEN sessions.ended_at IS NULL OR sessions.ended_at<excluded.ended_at THEN excluded.ended_at ELSE sessions.ended_at END,
                duration_secs=excluded.duration_secs,
                input_tokens=MAX(sessions.input_tokens,excluded.input_tokens),
                output_tokens=MAX(sessions.output_tokens,excluded.output_tokens),
                cache_read_tokens=MAX(sessions.cache_read_tokens,excluded.cache_read_tokens),
                cache_write_tokens=MAX(sessions.cache_write_tokens,excluded.cache_write_tokens),
                ai_title=COALESCE(excluded.ai_title,sessions.ai_title), summary=NULL,
                parser_version=excluded.parser_version,
                redaction_policy_hash=excluded.redaction_policy_hash,
                revision=excluded.revision,health=excluded.health,
                diagnostics_json=excluded.diagnostics_json,
                backed_up_at=excluded.backed_up_at,last_seen_at=excluded.last_seen_at,
                indexed_at=excluded.indexed_at,file_hash=excluded.file_hash,
                file_size_bytes=excluded.file_size_bytes,schema_version=2
            """,
            (
                session["id"],
                session["provider"],
                session["external_id"],
                session["external_id"] if session["provider"] == "claude_code" else None,
                artifact.source.id,
                session["kind"],
                session["lifecycle"],
                session["parent_id"],
                session["root_id"],
                session["originator"],
                session["client"],
                client_version,
                session["model"],
                title,
                "provider" if session["title"] else "fallback",
                session["workspace_id"],
                session_source_path,
                project,
                project,
                cwd,
                session["branch"],
                started,
                now,
                ended,
                _duration(started, ended),
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("cache_read_tokens") or 0),
                int(usage.get("cache_write_tokens") or 0),
                title,
                str(get_adapter(artifact.source.provider).parser_version),
                engine.policy_hash,
                revision,
                health,
                json.dumps(diagnostics, ensure_ascii=False),
                now,
                now,
                now,
                session_file_hash,
                session_file_size,
            ),
        )

        # The session JSONL artifact identity is stable across a Codex
        # active→archived move, so this upsert changes source/path in place.
        conn.execute(
            """
            DELETE FROM source_artifacts
            WHERE source_id=? AND relative_path=? AND id!=?
              AND session_id IS NULL AND status='failed'
            """,
            (artifact.source.id, redacted_artifact_path, artifact_id),
        )
        conn.execute(
            """
            INSERT INTO source_artifacts(
                id,source_id,session_id,relative_path,lifecycle,size_bytes,mtime_ns,
                content_sha256,parser_version,redaction_policy_hash,
                processed_bytes,processed_lines,status,diagnostics_json,
                last_seen_at,last_indexed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                source_id=excluded.source_id,session_id=excluded.session_id,
                relative_path=excluded.relative_path,lifecycle=excluded.lifecycle,
                size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
                content_sha256=excluded.content_sha256,
                parser_version=excluded.parser_version,
                redaction_policy_hash=excluded.redaction_policy_hash,
                processed_bytes=excluded.processed_bytes,
                processed_lines=excluded.processed_lines,status=excluded.status,
                diagnostics_json=excluded.diagnostics_json,
                last_seen_at=excluded.last_seen_at,
                last_indexed_at=excluded.last_indexed_at
            """,
            (
                artifact_id,
                artifact.source.id,
                session["id"],
                redacted_artifact_path,
                artifact.source.lifecycle,
                artifact.size,
                artifact.mtime_ns,
                outcome.content_hash,
                str(get_adapter(artifact.source.provider).parser_version),
                engine.policy_hash,
                outcome.processed_bytes,
                outcome.processed_lines,
                artifact_status,
                json.dumps(diagnostics, ensure_ascii=False),
                now,
                now,
            ),
        )

        delete_fts_for_blocks(conn, "artifact_id=?", (artifact_id,))
        conn.execute("DELETE FROM messages WHERE artifact_id=?", (artifact_id,))

        for message in conn.execute("SELECT * FROM ingest_messages ORDER BY sequence,id"):
            canonical_sequence = _canonical_sequence(artifact, int(message["sequence"]))
            blocks = conn.execute(
                "SELECT * FROM ingest_blocks WHERE message_id=? ORDER BY block_index,id",
                (message["id"],),
            ).fetchall()
            content = [
                {
                    "id": block["id"],
                    "type": block["kind"],
                    "visibility": block["visibility"],
                    "text": block["text_content"],
                    "data": json.loads(block["data_json"] or "{}"),
                    "call_id": block["call_id"],
                    "mime_type": block["mime_type"],
                    "is_error": bool(block["is_error"]),
                }
                for block in blocks
            ]
            tool_names = [
                str(item["data"].get("name") or item.get("text") or "tool")
                for item in content
                if item["type"] == "tool_call"
            ]
            conn.execute(
                """
                INSERT INTO messages(
                    id,session_id,artifact_id,session_uuid,external_message_id,
                    sequence,source_line,item_index,type,role,model,turn_id,timestamp,
                    parent_message_id,parent_uuid,visibility,has_tool_use,
                    has_thinking,has_image,tool_names,content_json,revision
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message["id"],
                    session["id"],
                    artifact_id,
                    session["external_id"] if session["provider"] == "claude_code" else None,
                    message["provider_message_id"],
                    canonical_sequence,
                    message["sequence"],
                    0,
                    "message",
                    message["role"],
                    message["model"],
                    message["turn_id"],
                    message["timestamp"],
                    message["parent_message_id"],
                    message["parent_message_id"],
                    "visible",
                    1 if tool_names else 0,
                    1 if any(item["type"] == "reasoning_summary" for item in content) else 0,
                    1 if any(item["type"] == "attachment" for item in content) else 0,
                    json.dumps(tool_names),
                    "[]",
                    revision,
                ),
            )
            for block in blocks:
                data = json.loads(block["data_json"] or "{}")
                cursor = conn.execute(
                    """
                    INSERT INTO message_blocks(
                        id,message_id,session_id,artifact_id,sequence,block_index,kind,
                        visibility,text_content,data_json,name,call_id,mime_type,
                        is_error,truncated,original_bytes,revision
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        block["id"],
                        message["id"],
                        session["id"],
                        artifact_id,
                        canonical_sequence,
                        block["block_index"],
                        block["kind"],
                        block["visibility"],
                        block["text_content"],
                        block["data_json"],
                        data.get("name") if isinstance(data, dict) else None,
                        block["call_id"],
                        block["mime_type"],
                        block["is_error"],
                        block["truncated"],
                        block["original_size"],
                        revision,
                    ),
                )
                if cursor.lastrowid is None:
                    raise IngestionError("message block insert did not return a row id")
                insert_block_fts(
                    conn,
                    int(cursor.lastrowid),
                    block["text_content"],
                    block["data_json"],
                    visible=block["visibility"] == "visible",
                )

        _refresh_derived(conn, session["id"])
        problem_artifacts = conn.execute(
            """
            SELECT status,diagnostics_json FROM source_artifacts
            WHERE session_id=? AND status IN('degraded','failed','missing')
            ORDER BY id
            """,
            (session["id"],),
        ).fetchall()
        session_health = (
            "failed"
            if any(row["status"] == "failed" for row in problem_artifacts)
            else "degraded"
            if problem_artifacts
            else "ok"
        )
        session_diagnostics: list[dict[str, Any]] = []
        for row in problem_artifacts:
            session_diagnostics.extend(_stored_diagnostics(row))
        conn.execute(
            "UPDATE sessions SET health=?,diagnostics_json=? WHERE id=?",
            (
                session_health,
                json.dumps(session_diagnostics[:_MAX_DIAGNOSTICS], ensure_ascii=False),
                session["id"],
            ),
        )
        source_problem_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM source_artifacts
                WHERE source_id=? AND status IN('degraded','failed','missing')
                """,
                (artifact.source.id,),
            ).fetchone()[0]
        )
        source_has_failed = bool(
            conn.execute(
                """
                SELECT 1 FROM source_artifacts
                WHERE source_id=? AND status='failed' LIMIT 1
                """,
                (artifact.source.id,),
            ).fetchone()
        )
        source_health = (
            "failed" if source_has_failed else "degraded" if source_problem_count else "ok"
        )
        if diagnostics:
            source_error = diagnostics[-1]["message"]
        elif source_problem_count:
            previous_error = conn.execute(
                "SELECT last_error FROM sources WHERE id=?", (artifact.source.id,)
            ).fetchone()
            source_error = previous_error[0] if previous_error else "source has degraded artifacts"
        else:
            source_error = None
        conn.execute(
            """
            UPDATE sources SET health=?,diagnostic_count=?,last_error=?,
                last_indexed_at=?,updated_at=? WHERE id=?
            """,
            (
                source_health,
                source_problem_count,
                source_error,
                now,
                now,
                artifact.source.id,
            ),
        )
        if previous_artifact_source and previous_artifact_source != artifact.source.id:
            old_problem_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM source_artifacts
                    WHERE source_id=? AND status IN('degraded','failed','missing')
                    """,
                    (previous_artifact_source,),
                ).fetchone()[0]
            )
            old_has_failed = bool(
                conn.execute(
                    """
                    SELECT 1 FROM source_artifacts
                    WHERE source_id=? AND status='failed' LIMIT 1
                    """,
                    (previous_artifact_source,),
                ).fetchone()
            )
            old_health = "failed" if old_has_failed else "degraded" if old_problem_count else "ok"
            conn.execute(
                """
                UPDATE sources SET health=?,diagnostic_count=?,
                    last_error=CASE WHEN ?=0 THEN NULL ELSE last_error END,
                    updated_at=? WHERE id=?
                """,
                (
                    old_health,
                    old_problem_count,
                    old_problem_count,
                    now,
                    previous_artifact_source,
                ),
            )
            old_source_state = (
                previous_artifact_source,
                old_health,
                old_problem_count,
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    if existing is None:
        event_type = "session_created"
    elif previous_lifecycle != "archived" and session["lifecycle"] == "archived":
        event_type = "session_archived"
    else:
        event_type = "session_updated"
    result = IngestResult(
        session_id=session["id"],
        external_session_id=session["external_id"],
        status=artifact_status,
        event_type=event_type,
        revision=revision,
        diagnostics=diagnostics,
    )
    publish(
        {
            "type": event_type,
            "session": {
                "id": result.session_id,
                "external_session_id": result.external_session_id,
                "provider": session["provider"],
                "kind": session["kind"],
                "lifecycle": session["lifecycle"],
                "revision": revision,
                "health": session_health,
            },
        }
    )
    if previous_source_health != source_health:
        publish(
            {
                "type": "source_health_changed",
                "source_id": artifact.source.id,
                "health": source_health,
                "diagnostic_count": source_problem_count,
            }
        )
    if old_source_state is not None:
        old_source_id, old_health, old_diagnostics = old_source_state
        publish(
            {
                "type": "source_health_changed",
                "source_id": old_source_id,
                "health": old_health,
                "diagnostic_count": old_diagnostics,
            }
        )
    return result


def ingest_artifact(artifact: ArtifactCandidate, config: AppConfig) -> IngestResult:
    """Parse and atomically replace one changed artifact revision."""
    conn = get_connection(config.db_path)
    engine = RedactionEngine(config.rules_path)
    adapter = get_adapter(artifact.source.provider)
    redacted_relative = engine.redact_text(artifact.relative_path)
    existing_path = conn.execute(
        "SELECT * FROM source_artifacts WHERE source_id=? AND relative_path=?",
        (artifact.source.id, redacted_relative),
    ).fetchone()
    parser_version = str(adapter.parser_version)
    if (
        existing_path is not None
        and int(existing_path["size_bytes"]) == artifact.size
        and int(existing_path["mtime_ns"]) == artifact.mtime_ns
        and str(existing_path["parser_version"]) == parser_version
        and existing_path["redaction_policy_hash"] == engine.policy_hash
        and existing_path["status"] not in {"failed", "missing", "needs_reindex"}
    ):
        previous_status = str(existing_path["status"])
        next_status = "degraded" if previous_status == "degraded" else "unchanged"
        conn.execute(
            "UPDATE source_artifacts SET status=?,last_seen_at=? WHERE id=?",
            (next_status, _now(), existing_path["id"]),
        )
        conn.commit()
        return IngestResult(
            session_id=existing_path["session_id"],
            status=next_status,
            diagnostics=_stored_diagnostics(existing_path),
            changed=False,
        )

    # A metadata-only touch must not invoke the provider parser. Hash with
    # constant memory first; parser or redaction-policy changes intentionally
    # bypass this shortcut and rebuild the canonical revision.
    if (
        existing_path is not None
        and str(existing_path["parser_version"]) == parser_version
        and existing_path["redaction_policy_hash"] == engine.policy_hash
        and existing_path["lifecycle"] == artifact.source.lifecycle
        and existing_path["status"] not in {"failed", "missing", "needs_reindex"}
        and existing_path["content_sha256"] == _sha256_file(artifact.path)
    ):
        previous_status = str(existing_path["status"])
        next_status = "degraded" if previous_status == "degraded" else "unchanged"
        conn.execute(
            """
            UPDATE source_artifacts SET size_bytes=?,mtime_ns=?,status=?,last_seen_at=?
            WHERE id=?
            """,
            (artifact.size, artifact.mtime_ns, next_status, _now(), existing_path["id"]),
        )
        conn.commit()
        return IngestResult(
            session_id=existing_path["session_id"],
            status=next_status,
            diagnostics=_stored_diagnostics(existing_path),
            changed=False,
        )

    sink = RedactingStagingSink(conn, engine)
    try:
        outcome = adapter.parse(artifact, sink)
        conn.commit()
        # Metadata-only touches do not create revisions. Parser/policy changes
        # still force the staged reindex even when the provider bytes match.
        if (
            existing_path is not None
            and existing_path["content_sha256"] == outcome.content_hash
            and str(existing_path["parser_version"]) == parser_version
            and existing_path["redaction_policy_hash"] == engine.policy_hash
            and existing_path["lifecycle"] == artifact.source.lifecycle
            and existing_path["status"] not in {"failed", "missing", "needs_reindex"}
        ):
            previous_status = str(existing_path["status"])
            next_status = "degraded" if previous_status == "degraded" else "unchanged"
            conn.execute(
                """
                UPDATE source_artifacts SET size_bytes=?,mtime_ns=?,status=?,
                    last_seen_at=? WHERE id=?
                """,
                (artifact.size, artifact.mtime_ns, next_status, _now(), existing_path["id"]),
            )
            conn.commit()
            sink.clear()
            return IngestResult(
                session_id=existing_path["session_id"],
                status=next_status,
                diagnostics=_stored_diagnostics(existing_path),
                changed=False,
            )
        result = _commit_staged(conn, artifact, outcome, engine)
        sink.clear()
        return result
    except Exception as exc:
        sink.clear()
        safe_error = engine.redact_text(str(exc))[:500]
        now = _now()
        conn.execute("BEGIN IMMEDIATE")
        _ensure_source(conn, artifact)
        failure_session_id = existing_path["session_id"] if existing_path else None
        try:
            identity = adapter.probe(artifact)
            canonical_session_id = _id(
                "session", artifact.source.provider, identity.provider_session_id
            )
            known_session = conn.execute(
                "SELECT id FROM sessions WHERE id=?", (canonical_session_id,)
            ).fetchone()
            failure_session_id = canonical_session_id if known_session else failure_session_id
            failure_id = _artifact_id(artifact, canonical_session_id)
        except Exception:
            failure_id = (
                existing_path["id"]
                if existing_path
                else _id("failed-artifact", artifact.source.id, artifact.relative_path)
            )
        conn.execute(
            """
            DELETE FROM source_artifacts
            WHERE source_id=? AND relative_path=? AND id!=?
              AND session_id IS NULL AND status='failed'
            """,
            (artifact.source.id, redacted_relative, failure_id),
        )
        conn.execute(
            """
            INSERT INTO source_artifacts(
                id,source_id,session_id,relative_path,lifecycle,size_bytes,mtime_ns,
                parser_version,redaction_policy_hash,status,diagnostics_json,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?, 'failed',?,?)
            ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,
                session_id=COALESCE(source_artifacts.session_id,excluded.session_id),
                relative_path=excluded.relative_path,lifecycle=excluded.lifecycle,
                size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
                parser_version=excluded.parser_version,
                redaction_policy_hash=excluded.redaction_policy_hash,status='failed',
                diagnostics_json=excluded.diagnostics_json,last_seen_at=excluded.last_seen_at
            """,
            (
                failure_id,
                artifact.source.id,
                failure_session_id,
                redacted_relative,
                artifact.source.lifecycle,
                artifact.size,
                artifact.mtime_ns,
                parser_version,
                engine.policy_hash,
                json.dumps([{"code": "parse_failed", "message": safe_error, "severity": "error"}]),
                now,
            ),
        )
        if failure_session_id:
            conn.execute(
                "UPDATE sessions SET health='failed',diagnostics_json=? WHERE id=?",
                (
                    json.dumps(
                        [{"code": "parse_failed", "message": safe_error, "severity": "error"}]
                    ),
                    failure_session_id,
                ),
            )
        problem_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM source_artifacts
                WHERE source_id=? AND status IN('degraded','failed','missing')
                """,
                (artifact.source.id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE sources SET health='failed',last_error=?,diagnostic_count=?,
                updated_at=? WHERE id=?
            """,
            (safe_error, problem_count, now, artifact.source.id),
        )
        conn.execute("COMMIT")
        publish(
            {"type": "source_health_changed", "source_id": artifact.source.id, "health": "failed"}
        )
        logger.error("artifact ingestion failed source=%s: %s", artifact.source.id, safe_error)
        return IngestResult(
            status="failed",
            diagnostics=[{"code": "parse_failed", "message": safe_error, "severity": "error"}],
        )


def reconcile_missing(
    conn: sqlite3.Connection,
    source: SourceConfig,
    seen_relative_paths: set[str],
    engine: RedactionEngine,
) -> int:
    """Mark disappeared artifacts missing without deleting canonical history."""
    redacted_seen = {engine.redact_text(path) for path in seen_relative_paths}
    rows = conn.execute(
        "SELECT id,session_id,relative_path FROM source_artifacts WHERE source_id=?",
        (source.id,),
    ).fetchall()
    missing = [row for row in rows if row["relative_path"] not in redacted_seen]
    missing_diagnostic = json.dumps(
        [
            {
                "code": "source_missing",
                "message": "Provider artifact is missing; last valid revision retained",
                "severity": "warning",
            }
        ]
    )
    for row in missing:
        conn.execute(
            """
            UPDATE source_artifacts SET status='missing',diagnostics_json=?,last_seen_at=?
            WHERE id=?
            """,
            (missing_diagnostic, _now(), row["id"]),
        )
        if row["session_id"]:
            failed = conn.execute(
                """
                SELECT 1 FROM source_artifacts
                WHERE session_id=? AND status='failed' LIMIT 1
                """,
                (row["session_id"],),
            ).fetchone()
            conn.execute(
                "UPDATE sessions SET health=?,diagnostics_json=? WHERE id=?",
                ("failed" if failed else "degraded", missing_diagnostic, row["session_id"]),
            )
    if missing:
        problem_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM source_artifacts
                WHERE source_id=? AND status IN('degraded','failed','missing')
                """,
                (source.id,),
            ).fetchone()[0]
        )
        source_has_failed = bool(
            conn.execute(
                "SELECT 1 FROM source_artifacts WHERE source_id=? AND status='failed' LIMIT 1",
                (source.id,),
            ).fetchone()
        )
        conn.execute(
            """
            UPDATE sources SET health=?,diagnostic_count=?,
                last_error='One or more provider artifacts are missing',updated_at=?
            WHERE id=?
            """,
            ("failed" if source_has_failed else "degraded", problem_count, _now(), source.id),
        )
        conn.commit()
        publish({"type": "source_health_changed", "source_id": source.id, "health": "degraded"})
    return len(missing)


def reindex_vault(
    config: AppConfig,
    *,
    provider: str | None = None,
    session_id: str | None = None,
) -> dict[str, int]:
    """Force current parser/policy over selected configured provider artifacts."""
    conn = get_connection(config.db_path)
    selected_artifacts: set[str] | None = None
    if session_id:
        rows = conn.execute(
            "SELECT id FROM source_artifacts WHERE session_id=?", (session_id,)
        ).fetchall()
        selected_artifacts = {str(row["id"]) for row in rows}
        for row in rows:
            conn.execute(
                """
                UPDATE source_artifacts SET status='needs_reindex'
                WHERE id=? AND status!='missing'
                """,
                (row["id"],),
            )
    elif provider:
        conn.execute(
            """
            UPDATE source_artifacts SET status='needs_reindex'
            WHERE status!='missing'
              AND source_id IN(SELECT id FROM sources WHERE provider=?)
            """,
            (provider,),
        )
    else:
        conn.execute("UPDATE source_artifacts SET status='needs_reindex' WHERE status!='missing'")
    conn.commit()

    counts = {"imported": 0, "updated": 0, "unchanged": 0, "degraded": 0, "failed": 0}
    for source in config.enabled_sources:
        if provider and source.type != provider:
            continue
        if source.type not in {"claude_code", "codex"}:
            continue
        for artifact in iter_source_artifacts(source):
            try:
                identity = get_adapter(source.type).probe(artifact)
                canonical = _id("session", source.type, identity.provider_session_id)
                artifact_id = _artifact_id(artifact, canonical)
            except Exception:
                artifact_id = ""
            if selected_artifacts is not None and artifact_id not in selected_artifacts:
                continue
            result = ingest_artifact(artifact, config)
            counts[result.status] = counts.get(result.status, 0) + 1
    return counts
