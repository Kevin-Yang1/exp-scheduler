from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def publish(self, event_type: str, payload: dict[str, object] | None = None) -> None:
        message = json.dumps(
            {
                "type": event_type,
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": payload or {},
            },
            ensure_ascii=False,
        )
        async with self._lock:
            queues = list(self._subscribers)
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                continue
