"""Thread-safe fan-out of session events to Server-Sent-Event subscribers.

Pipeline threads call ``publish``; each SSE connection owns an asyncio.Queue
on the server's event loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

_subscribers: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
_lock = threading.Lock()


def subscribe(session_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    entry = (asyncio.get_running_loop(), queue)
    with _lock:
        _subscribers.setdefault(session_id, set()).add(entry)
    return queue


def unsubscribe(session_id: str, queue: asyncio.Queue) -> None:
    with _lock:
        entries = _subscribers.get(session_id, set())
        for entry in list(entries):
            if entry[1] is queue:
                entries.discard(entry)
        if not entries:
            _subscribers.pop(session_id, None)


def _offer(queue: asyncio.Queue, item: tuple[str, Any]) -> None:
    if queue.full():
        # A stalled client should not block the pipeline; it will resync on reconnect.
        return
    queue.put_nowait(item)


def publish(session_id: str, event: str, data: Any) -> None:
    with _lock:
        entries = list(_subscribers.get(session_id, ()))
    for loop, queue in entries:
        try:
            loop.call_soon_threadsafe(_offer, queue, (event, data))
        except RuntimeError:
            # Event loop already closed (server shutting down).
            continue
