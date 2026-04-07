"""Pipeline orchestrator: parse → dedup → redact → metadata → write."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from vimgym.config import AppConfig
from vimgym.db import get_connection
from vimgym.pipeline.metadata import extract_metadata
from vimgym.pipeline.parser import parse_session
from vimgym.pipeline.redact import RedactionEngine
from vimgym.pipeline.summary import heuristic_summary
from vimgym.storage.writer import (
    session_exists_by_hash,
    session_exists_by_uuid,
    upsert_session,
)

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
        logger.exception("Unhandled error processing %s: %s", filepath, e)
        return ProcessResult(error=str(e))


_engine_cache: dict[str, RedactionEngine] = {}


def _get_engine(rules_path: Path) -> RedactionEngine:
    key = str(rules_path)
    if key not in _engine_cache:
        _engine_cache[key] = RedactionEngine(rules_path)
    return _engine_cache[key]


def _process(filepath: Path, config: AppConfig, source_id: str) -> ProcessResult:
    conn = get_connection(config.db_path)
    engine = _get_engine(config.rules_path)

    session = parse_session(filepath)
    session.source_id = source_id

    if not session.session_uuid:
        return ProcessResult(error=f"Could not extract session UUID from {filepath.name}")

    if session_exists_by_hash(conn, session.file_hash):
        return ProcessResult(
            session_uuid=session.session_uuid,
            skipped=True,
            skip_reason="file_hash already indexed",
        )
    if session_exists_by_uuid(conn, session.session_uuid):
        return ProcessResult(
            session_uuid=session.session_uuid,
            skipped=True,
            skip_reason="session_uuid already indexed",
        )

    session.raw_jsonl = engine.redact_session_raw(session.raw_jsonl)
    session.user_messages_text = engine.redact_text(session.user_messages_text)
    session.asst_messages_text = engine.redact_text(session.asst_messages_text)

    metadata = extract_metadata(session)
    summary = heuristic_summary(session)

    upsert_session(conn, session, metadata, summary)

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
    )
