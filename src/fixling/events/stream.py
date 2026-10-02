"""In-process pub/sub so the API can stream events over SSE (spec E1)."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

from fixling.models import Event

ALL_RUNS = "*"


@dataclass(eq=False)
class Subscription:
    run_id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)


class EventBus:
    """Thread-safe fan-out: publishers run in worker threads, subscribers live on an asyncio loop."""

    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()

    def subscribe(self, run_id: str = ALL_RUNS) -> Subscription:
        sub = Subscription(run_id=run_id, loop=asyncio.get_running_loop())
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def publish(self, event: Event) -> None:
        with self._lock:
            targets = [s for s in self._subs if s.run_id in (ALL_RUNS, event.run_id)]
        for sub in targets:
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, event)
            except RuntimeError:  # loop already closed: the subscriber is gone
                self.unsubscribe(sub)
