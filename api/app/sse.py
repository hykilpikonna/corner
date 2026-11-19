from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any


def _extract_post_id(data: dict[str, Any]) -> int | None:
    raw = data.get("post_id")
    if raw is None and "id" in data and isinstance(data["id"], int):
        raw = data["id"]
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    return None


@dataclass(frozen=True, slots=True)
class SSEEvent:
    id: int
    event: str
    data: dict[str, Any]
    post_id: int | None = None

    def to_sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.event}\ndata: {payload}\n\n"


@dataclass(slots=True)
class _Subscriber:
    sub_id: int
    queue: asyncio.Queue[SSEEvent]
    post_ids: frozenset[int] | None
    user_id: int | None


class SSEBus:
    """In-memory pub/sub event bus with monotonic IDs, replay ring buffer, and viewer tracking."""

    def __init__(self, *, max_history: int = 500, subscriber_queue_size: int = 200) -> None:
        self._max_history = max_history
        self._subscriber_queue_size = subscriber_queue_size
        self._history: deque[SSEEvent] = deque(maxlen=max_history)
        self._next_id: int = 1
        self._next_sub_id: int = 1
        self._subscribers: dict[int, _Subscriber] = {}

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def last_event_id(self) -> int:
        return self._next_id - 1

    def publish(
        self,
        event: str,
        data: dict[str, Any],
        *,
        post_id: int | None = None,
    ) -> SSEEvent:
        effective_post_id = post_id if post_id is not None else _extract_post_id(data)
        sse_event = SSEEvent(
            id=self._next_id,
            event=event,
            data=data,
            post_id=effective_post_id,
        )
        self._next_id += 1
        self._history.append(sse_event)

        for sub in list(self._subscribers.values()):
            if (
                event != "post.created"
                and sub.post_ids is not None
                and effective_post_id is not None
                and effective_post_id not in sub.post_ids
            ):
                continue
            self._enqueue_drop_oldest(sub.queue, sse_event)

        return sse_event

    @staticmethod
    def _enqueue_drop_oldest(queue: asyncio.Queue[SSEEvent], item: SSEEvent) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def replay_since(
        self,
        last_event_id: int,
        *,
        post_ids: set[int] | frozenset[int] | None = None,
    ) -> list[SSEEvent]:
        replayed: list[SSEEvent] = []
        for ev in self._history:
            if ev.id <= last_event_id:
                continue
            if (
                ev.event != "post.created"
                and post_ids is not None
                and ev.post_id is not None
                and ev.post_id not in post_ids
            ):
                continue
            replayed.append(ev)
        return replayed

    def subscribe(
        self,
        *,
        post_ids: set[int] | frozenset[int] | None = None,
        user_id: int | None = None,
        last_event_id: int | None = None,
    ) -> tuple[int, asyncio.Queue[SSEEvent], list[SSEEvent]]:
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        frozen_posts = frozenset(post_ids) if post_ids is not None else None
        queue: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        self._subscribers[sub_id] = _Subscriber(
            sub_id=sub_id,
            queue=queue,
            post_ids=frozen_posts,
            user_id=user_id,
        )
        replayed = (
            self.replay_since(last_event_id, post_ids=frozen_posts)
            if last_event_id is not None
            else []
        )
        return sub_id, queue, replayed

    def unsubscribe(self, sub_id: int) -> None:
        self._subscribers.pop(sub_id, None)

    def is_user_viewing(self, user_id: int, post_id: int) -> bool:
        """Return True if `user_id` has an active SSE stream watching `post_id` (or all posts)."""
        for sub in self._subscribers.values():
            if sub.user_id != user_id:
                continue
            if sub.post_ids is None or post_id in sub.post_ids:
                return True
        return False
