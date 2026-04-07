"""Cross-thread event bus: watcher thread → server async websocket broadcast.

A single in-process queue.Queue is fine for v1 (one daemon process,
one writer thread, many readers). Server polls in a background asyncio task.
"""
from __future__ import annotations

import queue
from typing import Any

# Bounded so a runaway watcher cannot grow memory unbounded.
event_queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=1024)


def publish(event: dict[str, Any]) -> None:
    try:
        event_queue.put_nowait(event)
    except queue.Full:
        pass
