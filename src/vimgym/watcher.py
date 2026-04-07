"""Filesystem watcher: detects new/modified Claude Code session files."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from vimgym.config import AppConfig
from vimgym.db import get_connection
from vimgym.events import publish
from vimgym.pipeline.orchestrator import process_session

logger = logging.getLogger(__name__)


# Companion-directory segments that hold subagent and tool-result files.
# These are NOT root session files and must never be processed as sessions.
_COMPANION_SEGMENTS = ("/subagents/", "/tool-results/")


def _is_session_file(path: str) -> bool:
    """Filter rule: only root-level *.jsonl files in a project directory."""
    if not path.endswith(".jsonl"):
        return False
    if os.path.basename(path).startswith("."):
        return False
    for seg in _COMPANION_SEGMENTS:
        if seg in path:
            return False
    return True


class SessionWatcher(FileSystemEventHandler):
    """Debounced session-file watcher bound to a single configured source.

    Each path gets its own threading.Timer; new events for the same path
    cancel and reschedule. Once the timer fires, file size is polled until
    stable, then process_session() runs with this watcher's source_id.
    """

    def __init__(self, config: AppConfig, source_id: str = "claude_code"):
        self._config = config
        self._source_id = source_id
        self._debounce: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    # ------ event hooks --------------------------------------------------

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Treat the destination as a new file.
        dest = getattr(event, "dest_path", None)
        if dest and not event.is_directory:
            dest_str = dest.decode("utf-8", "replace") if isinstance(dest, bytes) else dest
            if _is_session_file(dest_str):
                self._schedule(dest_str)

    # ------ internals ----------------------------------------------------

    def _maybe_schedule(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # watchdog typing returns str | bytes; normalize to str.
        src = event.src_path
        path = src.decode("utf-8", "replace") if isinstance(src, bytes) else src
        if not _is_session_file(path):
            return
        self._schedule(path)

    def _schedule(self, path: str) -> None:
        with self._lock:
            existing = self._debounce.get(path)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(
                self._config.debounce_secs,
                self._process_when_stable,
                args=[path],
            )
            timer.daemon = True
            self._debounce[path] = timer
            timer.start()

    def _process_when_stable(self, path: str) -> None:
        try:
            self._wait_for_stability(path)
            self._run(path)
        except Exception:
            logger.exception("watcher error processing %s", path)
        finally:
            with self._lock:
                self._debounce.pop(path, None)

    def _wait_for_stability(self, path: str) -> None:
        """Poll file size until two consecutive reads agree, capped at 15s."""
        prev = -1
        agree = 0
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                size = os.path.getsize(path)
            except OSError:
                return  # file vanished — let process_session handle it
            if size == prev:
                agree += 1
                if agree >= max(1, self._config.stability_polls - 1):
                    return
            else:
                agree = 0
                prev = size
            time.sleep(self._config.stability_poll_interval)
        logger.warning("file did not stabilize within 15s: %s", path)

    def _run(self, path: str) -> None:
        result = process_session(Path(path), self._config, source_id=self._source_id)
        if result.error:
            logger.error("process_session failed: %s — %s", path, result.error)
            return
        if result.skipped:
            logger.debug("skipped (already indexed): %s", path)
            return
        logger.info(
            "backed_up source=%s session=%s project=%s messages=%d",
            self._source_id,
            (result.session_uuid or "")[:8],
            result.project_name,
            result.message_count,
        )
        publish(
            {
                "type": "session_added",
                "session": {
                    "session_uuid": result.session_uuid,
                    "project_name": result.project_name,
                    "duration_secs": result.duration_secs,
                    "message_count": result.message_count,
                    "source_id": self._source_id,
                },
            }
        )


def backfill(config: AppConfig) -> int:
    """Process every *.jsonl under each enabled source that isn't yet in the DB.

    Returns the number of files newly processed (excluding skips and errors).
    Sources whose `type` has no parser yet are skipped silently.
    """
    sources = config.enabled_sources
    if not sources:
        return 0

    # Make sure DB connection exists in this thread (raises early if vault is bad).
    get_connection(config.db_path)

    processed = 0
    for source in sources:
        if source.type != "claude_code":
            logger.debug("backfill: skipping source %s (no parser)", source.id)
            continue
        root = source.expanded_path
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            if not _is_session_file(str(path)):
                continue
            result = process_session(path, config, source_id=source.id)
            if result.error is None and not result.skipped:
                processed += 1
    return processed


def start_watching(config: AppConfig) -> tuple[BaseObserver, list[SessionWatcher]]:
    """Start one filesystem observer with one handler per enabled source.

    Returns (observer, [handlers]). Caller stops the observer when done.
    Sources whose parser is unavailable (`type != claude_code`) are NOT watched
    even if `enabled=True`, so users can leave them on without breaking anything.
    """
    observer = Observer()
    handlers: list[SessionWatcher] = []

    for source in config.enabled_sources:
        if source.type != "claude_code":
            logger.info("watcher: source %s detected but parser unavailable", source.id)
            continue
        path = source.expanded_path
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("watcher: cannot create source path %s", path)
            continue
        handler = SessionWatcher(config, source_id=source.id)
        observer.schedule(handler, str(path), recursive=True)
        handlers.append(handler)
        logger.info("watcher: scheduled source=%s path=%s", source.id, path)

    if not handlers:
        logger.warning("watcher: no enabled sources with available parsers")

    observer.start()
    return observer, handlers
