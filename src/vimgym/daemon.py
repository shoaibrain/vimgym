"""Daemon process manager: PID file, start/stop, foreground runner."""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

from vimgym.config import AppConfig, init_vault
from vimgym.db import init_db
from vimgym.events import publish
from vimgym.server import create_app
from vimgym.watcher import backfill, resume_watching, start_watching, stop_watching

logger = logging.getLogger(__name__)


# Log rotation: cap each file at 5 MB, keep 5 backups (~25 MB total).
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 5
_STARTUP_TIMEOUT_SECS = 10 * 60.0
_STARTUP_POLL_SECS = 0.1


def _configure_logging(config: AppConfig) -> None:
    """Configure root logger exactly once for the daemon foreground process.

    Attaches only a RotatingFileHandler. We deliberately do NOT attach a
    StreamHandler(sys.stderr): the parent's start_daemon() spawns this process
    with stdout/stderr redirected to the same log file, so a StreamHandler
    would write every record twice. Anything that escapes Python logging
    (uncaught tracebacks, third-party prints) still lands in the log via
    that fd redirect.
    """
    root = logging.getLogger()
    if getattr(root, "_vimgym_configured", False):
        return

    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    root.setLevel(level)

    # Drop any handlers a third party (or a re-import) may have attached
    # before we got here, so we own logging end-to-end.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.handlers.RotatingFileHandler(
        config.log_path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)

    # Quiet uvicorn's own loggers and force them to propagate to our root
    # handler. Without this, uvicorn installs its own StreamHandler on
    # uvicorn.error / uvicorn.access at startup, which would re-introduce
    # the double-write via the inherited stderr fd.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True
        lg.setLevel(level)

    root._vimgym_configured = True  # type: ignore[attr-defined]


# ───────────────────────── PID file ─────────────────────────


def _read_pid(pid_path: Path) -> int | None:
    try:
        text = pid_path.read_text().strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_running(config: AppConfig) -> bool:
    pid = _read_pid(config.pid_path)
    if pid is None:
        return False
    if not _pid_alive(pid):
        # Stale PID file: remove it.
        try:
            config.pid_path.unlink()
        except OSError:
            pass
        return False
    return True


def get_pid(config: AppConfig) -> int | None:
    if not is_running(config):
        return None
    return _read_pid(config.pid_path)


# ───────────────────────── Foreground runner ─────────────────────────
# This is what the spawned background process executes.


def run_foreground(config: AppConfig) -> int:
    """Run watcher + uvicorn in this process. Blocks until SIGTERM/SIGINT."""
    config.vault_dir.mkdir(parents=True, exist_ok=True)
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    _configure_logging(config)
    init_db(config.db_path)
    # Persist/detect sources only after database migration succeeds so a failed
    # v1→v2 attempt leaves both the old database and old config rollback-ready.
    config, _ = init_vault(config)

    # Reconcile once, establish the observer while its worker is paused, then
    # reconcile again to close the scan-to-watch race. Queued events resume
    # afterward and become cheap unchanged checks.
    n = backfill(config)
    observer, _handlers = start_watching(config, paused=True)
    try:
        n += backfill(config)
    except Exception:
        stop_watching(observer)
        raise
    resume_watching(observer)
    logger.info("startup backfill processed %d changed artifacts after reconciliation", n)

    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.server_host,
            port=config.server_port,
            log_level=config.log_level.lower(),
            access_log=False,
            log_config=None,  # we own logging; don't let uvicorn re-attach handlers
        )
    )

    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        logger.info("received signal %d, shutting down", signum)
        server.should_exit = True
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        # uvicorn.Server.run() handles its own loop; runs until should_exit.
        server.run()
    finally:
        try:
            stop_watching(observer)
        except Exception:
            logger.exception("observer shutdown failed")
        # Wake the websocket pump so it doesn't block forever.
        publish({"type": "shutdown"})

    return 0


# ───────────────────────── Daemonize ─────────────────────────


def start_daemon(config: AppConfig) -> int:
    """Spawn the foreground runner as a detached background process.

    Return only after this exact child serves a healthy HTTP response.
    """
    if is_running(config):
        raise RuntimeError(f"daemon already running (pid {_read_pid(config.pid_path)})")

    config.vault_dir.mkdir(parents=True, exist_ok=True)
    config.log_path.parent.mkdir(parents=True, exist_ok=True)

    log_fh = open(config.log_path, "ab")

    env = os.environ.copy()
    env["VIMGYM_PATH"] = str(config.vault_dir)
    env["VIMGYM_PORT"] = str(config.server_port)
    # The child reads the complete sources[] list from the persisted v2 config.
    # A parent-shell compatibility override must not collapse that list when the
    # detached process reloads configuration.
    env.pop("VIMGYM_WATCH_PATH", None)

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "vimgym.daemon", "--run-foreground"],
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    finally:
        log_fh.close()

    config.pid_path.write_text(str(proc.pid))

    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECS
    while True:
        returncode = proc.poll()
        if returncode is not None:
            _remove_pid_file(config)
            raise RuntimeError(
                f"daemon exited before becoming ready with code {returncode}; see {config.log_path}"
            )
        if _server_responding(config):
            return proc.pid
        if time.monotonic() >= deadline:
            _stop_failed_start(proc)
            _remove_pid_file(config)
            raise RuntimeError(
                f"daemon did not become ready within {_STARTUP_TIMEOUT_SECS:g} seconds; "
                f"see {config.log_path}"
            )
        time.sleep(_STARTUP_POLL_SECS)


def _remove_pid_file(config: AppConfig) -> None:
    try:
        config.pid_path.unlink()
    except OSError:
        pass


def _stop_failed_start(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _server_responding(config: AppConfig) -> bool:
    import httpx

    try:
        response = httpx.get(
            f"http://{config.server_host}:{config.server_port}/health",
            timeout=0.2,
        )
        body = response.json()
        expected_pid = _read_pid(config.pid_path)
        return (
            response.status_code == 200
            and body.get("status") == "ok"
            and expected_pid is not None
            and body.get("pid") == expected_pid
        )
    except (httpx.HTTPError, ValueError):
        return False


def stop_daemon(config: AppConfig) -> bool:
    """SIGTERM the daemon, wait, SIGKILL if needed. Returns True if stopped."""
    pid = _read_pid(config.pid_path)
    if pid is None:
        return False
    if not _pid_alive(pid):
        try:
            config.pid_path.unlink()
        except OSError:
            pass
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    try:
        config.pid_path.unlink()
    except OSError:
        pass
    return True


# ───────────────────────── Module entry ─────────────────────────


def main() -> int:
    if "--run-foreground" in sys.argv:
        from vimgym.config import load_config

        return run_foreground(load_config())
    print("vimgym daemon: use `vg start` instead", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
