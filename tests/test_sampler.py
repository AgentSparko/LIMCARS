"""Sampler math tests: counter differences, reset handling, window stats."""
import time

from app.sampler import EngineSampler
from app.sse import Broker


def _mk():
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    eng = {"id": "t", "url": "http://x", "kind": "vllm"}
    return EngineSampler(cfg, eng, Broker(), store=None)


def test_window_rate_and_ratio():
    s = _mk()
    now = time.time()
    s._win_delta("prompt_tokens", 100, 0, now, 10.0)      # 100 tok over 10 s
    assert abs(s._win_rate("prompt_tokens") - 10.0) < 1e-6
    s._win_delta("prefix_hits", 30, 0, now, 10.0)
    s._win_delta("prefix_queries", 60, 0, now, 10.0)
    assert s._win_ratio("prefix_hits", "prefix_queries", 100) == 50.0


def test_reset_gives_none_not_negative():
    s = _mk()
    now = time.time()
    s._win_delta("gen_tokens", 50, 100, now, 1.0)  # cur < old → reset
    assert s._counter_win.get("gen_tokens") is None


def test_stale_samples_drop_out_of_window():
    s = _mk()
    old = time.time() - 3600
    s._win_delta("gen_tokens", 10, 0, old, 1.0)
    assert s._win_rate("gen_tokens") is None
