import queue
import shutil
import time
from pathlib import Path

from vimgym.config import AppConfig, SourceConfig
from vimgym.db import get_connection, init_db
from vimgym.events import event_queue
from vimgym.watcher import (
    _is_session_file,
    backfill,
    resume_watching,
    start_watching,
    stop_watching,
)


def _cfg_with_watch(vault: Path, watch: Path, **overrides) -> AppConfig:
    """Build a v2 AppConfig with a single claude_code source pointing at `watch`."""
    return AppConfig(
        vault_dir=vault,
        sources=[
            SourceConfig(
                id="claude_code",
                name="Claude Code",
                type="claude_code",
                path=str(watch),
                enabled=True,
            )
        ],
        **overrides,
    )


REAL = Path(__file__).parent / "fixtures" / "sessions" / "-Users-example-edforge"


def test_filter_accepts_session_files():
    assert _is_session_file("/path/-Users-foo/abc.jsonl") is True


def test_filter_rejects_non_jsonl():
    assert _is_session_file("/path/abc.txt") is False
    assert _is_session_file("/path/abc.json") is False


def test_filter_accepts_subagents_but_rejects_multiplexed_journals():
    assert _is_session_file("/path/-Users-foo/abc/subagents/agent-1.jsonl") is True
    assert _is_session_file("/path/-Users-foo/abc/subagents/workflow/journal.jsonl") is False


def test_filter_rejects_dotfiles():
    assert _is_session_file("/path/.hidden.jsonl") is False


def test_backfill_processes_existing_files(tmp_path):
    watch = tmp_path / "watch"
    proj = watch / "-Users-example-edforge"
    proj.mkdir(parents=True)
    src = REAL / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
    shutil.copy(src, proj / src.name)

    cfg = _cfg_with_watch(tmp_path / "vault", watch)
    init_db(cfg.db_path)
    n = backfill(cfg)
    assert n == 1

    conn = get_connection(cfg.db_path)
    count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert count == 1

    # source_id was persisted
    src_id = conn.execute("SELECT source_id FROM sessions").fetchone()[0]
    assert src_id == "claude_code"

    n2 = backfill(cfg)
    assert n2 == 0


def test_watcher_processes_new_file(tmp_path):
    watch = tmp_path / "watch"
    proj = watch / "-Users-example-edforge"
    proj.mkdir(parents=True)

    cfg = _cfg_with_watch(
        tmp_path / "vault",
        watch,
        debounce_secs=0.3,
        stability_polls=1,
        stability_poll_interval=0.05,
    )
    init_db(cfg.db_path)

    observer, _handlers = start_watching(cfg)
    try:
        src = REAL / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
        shutil.copy(src, proj / src.name)

        deadline = time.monotonic() + 10.0
        conn = get_connection(cfg.db_path)
        while time.monotonic() < deadline:
            n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            if n >= 1:
                break
            time.sleep(0.2)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        observer.stop()
        observer.join(timeout=2)


def test_watcher_debounces_rapid_modifications(tmp_path):
    watch = tmp_path / "watch"
    proj = watch / "-Users-example-edforge"
    proj.mkdir(parents=True)

    cfg = _cfg_with_watch(
        tmp_path / "vault",
        watch,
        debounce_secs=0.4,
        stability_polls=1,
        stability_poll_interval=0.05,
    )
    init_db(cfg.db_path)

    observer, _handlers = start_watching(cfg)
    try:
        src = REAL / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
        target = proj / src.name
        shutil.copy(src, target)
        time.sleep(0.05)
        with open(target, "ab") as f:
            f.write(b"")
        time.sleep(2.0)
        conn = get_connection(cfg.db_path)
        n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert n == 1
    finally:
        observer.stop()
        observer.join(timeout=2)


def test_default_append_reaches_search_and_event_within_release_budget(tmp_path):
    watch = tmp_path / "watch"
    project = watch / "-Users-example-edforge"
    project.mkdir(parents=True)
    cfg = _cfg_with_watch(tmp_path / "vault", watch)
    init_db(cfg.db_path)
    while True:
        try:
            event_queue.get_nowait()
        except queue.Empty:
            break

    observer, _handlers = start_watching(cfg)
    started = time.monotonic()
    saw_event = False
    searchable = False
    try:
        source = REAL / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
        shutil.copy(source, project / source.name)
        conn = get_connection(cfg.db_path)
        deadline = started + 12.0
        while time.monotonic() < deadline:
            try:
                event = event_queue.get_nowait()
                saw_event = saw_event or event.get("type") == "session_created"
            except queue.Empty:
                pass
            searchable = conn.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0] > 0
            if saw_event and searchable:
                break
            time.sleep(0.05)
        assert saw_event
        assert searchable
        assert time.monotonic() - started <= 12.0
    finally:
        stop_watching(observer)


def test_paused_startup_watcher_queues_until_reconciliation_finishes(tmp_path):
    watch = tmp_path / "watch"
    project = watch / "-Users-example-edforge"
    project.mkdir(parents=True)
    cfg = _cfg_with_watch(
        tmp_path / "vault",
        watch,
        debounce_secs=0.1,
        stability_polls=1,
        stability_poll_interval=0.01,
    )
    init_db(cfg.db_path)
    observer, _handlers = start_watching(cfg, paused=True)
    try:
        source = REAL / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"
        shutil.copy(source, project / source.name)
        time.sleep(0.5)
        conn = get_connection(cfg.db_path)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

        resume_watching(observer)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1:
                break
            time.sleep(0.05)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        stop_watching(observer)
