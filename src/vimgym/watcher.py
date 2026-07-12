"""Bounded coalescing filesystem capture for Claude and Codex artifacts."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver

from vimgym.config import AppConfig, SourceConfig
from vimgym.db import get_connection
from vimgym.events import publish
from vimgym.ingestion import (
    candidate_for_path,
    ingest_artifact,
    iter_source_artifacts,
    reconcile_missing,
)
from vimgym.pipeline.redact import RedactionEngine

logger = logging.getLogger(__name__)
_QUEUE_CAPACITY = 2048
_TEXT_SIDECAR_SUFFIXES = {"", ".txt", ".log", ".md", ".json", ".xml", ".csv"}


def _is_session_file(path: str) -> bool:
    """Return whether a path is provider session JSONL.

    Claude subagents are first-class in v0.2. Multiplexed workflow journals
    are transport duplicates and are the sole JSONL exclusion.
    """
    candidate = Path(path)
    return (
        candidate.suffix.lower() == ".jsonl"
        and not candidate.name.startswith(".")
        and candidate.name != "journal.jsonl"
    )


def _is_artifact_file(path: str, source: SourceConfig) -> bool:
    if _is_session_file(path):
        return True
    candidate = Path(path)
    return (
        source.type == "claude_code"
        and "tool-results" in candidate.parts
        and candidate.suffix.lower() in _TEXT_SIDECAR_SUFFIXES
        and not candidate.name.startswith(".")
    )


class IngestionCoordinator:
    """One worker and one bounded coalescing queue for all watched sources."""

    def __init__(
        self,
        config: AppConfig,
        capacity: int = _QUEUE_CAPACITY,
        *,
        paused: bool = False,
    ) -> None:
        self.config = config
        self.capacity = max(1, int(capacity))
        self._pending: "OrderedDict[tuple[str, str], tuple[float, SourceConfig]]" = OrderedDict()
        self._rescan_sources: "OrderedDict[str, SourceConfig]" = OrderedDict()
        self._condition = threading.Condition()
        self._stopping = False
        self._paused = paused
        self._worker = threading.Thread(target=self._loop, name="vimgym-ingestion", daemon=True)
        self._worker.start()

    def submit(self, path: str, source: SourceConfig) -> None:
        key = (source.id, path)
        due = time.monotonic() + max(0.0, self.config.debounce_secs)
        with self._condition:
            if key in self._pending:
                self._pending.pop(key)
            elif len(self._pending) >= self.capacity:
                dropped, (_, dropped_source) = self._pending.popitem(last=False)
                # Preserve bounded memory without losing eventual capture. A
                # single coalesced source rescan repairs every event displaced
                # during a burst; normal per-path work remains one-worker only.
                self._rescan_sources[dropped_source.id] = dropped_source
                self._rescan_sources[source.id] = source
                logger.warning(
                    "ingestion queue full; scheduled source reconciliation source=%s",
                    dropped[0],
                )
            self._pending[key] = (due, source)
            self._condition.notify()

    def stop(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._worker.join(timeout=timeout)

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    def _loop(self) -> None:
        while True:
            run_rescan: SourceConfig | None = None
            key: tuple[str, str] | None = None
            source: SourceConfig | None = None
            with self._condition:
                if self._stopping:
                    return
                if self._paused:
                    self._condition.wait(timeout=1.0)
                    continue
                if self._pending:
                    key, (due, source) = min(self._pending.items(), key=lambda item: item[1][0])
                    remaining = due - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(timeout=min(remaining, 1.0))
                        continue
                    self._pending.pop(key, None)
                elif self._rescan_sources:
                    _, run_rescan = self._rescan_sources.popitem(last=False)
                    # Run outside the condition so producers remain fast.
                else:
                    self._condition.wait(timeout=1.0)
                    continue
            if run_rescan is not None:
                self._reconcile_source(run_rescan)
                continue
            if key is None or source is None:  # pragma: no cover - guarded above
                continue
            _, path = key
            self._process(path, source)

    def _reconcile_source(self, source: SourceConfig) -> None:
        try:
            conn = get_connection(self.config.db_path)
            engine = RedactionEngine(self.config.rules_path)
            seen: set[str] = set()
            for artifact in iter_source_artifacts(source):
                seen.add(artifact.relative_path)
                ingest_artifact(artifact, self.config)
            reconcile_missing(conn, source, seen, engine)
        except Exception as exc:
            try:
                safe = RedactionEngine(self.config.rules_path).redact_text(str(exc))[:500]
            except Exception:
                safe = "source reconciliation failed"
            logger.error("source reconciliation failed source=%s: %s", source.id, safe)

    def _process(self, raw_path: str, source: SourceConfig) -> None:
        path = Path(raw_path)
        if not path.exists():
            self._mark_missing(path, source)
            return
        if not self._wait_for_stability(path):
            if path.exists():
                self.submit(str(path), source)
            else:
                self._mark_missing(path, source)
            return
        try:
            artifact = candidate_for_path(source, path)
            result = ingest_artifact(artifact, self.config)
        except Exception as exc:
            try:
                safe = RedactionEngine(self.config.rules_path).redact_text(str(exc))[:500]
            except Exception:
                safe = "artifact processing failed"
            logger.error("watcher processing failed source=%s: %s", source.id, safe)
            return
        logger.info(
            "capture source=%s status=%s session=%s revision=%d",
            source.id,
            result.status,
            (result.session_id or "")[:8],
            result.revision,
        )

    def _mark_missing(self, path: Path, source: SourceConfig) -> None:
        try:
            relative = path.resolve().relative_to(source.expanded_path.resolve()).as_posix()
            engine = RedactionEngine(self.config.rules_path)
            conn = get_connection(self.config.db_path)
            row = conn.execute(
                """
                SELECT id,session_id FROM source_artifacts
                WHERE source_id=? AND relative_path=?
                """,
                (source.id, engine.redact_text(relative)),
            ).fetchone()
            if row:
                diagnostic = (
                    '[{"code":"source_missing","message":"Provider artifact is missing; '
                    'last valid revision retained","severity":"warning"}]'
                )
                conn.execute(
                    """
                    UPDATE source_artifacts SET status='missing',diagnostics_json=?,last_seen_at=?
                    WHERE id=?
                    """,
                    (
                        diagnostic,
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        row["id"],
                    ),
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
                        ("failed" if failed else "degraded", diagnostic, row["session_id"]),
                    )
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
                        """
                        SELECT 1 FROM source_artifacts
                        WHERE source_id=? AND status='failed' LIMIT 1
                        """,
                        (source.id,),
                    ).fetchone()
                )
                source_health = "failed" if source_has_failed else "degraded"
                conn.execute(
                    """
                    UPDATE sources SET health=?,diagnostic_count=?,
                        last_error='One or more provider artifacts are missing',updated_at=?
                    WHERE id=?
                    """,
                    (
                        source_health,
                        problem_count,
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        source.id,
                    ),
                )
                conn.commit()
                publish(
                    {
                        "type": "source_health_changed",
                        "source_id": source.id,
                        "health": source_health,
                        "diagnostic_count": problem_count,
                    }
                )
        except Exception:
            logger.warning("could not mark missing artifact source=%s", source.id)

    def _wait_for_stability(self, path: Path) -> bool:
        previous: tuple[int, int] | None = None
        agrees = 0
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                stat = path.stat()
            except OSError:
                return False
            state = (stat.st_size, stat.st_mtime_ns)
            if state == previous:
                agrees += 1
                if agrees >= max(1, self.config.stability_polls - 1):
                    return True
            else:
                agrees = 0
                previous = state
            time.sleep(self.config.stability_poll_interval)
        logger.warning("artifact did not stabilize source path was deferred")
        return False


class SessionWatcher(FileSystemEventHandler):
    """Thin event adapter bound to a source and shared ingestion worker."""

    def __init__(
        self,
        config: AppConfig,
        source_id: str = "claude_code",
        *,
        coordinator: IngestionCoordinator | None = None,
    ) -> None:
        self._config = config
        self._source = next(
            (item for item in config.sources if item.id == source_id),
            SourceConfig(source_id, source_id, "claude_code", str(config.watch_path)),
        )
        self._coordinator = coordinator or IngestionCoordinator(config)
        self._owns_coordinator = coordinator is None

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event.src_path, event.is_directory)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event.src_path, event.is_directory)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event.src_path, event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._maybe_schedule(event.src_path, False)
        self._maybe_schedule(getattr(event, "dest_path", ""), False)

    def _maybe_schedule(self, raw_path: str | bytes, is_directory: bool) -> None:
        if is_directory:
            return
        path = raw_path.decode("utf-8", "replace") if isinstance(raw_path, bytes) else raw_path
        if path and _is_artifact_file(path, self._source):
            self._coordinator.submit(path, self._source)

    # Legacy test hooks now submit to the single coordinator rather than
    # creating per-path timer threads.
    def _schedule(self, path: str) -> None:
        self._coordinator.submit(path, self._source)

    def _run(self, path: str) -> None:
        self._coordinator._process(path, self._source)


class _ManagedObserver(PollingObserver):
    """Portable observer that also owns the shared ingestion worker."""

    _vimgym_coordinator: IngestionCoordinator | None = None

    def stop(self) -> None:
        super().stop()
        if self._vimgym_coordinator is not None:
            self._vimgym_coordinator.stop()
            self._vimgym_coordinator = None


def backfill(config: AppConfig) -> int:
    """Reconcile every supported artifact on startup; return changed count."""
    conn = get_connection(config.db_path)
    engine = RedactionEngine(config.rules_path)
    changed = 0
    for source in config.enabled_sources:
        if source.type not in {"claude_code", "codex"}:
            continue
        seen: set[str] = set()
        for artifact in iter_source_artifacts(source):
            seen.add(artifact.relative_path)
            result = ingest_artifact(artifact, config)
            if result.changed:
                changed += 1
        reconcile_missing(conn, source, seen, engine)
    return changed


def start_watching(
    config: AppConfig, *, paused: bool = False
) -> tuple[BaseObserver, list[SessionWatcher]]:
    # Polling avoids platform-specific FSEvents teardown crashes and provides
    # identical behavior on the supported macOS/Linux matrix.
    observer = _ManagedObserver(timeout=0.2)
    coordinator = IngestionCoordinator(config, paused=paused)
    handlers: list[SessionWatcher] = []
    for source in config.enabled_sources:
        if source.type not in {"claude_code", "codex"}:
            continue
        path = source.expanded_path
        if not path.is_dir():
            continue
        handler = SessionWatcher(config, source.id, coordinator=coordinator)
        observer.schedule(handler, str(path), recursive=True)
        handlers.append(handler)
        logger.info("watcher scheduled source=%s", source.id)
    observer._vimgym_coordinator = coordinator
    observer.start()
    return observer, handlers


def stop_watching(observer: BaseObserver) -> None:
    observer.stop()
    observer.join(timeout=3)
    coordinator = getattr(observer, "_vimgym_coordinator", None)
    if coordinator is not None:
        coordinator.stop()


def resume_watching(observer: BaseObserver) -> None:
    coordinator = getattr(observer, "_vimgym_coordinator", None)
    if coordinator is not None:
        coordinator.resume()
