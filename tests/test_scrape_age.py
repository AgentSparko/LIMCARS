"""scrape_age_s + the lean /api/engine-status read (paste-day ergonomics).

Background (NEAR-MISS-LOG 2026-11-10 11:4x): an external tool read the full
/api/snapshot (11.3 MB, ~1 s) and mis-selected a dead config row. Two fixes:
every engine row now carries scrape_age_s (seconds since the last SUCCESSFUL
scrape, computed server-side at read time — a frozen last row is otherwise
indistinguishable from a fresh one without reader-clock math), and a lean
engine-filtered endpoint returns a few hundred bytes per engine instead of
the whole board.
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.sampler import EngineSampler
from app.server import Handler
from app.sse import Broker

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _mk(sid="t", url="http://x"):
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    return EngineSampler(cfg, {"id": sid, "url": url, "kind": "vllm"},
                         Broker(), store=None)


class _Engines:
    def __init__(self, samplers):
        self._s = {s.id: s for s in samplers}

    def all(self):
        return list(self._s.values())

    def get(self, sid):
        return self._s.get(sid)


class _App:
    def __init__(self, samplers):
        self.engines = _Engines(samplers)


def _handler(samplers):
    h = Handler.__new__(Handler)
    h.app = _App(samplers)
    return h


# ---------------- scrape_age_s on the full snapshot -------------------------

def test_never_scraped_engine_reports_none_not_zero():
    """A zero age on an engine that never answered would claim freshness."""
    s = _mk()
    assert s.last_scrape == 0.0
    assert s.snapshot()["scrape_age_s"] is None


def test_scrape_age_measured_from_last_success_at_read_time():
    s = _mk()
    s.last_scrape = time.time() - 5.0
    age = s.snapshot()["scrape_age_s"]
    assert 4.5 <= age <= 6.0
    # recomputed per read: the age grows while nobody scrapes again
    assert s.snapshot()["scrape_age_s"] >= age


# ---------------- /api/engine-status ----------------------------------------

def _fill(s, t=None):
    s.ring.append({"t": t or time.time(), "running": 2, "waiting": 0,
                   "kv_pct": 42.0, "kv_used_tokens": 100000,
                   "kv_pool_tokens": 663179, "decode_tps_now": 61.5,
                   "prefill_tps_now": 900.0, "ttft_p50_ms": 250.0},
                  t or time.time())


def test_lean_body_carries_headlines_and_no_series():
    s = _mk()
    s.last_scrape = time.time() - 1.0
    _fill(s)
    obj, code = _handler([s])._engine_status({})
    assert code == 200 and obj["ok"] is True
    row = obj["engines"]["t"]
    assert row["alive"] is s.alive and row["url"] == "http://x"
    assert row["latest"]["kv_pct"] == 42.0
    assert row["latest"]["kv_pool_tokens"] == 663179
    # the lean promise: no series/rings/hw — a tool can read this in one packet
    assert "series" not in row and "latest" in row
    assert "ttft_p50_ms" not in row["latest"]      # headline subset only
    assert len(json.dumps(obj)) < 2048


def test_scrape_age_survives_json_roundtrip():
    s = _mk()
    s.last_scrape = time.time() - 30.0
    obj, _ = _handler([s])._engine_status({})
    row = json.loads(json.dumps(obj))["engines"]["t"]
    assert 29.0 <= row["scrape_age_s"] <= 31.0


def test_engine_filter_returns_single_engine():
    a, b = _mk("a", "http://x:1"), _mk("b", "http://x:2")
    b.last_scrape = time.time() - 2.0
    b.alive = True
    obj, code = _handler([a, b])._engine_status({"engine": ["b"]})
    assert code == 200
    assert obj["engine"]["id"] == "b" and "engines" not in obj


def test_unknown_engine_is_404_not_empty_ok():
    """A typo'd id must fail loudly — silently returning the wrong engine's
    row is exactly the paste-day mis-selection this endpoint exists to kill."""
    obj, code = _handler([_mk()])._engine_status({"engine": ["nope"]})
    assert code == 404 and obj["ok"] is False


def test_alive_first_ordering_matches_snapshot():
    """The unfiltered list keeps snapshots()' alive-first contract so a tool
    that takes the first entry gets a live engine, never a dead config row."""
    dead, live = _mk("aaa-dead", "http://x:1"), _mk("zzz-live", "http://x:2")
    live.alive = True
    obj, _ = _handler([dead, live])._engine_status({})
    assert list(obj["engines"]) == ["zzz-live", "aaa-dead"]


def test_never_scraped_survives_lean_read():
    obj, _ = _handler([_mk()])._engine_status({})
    row = obj["engines"]["t"]
    assert row["scrape_age_s"] is None and row["latest"] == {}


# ---------------- JS falsification for the Seen cell ------------------------

@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_seen_label_js():
    r = subprocess.run([NODE, str(ROOT / "tests" / "js" / "scrape_age.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr