#!/usr/bin/env python3
"""Measure and optionally enforce the v0.2 release-scale local budgets."""

from __future__ import annotations

import argparse
import json
import platform
import resource
import shutil
import statistics
import time
from pathlib import Path

from fastapi.testclient import TestClient

from vimgym.backup import create_backup, restore_backup
from vimgym.config import AppConfig, SourceConfig, save_config
from vimgym.db import close_all_connections, get_connection, init_db
from vimgym.server import create_app
from vimgym.storage.queries import search_sessions
from vimgym.watcher import backfill


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports KiB.
    return value / (1024 * 1024) if platform.system() == "Darwin" else value / 1024


def benchmark(corpus: Path, work: Path) -> dict[str, float | int | str]:
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    vault = work / "vault"
    sources = [
        SourceConfig(
            "claude_code",
            "Claude benchmark",
            "claude_code",
            str(corpus / "claude" / "projects"),
        ),
        SourceConfig(
            "codex_active",
            "Codex benchmark",
            "codex",
            str(corpus / "codex" / "sessions"),
        ),
    ]
    cfg = AppConfig(vault_dir=vault, sources=sources)
    init_db(cfg.db_path)
    save_config(cfg)

    started = time.perf_counter()
    changed = backfill(cfg)
    ingest_seconds = time.perf_counter() - started
    conn = get_connection(cfg.db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    session_count = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    message_count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    db_mib = cfg.db_path.stat().st_size / (1024 * 1024)

    # Populate SQLite/OS page caches before measuring the explicitly warm gate.
    for query in ("benchmark", "migration", "backup", "restore", "capture"):
        search_sessions(conn, query, limit=20)
    representative_session = str(
        conn.execute("SELECT id FROM sessions ORDER BY id LIMIT 1").fetchone()[0]
    )
    search_ms: list[float] = []
    page_ms: list[float] = []
    message_page_ms: list[float] = []
    with TestClient(create_app(cfg), base_url=f"http://127.0.0.1:{cfg.server_port}") as client:
        # Warm the same public endpoints whose release budgets are enforced.
        assert client.get("/api/search", params={"q": "benchmark"}).status_code == 200
        assert client.get("/api/sessions", params={"limit": 50}).status_code == 200
        assert (
            client.get(
                f"/api/sessions/{representative_session}/messages", params={"limit": 100}
            ).status_code
            == 200
        )
        for index in range(100):
            query = ("benchmark", "migration", "backup", "restore", "capture")[index % 5]
            tick = time.perf_counter()
            response = client.get("/api/search", params={"q": query, "limit": 20})
            search_ms.append((time.perf_counter() - tick) * 1000)
            if response.status_code != 200:
                raise RuntimeError(f"benchmark search API failed: {response.status_code}")
        for _ in range(50):
            tick = time.perf_counter()
            response = client.get("/api/sessions", params={"limit": 50})
            page_ms.append((time.perf_counter() - tick) * 1000)
            if response.status_code != 200:
                raise RuntimeError(f"benchmark session API failed: {response.status_code}")
            tick = time.perf_counter()
            response = client.get(
                f"/api/sessions/{representative_session}/messages", params={"limit": 100}
            )
            message_page_ms.append((time.perf_counter() - tick) * 1000)
            if response.status_code != 200:
                raise RuntimeError(f"benchmark message API failed: {response.status_code}")

    backup_started = time.perf_counter()
    backup = create_backup(vault, work / "backup.vgbak")
    backup_seconds = time.perf_counter() - backup_started
    close_all_connections()
    restore_started = time.perf_counter()
    restored = restore_backup(backup.path, work / "restored")
    restore_seconds = time.perf_counter() - restore_started
    restored_conn = get_connection(restored.vault_dir / "vault.db")
    restored_sessions = int(restored_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    close_all_connections()

    return {
        "platform": platform.platform(),
        "changed_artifacts": changed,
        "sessions": session_count,
        "messages": message_count,
        "restored_sessions": restored_sessions,
        "cold_ingest_seconds": round(ingest_seconds, 3),
        "peak_rss_mib": round(_rss_mib(), 2),
        "checkpointed_db_mib": round(db_mib, 2),
        "warm_search_p95_ms": round(_percentile(search_ms, 0.95), 2),
        "warm_search_p99_ms": round(_percentile(search_ms, 0.99), 2),
        "session_page_p95_ms": round(_percentile(page_ms, 0.95), 2),
        "message_page_p95_ms": round(_percentile(message_page_ms, 0.95), 2),
        "backup_seconds": round(backup_seconds, 3),
        "verify_restore_seconds": round(restore_seconds, 3),
        "search_median_ms": round(statistics.median(search_ms), 2),
    }


def enforce(metrics: dict[str, float | int | str]) -> None:
    limits = {
        "cold_ingest_seconds": 75.0,
        "peak_rss_mib": 250.0,
        "checkpointed_db_mib": 250.0,
        "warm_search_p95_ms": 250.0,
        "warm_search_p99_ms": 500.0,
        "session_page_p95_ms": 200.0,
        "message_page_p95_ms": 200.0,
        "backup_seconds": 30.0,
        "verify_restore_seconds": 60.0,
    }
    failures = [
        f"{name}={metrics[name]} exceeds {limit}"
        for name, limit in limits.items()
        if float(metrics[name]) > limit
    ]
    if metrics["sessions"] != metrics["restored_sessions"]:
        failures.append("restored session count differs")
    if failures:
        raise SystemExit("benchmark gate failed: " + "; ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()
    metrics = benchmark(args.corpus, args.work)
    rendered = json.dumps(metrics, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    if args.enforce:
        enforce(metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
