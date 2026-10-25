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


# --- prefill honesty -------------------------------------------------------
# User report: the prefill chart peaked above 10 000 tok/s while the engine's
# own counters said ~140. Cause: a window's worth of completion-charged prompt
# tokens divided by the ~1 s span of the ticks a burst happened to land in.

def _seed_prefill(s, now, prompt, hits, pf_secs, base_age):
    """A stretch of prompt/hit deltas plus prefill_time coarse anchors whose
    oldest sample is base_age seconds old (the settle-window anchors)."""
    s._counter_win["prompt_tokens"] = [(now - base_age + 0.1, prompt, base_age)]
    s._counter_win["prefix_hits"] = [(now - base_age + 0.1, hits, base_age)]
    zero = ([(1.0, 0.0), (float("inf"), 0.0)], 0.0, 0.0)
    cur = ([(1.0, 0.0), (float("inf"), 1.0)], pf_secs, 1.0)
    s._hist_prev["prefill_time"] = cur
    s._hist_hr["prefill_time"] = [(now - base_age, zero)]


def test_prefill_needs_a_real_window_before_it_publishes():
    s = _mk()
    now = time.time()
    _seed_prefill(s, now, prompt=10_000, hits=0, pf_secs=1.0, base_age=2.0)
    assert s._prefill_rate(now) is None      # a 2 s baseline is not a window


def test_prefill_rate_is_uncached_tokens_per_prefill_second():
    s = _mk()
    now = time.time()
    _seed_prefill(s, now, prompt=60_000, hits=45_000, pf_secs=5.0, base_age=40.0)
    v = s._prefill_rate(now)
    assert v is not None and abs(v - 3_000.0) < 1e-6   # 15k uncached / 5 s


def test_prefill_never_publishes_an_implausible_speed():
    s = _mk()
    now = time.time()
    _seed_prefill(s, now, prompt=1_000_000, hits=0, pf_secs=1.0, base_age=40.0)
    assert s._prefill_rate(now) is None      # refused, not clamped and shown
