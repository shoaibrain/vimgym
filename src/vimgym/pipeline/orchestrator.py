"""Legacy Claude parser bridge for v0.1 callers and compatibility tests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from vimgym.config import AppConfig
from vimgym.db import get_connection
from vimgym.pipeline.metadata import extract_metadata
from vimgym.pipeline.parser import parse_session
from vimgym.pipeline.redact import RedactionEngine
from vimgym.storage.writer import upsert_session
from vimgym.storage.writer import PARSER_VERSION

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    session_uuid: str = ""
    project_name: str = ""
    skipped: bool = False
    skip_reason: str = ""
    error: str | None = None
    duration_secs: int | None = None
    message_count: int = 0
    event_type: str = ""
    revision: int = 0


def process_session(
    filepath: Path,
    config: AppConfig,
    source_id: str = "claude_code",
) -> ProcessResult:
    """Run the full pipeline on a single JSONL file. Never raises.

    `source_id` is the configured source that produced this file. It is
    persisted on the session row for provenance and future per-source filtering.
    """
    try:
        return _process(filepath, config, source_id)
    except Exception as e:
        # Diagnostics and logs are egress surfaces too. Never log provider
        # records or an unredacted absolute path on a parse failure.
        try:
            safe_error = _get_engine(config.rules_path).redact_text(str(e))[:2000]
        except Exception:
            safe_error = "processing failed while redaction policy was unavailable"
        logger.error("session processing failed source=%s: %s", source_id, safe_error)
        return ProcessResult(error=safe_error)


_engine_cache: dict[str, RedactionEngine] = {}


def _get_engine(rules_path: Path) -> RedactionEngine:
    key = str(rules_path)
    if key not in _engine_cache:
        _engine_cache[key] = RedactionEngine(rules_path)
    return _engine_cache[key]


def _process(filepath: Path, config: AppConfig, source_id: str) -> ProcessResult:
    conn = get_connection(config.db_path)
    engine = _get_engine(config.rules_path)

    stat = filepath.stat()
    relative_path = engine.redact_text(str(Path(filepath.parent.name) / filepath.name))
    artifact = conn.execute(
        """
        SELECT * FROM source_artifacts
        WHERE source_id=? AND relative_path=?
        """,
        (source_id, relative_path),
    ).fetchone()
    if (
        artifact is not None
        and int(artifact["size_bytes"]) == stat.st_size
        and int(artifact["mtime_ns"]) == stat.st_mtime_ns
        and int(artifact["parser_version"]) == PARSER_VERSION
        and artifact["redaction_policy_hash"] == engine.policy_hash
        and artifact["status"] not in {"failed", "needs_reindex", "missing"}
    ):
        conn.execute(
            "UPDATE source_artifacts SET status='unchanged', last_seen_at=datetime('now') WHERE id=?",
            (artifact["id"],),
        )
        conn.commit()
        return ProcessResult(
            session_uuid="",
            skipped=True,
            skip_reason="artifact state unchanged",
            event_type="unchanged",
        )

    session = parse_session(filepath)
    session.source_id = source_id

    if not session.session_uuid:
        return ProcessResult(error=f"Could not extract session UUID from {filepath.name}")

    existing = conn.execute(
        """
        SELECT id, revision, file_hash FROM sessions
        WHERE provider='claude_code' AND external_session_id=?
        """,
        (session.session_uuid,),
    ).fetchone()
    if existing is not None and existing["file_hash"] == session.file_hash:
        if artifact is not None:
            conn.execute(
                """
                UPDATE source_artifacts SET size_bytes=?, mtime_ns=?, status='unchanged',
                    last_seen_at=datetime('now') WHERE id=?
                """,
                (stat.st_size, stat.st_mtime_ns, artifact["id"]),
            )
            conn.commit()
        return ProcessResult(
            session_uuid=session.session_uuid,
            skipped=True,
            skip_reason="content hash unchanged",
            event_type="unchanged",
            revision=int(existing["revision"]),
        )

    metadata = extract_metadata(session)
    internal_id = upsert_session(conn, session, metadata, redaction_engine=engine)
    event_type = "session_updated" if existing is not None else "session_created"
    status = (
        "degraded" if session.parse_errors else ("updated" if existing is not None else "imported")
    )
    conn.execute(
        "UPDATE source_artifacts SET status=? WHERE session_id=?",
        (status, internal_id),
    )
    conn.commit()
    stored = conn.execute("SELECT revision FROM sessions WHERE id=?", (internal_id,)).fetchone()

    logger.info(
        "backed_up session=%s project=%s messages=%d",
        session.session_uuid[:8],
        metadata.project_name,
        metadata.message_count,
    )

    return ProcessResult(
        session_uuid=session.session_uuid,
        project_name=metadata.project_name,
        duration_secs=metadata.duration_secs,
        message_count=metadata.message_count,
        event_type=event_type,
        revision=int(stored["revision"]) if stored else 1,
    )
