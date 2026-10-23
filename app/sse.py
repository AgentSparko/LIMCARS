"""Tiny SSE broker: per-topic ring of recent events + subscriber queues.

Every event carries a monotonically increasing `id` (= topic sequence).
A client that drops reconnects with `Last-Event-ID` and gets any missed
events replayed from the topic ring (bounded), then live tail — the same
resumable-stream property TandemLLM's dashboard relies on.

Publishers: the sampler threads. Consumers: HTTP handler generators.
Slow consumers never block the publisher: queue.put_nowait, drop oldest
when full (a lagging browser tab shows a gap, the sampler stays 2 Hz).
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque


class Topic:
    def __init__(self, name: str, replay: int = 600):
        self.name = name
        self._seq = itertools.count(1)
        self._ring: deque[tuple[int, str]] = deque(maxlen=replay)
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.last_ts = 0.0

    def publish(self, data: str) -> int:
        sid = next(self._seq)
        with self._lock:
            self._ring.append((sid, data))
            subs = list(self._subs)
            self.last_ts = time.time()
        for q in subs:
            try:
                q.put_nowait((sid, data))
            except queue.Full:
                try:  # drop the oldest unread event, keep this one
                    q.get_nowait()
                except queue.Empty:
                    pass
                q.put_nowait((sid, data))
        return sid

    def subscribe(self, since: int | None = None) -> tuple[queue.Queue, list[tuple[int, str]]]:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            backlog = [(sid, d) for sid, d in self._ring if since is not None and sid > since]
            self._subs.append(q)
        return q, backlog

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass


class Broker:
    def __init__(self):
        self._topics: dict[str, Topic] = {}
        self._lock = threading.Lock()

    def topic(self, name: str, replay: int = 600) -> Topic:
        with self._lock:
            t = self._topics.get(name)
            if t is None:
                t = self._topics[name] = Topic(name, replay)
            return t

    def publish(self, name: str, data: str) -> int:
        return self.topic(name).publish(data)

    def idle_seconds(self, name: str) -> float:
        t = self._topics.get(name)
        return time.time() - t.last_ts if t and t.last_ts else 1e9
