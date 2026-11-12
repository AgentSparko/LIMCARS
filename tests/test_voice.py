"""Voice-lane tile pins (app/voice.py + HwSampler._voice_tick).

The tile's honesty contract:
- feature off (voice_port 0) -> zero per-tick work, no keys in the row;
- the delta is the SYSTEM MemAvailable drop — on GB10 the GPU side of a
  model's footprint lives in the unified pool and RSS alone under-reports
  it by ~4x (measured 2026-11-10: RSS 1.0 GiB, system delta 4.6-4.8 GiB);
- a baseline exists only for a spawn the dashboard WATCHED (an off tick
  came first). Booted with the voice stack already up -> delta None,
  never an invented baseline;
- latency failures hold the last good value for LAT_HOLD_TICKS, then go
  null rather than freeze a stale figure forever.
"""
import os

import pytest

from app import voice
from app.hw import HwSampler
from app.sse import Broker


def _sampler(port=8181):
    return HwSampler({"hw_hz": 1.0, "ring_seconds": 900, "voice_port": port,
                      "voice_latency_url": ""}, Broker())


# ------------------------------------------------------- monitor state ----

def test_feature_off_touches_nothing():
    s = _sampler(port=0)
    row = {"mem_avail_gb": 13.0}
    s._voice_tick(row)
    assert row == {"mem_avail_gb": 13.0}


def test_boot_with_process_running_has_no_fake_baseline(monkeypatch):
    monkeypatch.setattr(voice, "find_voice_pid", lambda p, c=None: 42)
    monkeypatch.setattr(voice, "rss_gb", lambda pid: 1.0)
    s = _sampler()
    row = {"mem_avail_gb": 12.5}
    s._voice_tick(row)
    assert row["voice_alive"] == 1 and row["voice_pid"] == 42
    assert row["voice_delta_gb"] is None          # never invented
    s._voice_tick(row := {"mem_avail_gb": 8.0})
    assert row["voice_delta_gb"] is None          # still no pre-spawn reading


def test_watched_spawn_measures_the_system_delta(monkeypatch):
    pid = [None]
    monkeypatch.setattr(voice, "find_voice_pid", lambda p, c=None: pid[0])
    monkeypatch.setattr(voice, "rss_gb", lambda x: 1.0)
    monkeypatch.setattr(voice, "read_latency", lambda url: None)
    s = _sampler()
    s._voice_tick(row := {"mem_avail_gb": 13.5})
    assert row["voice_alive"] == 0                # off tick: baseline watch armed
    pid[0] = 42
    s._voice_tick(row := {"mem_avail_gb": 13.4})  # spawn seen, baseline 13.4
    assert row["voice_delta_gb"] == pytest.approx(0.0)
    s._voice_tick(row := {"mem_avail_gb": 8.7})   # model loaded
    assert row["voice_delta_gb"] == pytest.approx(4.7)
    pid[0] = None
    s._voice_tick(row := {"mem_avail_gb": 13.5})
    assert row["voice_alive"] == 0 and row["voice_delta_gb"] is None
    pid[0] = 99                                   # restart: fresh baseline
    s._voice_tick(row := {"mem_avail_gb": 13.3})
    assert row["voice_pid"] == 99 and row["voice_delta_gb"] == pytest.approx(0.0)


def test_latency_holds_then_expires(monkeypatch):
    lat = [211.0]
    monkeypatch.setattr(voice, "find_voice_pid", lambda p, c=None: 42)
    monkeypatch.setattr(voice, "rss_gb", lambda pid: 1.0)
    monkeypatch.setattr(voice, "read_latency", lambda url: lat[0])
    s = _sampler()
    s._voice_tick(row := {"mem_avail_gb": 13.0})
    assert row["voice_latency_ms"] == 211.0 and row["voice_latency_stale"] == 0
    lat[0] = None                                  # endpoint goes quiet
    for _ in range(voice.LAT_HOLD_TICKS):
        s._voice_tick(row := {"mem_avail_gb": 13.0})
        assert row["voice_latency_ms"] == 211.0    # held...
        assert row["voice_latency_stale"] == 1     # ...and MARKED as held
    s._voice_tick(row := {"mem_avail_gb": 13.0})
    assert row["voice_latency_ms"] is None         # stale, not frozen
    assert row["voice_latency_stale"] == 0


# ------------------------------------------------------- pid detection ----

def test_find_pid_by_listener_and_cache(monkeypatch):
    monkeypatch.setattr(voice, "_listen_sockets", lambda: [(8181, "sockA")])
    owned = {42: {"sockA"}, 7: {"sockB"}}
    monkeypatch.setattr(voice, "_pid_inodes", lambda pid: owned.get(pid, set()))
    monkeypatch.setattr(voice.procs, "pids", lambda: [7, 42])
    monkeypatch.setattr(voice.procs, "alive", lambda pid: True)
    assert voice.find_voice_pid(8181) == 42
    assert voice.find_voice_pid(8181, cached=42) == 42      # cache revalidated
    assert voice.find_voice_pid(9999) is None               # no listener
    monkeypatch.setattr(voice.procs, "alive", lambda pid: pid != 42)
    owned.pop(42)                       # dead pid: kernel reclaimed its fds
    assert voice.find_voice_pid(8181, cached=42) is None    # cache died too


def test_cached_pid_alive_but_listener_moved_returns_new_owner(monkeypatch):
    # M2 gap (audit 2026-11-10): pid reuse — the cached pid is still ALIVE
    # (some other process holds that pid now) but the listener socket's
    # inode belongs to a NEW pid. Revalidation must check inode ownership,
    # not just aliveness; a cache that trusts `alive` alone reports the
    # wrong process forever (the mutation `if cached is not None and
    # procs.alive(cached): return cached` must FAIL this pin).
    monkeypatch.setattr(voice, "_listen_sockets", lambda: [(8181, "sockB")])
    owned = {42: {"sockA"}, 77: {"sockB"}}   # 42 alive but owns a stale socket
    monkeypatch.setattr(voice, "_pid_inodes", lambda pid: owned.get(pid, set()))
    monkeypatch.setattr(voice.procs, "pids", lambda: [7, 42, 77])
    monkeypatch.setattr(voice.procs, "alive", lambda pid: True)
    assert voice.find_voice_pid(8181, cached=42) == 77


def test_new_owner_gets_null_latency_not_carried(monkeypatch):
    # sampler side of M2: when detection returns a DIFFERENT pid (the old
    # process died, a new one owns the listener), a latency held from the
    # previous process must go null immediately — never shown, even one
    # tick, as if it belonged to the new owner.
    pid = [42]
    lat = [211.0]
    monkeypatch.setattr(voice, "find_voice_pid", lambda p, c=None: pid[0])
    monkeypatch.setattr(voice, "rss_gb", lambda x: 1.0)
    monkeypatch.setattr(voice, "read_latency", lambda url: lat[0])
    s = _sampler()
    s._voice_tick(row := {"mem_avail_gb": 13.0})
    assert row["voice_latency_ms"] == 211.0
    lat[0] = None                                # endpoint quiet: value held
    s._voice_tick(row := {"mem_avail_gb": 13.0})
    assert row["voice_latency_ms"] == 211.0 and row["voice_latency_stale"] == 1
    pid[0] = 77                                  # restart: new owner detected
    s._voice_tick(row := {"mem_avail_gb": 13.0})
    assert row["voice_pid"] == 77
    assert row["voice_latency_ms"] is None       # not carried to new process
    assert row["voice_latency_stale"] == 0


def test_find_pid_real_chain_finds_this_process():
    # smoke on the real chain: pytest's own listening socket, if any, or the
    # dashboard's port — proves the /proc/net/tcp -> fd scan works unpatched
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        port = srv.getsockname()[1]
        assert voice.find_voice_pid(port) == os.getpid()
    finally:
        srv.close()


def test_rss_reads_real_process():
    rss = voice.rss_gb(os.getpid())
    assert rss is not None and rss > 0.01
    assert voice.rss_gb(999_999) is None


# ------------------------------------------------------- latency parse ----

class _Resp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_latency_key_priority_and_misses(monkeypatch):
    import urllib.request as ur
    monkeypatch.setattr(ur, "urlopen",
                        lambda url, timeout=0: _Resp(
                            b'{"ttft_ms": 130, "round_trip_ms": 211}'))
    assert voice.read_latency("http://x") == 211.0     # round_trip wins
    monkeypatch.setattr(ur, "urlopen",
                        lambda url, timeout=0: _Resp(b'{"ttft_ms": 130}'))
    assert voice.read_latency("http://x") == 130.0
    monkeypatch.setattr(ur, "urlopen",
                        lambda url, timeout=0: _Resp(b'not json'))
    assert voice.read_latency("http://x") is None

    def boom(url, timeout=0):
        raise OSError("hung")
    monkeypatch.setattr(ur, "urlopen", boom)
    assert voice.read_latency("http://x") is None
    assert voice.read_latency("") is None              # unconfigured: no call