"""Voice-lane monitor: the voice-bot serving process, read by the hw tick.

The voice mission (LFM3-Audio-2B listen->speak front, study
repo-research/lfm3-voice-bot-study.md) runs a llama.cpp server beside the
:8020 engine on unified memory. The dashboard tile answers the four
questions that matter while it runs: is the process alive, what does it
cost (RSS + the SYSTEM MemAvailable delta — on GB10 the GPU allocations
live in the shared pool and are invisible to RSS, so the delta is the
honest footprint), and what the last round-trip latency was when the
stack exposes one.

Detection is by LISTENING PORT, not process name: the voice stack is
whatever binary binds `voice_port` (llama-server today, anything tomorrow)
— the same /proc/net/tcp -> socket-inode -> /proc/<pid>/fd chain
app/live_engines.py uses for engine discovery. The pid is cached and
revalidated cheaply (alive + still owns the listener); the full pids scan
only runs when the cache misses.

Baseline honesty: the delta is measured against the MemAvailable reading
of the tick the process was FIRST SEEN — a spawn the dashboard watched.
If LIMCARS started while the voice process was already running there is
no pre-spawn reading to compare against, so delta stays None ("–")
instead of inventing a baseline.

Config: voice_port (0 = feature off, zero cost per tick) and the optional
voice_latency_url — a JSON endpoint carrying round_trip_ms / ttft_ms /
latency_ms; failures hold the last value for 10 ticks, then go null
rather than freeze a stale figure forever.
"""
from __future__ import annotations

import json
import urllib.request

from . import procs
from .live_engines import _listen_sockets, _pid_inodes

LATENCY_KEYS = ("round_trip_ms", "ttft_ms", "latency_ms")
LAT_HOLD_TICKS = 10


def find_voice_pid(port: int, cached: int | None = None) -> int | None:
    """Pid owning the TCP listener on `port`, reusing the cached pid when
    it is still alive and still owns a socket of that port."""
    inos = {ino for (p, ino) in _listen_sockets() if p == port}
    if not inos:
        return None
    if cached is not None and procs.alive(cached) and (_pid_inodes(cached) & inos):
        return cached
    for pid in procs.pids():
        try:
            if _pid_inodes(pid) & inos:
                return pid
        except OSError:
            continue
    return None


def rss_gb(pid: int) -> float | None:
    """VmRSS of a process in GiB, or None if /proc says otherwise."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 2**20, 2)
    except (OSError, ValueError):
        pass
    return None


def read_latency(url: str) -> float | None:
    """First numeric of LATENCY_KEYS from a JSON endpoint; None on any
    failure — the caller decides how long to hold the last good value."""
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=0.4) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        for k in LATENCY_KEYS:
            v = data.get(k)
            if isinstance(v, (int, float)):
                return float(v)
    except Exception:  # noqa: BLE001 — any endpoint hiccup is a miss
        pass
    return None