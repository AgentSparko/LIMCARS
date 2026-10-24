"""Per-client context chart backend.

The engine only reports AGGREGATE KV, so the real per-client footprint is
tracked from the Hermes state DBs: SUM(LENGTH(content))/4 over active,
un-compacted messages (the same 4-char heuristic Hermes uses internally).

Only HOT sessions are sampled — ended sessions are not resident in the KV
cache and render as flat-line noise (old LIMCARS T13 lesson). Samples live
in a 48 h ring; the chart shows the last 30 min.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

CC_LIVE_H = 6        # hot = activity within 6 h (sampling filter)
CC_ACTIVE_MIN = 5    # legend "active" = activity within 5 min
CC_RETENTION_S = 48 * 3600
CC_HZ = 0.1          # 1 sample / 10 s — context grows slowly


def _profile_dbs() -> list[Path]:
    from .hermes_home import hermes_state_dbs
    return [db for _, db in hermes_state_dbs()]


def samples_now() -> list[dict]:
    live_cut = time.time() - CC_LIVE_H * 3600
    act_cut = time.time() - CC_ACTIVE_MIN * 60
    out = []
    for db in _profile_dbs():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error:
            continue
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                """SELECT m.session_id, COALESCE(s.title,'') AS title,
                          COALESCE(SUM(LENGTH(m.content)),0)/4 AS est,
                          COUNT(*) AS n, MAX(s.last_activity_at) AS la
                   FROM messages m JOIN sessions s ON s.id = m.session_id
                   WHERE m.active = 1 AND m.compacted = 0
                     AND s.ended_at IS NULL
                     AND s.last_activity_at >= ?
                     AND m.session_id IN (
                         SELECT id FROM sessions
                         WHERE ended_at IS NULL
                           AND last_activity_at >= ?
                         ORDER BY last_activity_at DESC LIMIT 5)
                   GROUP BY m.session_id HAVING est > 500""",
                (live_cut, live_cut)).fetchall()
        except sqlite3.Error:
            c.close()
            continue
        c.close()
        for r in rows:
            out.append({
                "client": r["session_id"],
                "title": r["title"] or str(r["session_id"])[-8:],
                "tokens": int(r["est"]),
                "messages": int(r["n"]),
                "active": (r["la"] or 0) >= act_cut,
            })
    return out


class ClientSampler:
    def __init__(self, broker):
        self.broker = broker
        self.lock = threading.Lock()
        self.rows: deque[tuple[float, dict]] = deque()
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def start(self) -> None:
        self._th = threading.Thread(target=self._loop, daemon=True,
                                    name="clientsampler")
        self._th.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(1.0 / CC_HZ):  # 10 s per tick
            try:
                ts = time.time()
                fresh = samples_now()
                with self.lock:
                    for s in fresh:
                        self.rows.append((ts, s))
                    cutoff = ts - CC_RETENTION_S
                    while self.rows and self.rows[0][0] < cutoff:
                        self.rows.popleft()
                if self.broker:
                    self.broker.publish("clients", json.dumps(
                        {"ts": ts, "clients": fresh}))
            except Exception:  # noqa: BLE001 — never fatal
                pass

    def window(self, seconds: float) -> dict:
        cutoff = time.time() - seconds
        with self.lock:
            series = [(t, s) for t, s in self.rows if t >= cutoff]
        # legend only for clients with a sample IN the window (stale-session
        # flat lines were the old dash's #1 noise complaint)
        meta: dict[str, dict] = {}
        for t, s in series:
            m = meta.setdefault(s["client"], {"client": s["client"],
                                              "title": s["title"],
                                              "active": False, "last_ts": 0})
            m["last_ts"] = max(m["last_ts"], t)
            m["active"] = m["active"] or s["active"]
            m["title"] = s["title"] or m["title"]
        return {"series": [{"ts": t, **s} for t, s in series],
                "clients": sorted(meta.values(),
                                  key=lambda m: -m["last_ts"]),
                "window_s": seconds}
