from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class EventBus:
    """In-process async event bus backed by asyncio.Queue per subscriber."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._latest: dict[str, Any] = {}

    async def publish(self, channel: str, data: Any):
        self._latest[channel] = data
        for queue in self._subscribers[channel]:
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(data)

    def subscribe(self, channel: str, maxsize: int = 1000) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers[channel].append(queue)
        return queue

    def get_latest(self, channel: str) -> Any:
        return self._latest.get(channel)
