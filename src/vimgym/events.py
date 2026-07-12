"""Cross-thread event bus: watcher thread → server async websocket broadcast.

The v0.2 daemon has one ingestion writer and one bounded in-process event queue;
the server drains it from a background asyncio task. Durable state remains the
source of truth when transient clients reconnect or the queue coalesces a burst.
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
