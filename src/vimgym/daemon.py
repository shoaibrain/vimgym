"""Daemon process manager: PID file, start/stop, foreground runner."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

from vimgym.config import AppConfig, save_config
from vimgym.db import init_db
from vimgym.events import publish
from vimgym.server import create_app
from vimgym.watcher import backfill, start_watching

logger = logging.getLogger(__name__)


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
    init_db(config.db_path)
    save_config(config)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(config.log_path),
            logging.StreamHandler(sys.stderr),
        ],
    )

    # Backfill before starting the watcher so we don't double-process.
    n = backfill(config)
    logger.info("backfill processed %d new files", n)

    observer, _handlers = start_watching(config)

    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.server_host,
            port=config.server_port,
            log_level=config.log_level.lower(),
            access_log=False,
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
            observer.stop()
            observer.join(timeout=3)
        except Exception:
            logger.exception("observer shutdown failed")
        # Wake the websocket pump so it doesn't block forever.
        publish({"type": "shutdown"})

    return 0


# ───────────────────────── Daemonize ─────────────────────────


def start_daemon(config: AppConfig) -> int:
    """Spawn the foreground runner as a detached background process.

    Returns the child PID. Raises RuntimeError if already running.
    """
    if is_running(config):
        raise RuntimeError(f"daemon already running (pid {_read_pid(config.pid_path)})")

    config.vault_dir.mkdir(parents=True, exist_ok=True)
    config.log_path.parent.mkdir(parents=True, exist_ok=True)

    log_fh = open(config.log_path, "ab")

    env = os.environ.copy()
    env["VIMGYM_PATH"] = str(config.vault_dir)
    env["VIMGYM_PORT"] = str(config.server_port)
    # NOTE: do NOT forward VIMGYM_WATCH_PATH to the child. Since schema v2,
    # the child reads its on-disk config from VIMGYM_PATH, which already
    # contains the full sources[] list. Forwarding the env var would
    # collapse multi-source configs to a single 'env_override' entry.
    # If the user explicitly set it in their parent shell (dev workflow),
    # os.environ.copy() above already preserved it.

    proc = subprocess.Popen(
        [sys.executable, "-m", "vimgym.daemon", "--run-foreground"],
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )

    config.pid_path.write_text(str(proc.pid))

    # Wait briefly to confirm it actually started (didn't immediately exit).
    for _ in range(20):
        time.sleep(0.1)
        if proc.poll() is not None:
            try:
                config.pid_path.unlink()
            except OSError:
                pass
            raise RuntimeError(
                f"daemon exited immediately with code {proc.returncode}; "
                f"see {config.log_path}"
            )
        if _server_responding(config):
            break

    return proc.pid


def _server_responding(config: AppConfig) -> bool:
    import socket
    try:
        with socket.create_connection((config.server_host, config.server_port), timeout=0.2):
            return True
    except OSError:
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
