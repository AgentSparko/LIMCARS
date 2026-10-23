"""Time-indexed rings. Wall-clock trimmed, NOT count trimmed: a tick is one
sample of `hz` and each entry carries its own timestamp, so gaps (engine
restarting, sampler starved) show as gaps, never as smeared data.

The sampler owns the rings and appends from its thread; readers (API/SSE
snapshot) take a shallow list copy under a lock — cheap at these rates.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class Ring:
    """Ring of (t, row-dict) trimmed to `seconds` of wall clock."""

    __slots__ = ("_d", "_seconds", "_lock")

    def __init__(self, seconds: float):
        self._d: deque[tuple[float, dict]] = deque()
        self._seconds = seconds
        self._lock = threading.Lock()

    def append(self, row: dict, t: float | None = None) -> None:
        t = time.time() if t is None else t
        with self._lock:
            self._d.append((t, row))
            cutoff = t - self._seconds
            d = self._d
            while d and d[0][0] < cutoff:
                d.popleft()

    def window(self, seconds: float) -> list[tuple[float, dict]]:
        cutoff = time.time() - seconds
        with self._lock:
            return [(t, r) for t, r in self._d if t >= cutoff]

    def all(self) -> list[tuple[float, dict]]:
        with self._lock:
            return list(self._d)

    def last(self) -> dict | None:
        with self._lock:
            return self._d[-1][1] if self._d else None

    def __len__(self) -> int:
        return len(self._d)
