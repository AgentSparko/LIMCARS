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


# --- llama.cpp: completion-charged counters are paired, never per-tick ------

def _mk_lcpp():
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    return EngineSampler(cfg, {"id": "l", "url": "http://x", "kind": "llamacpp"}, Broker(), store=None)


def test_llamacpp_prefill_pairs_processed_tokens_with_prompt_seconds():
    s = _mk_lcpp()
    now = time.time()
    s._win_delta("prefill_compute_tokens", 3000, 0, now - 5, 1.0)
    s._win_delta("prefill_seconds", 2.0, 0, now - 5, 1.0)
    assert s._prefill_rate(now) == 1500.0      # no prefill_time hist needed


def test_llamacpp_decode_paired_with_compute_seconds():
    s = _mk_lcpp()
    now = time.time()
    s._win_delta("gen_tokens", 500, 0, now - 1, 0.5)    # one completion burst
    s._win_delta("decode_seconds", 10.0, 0, now - 1, 0.5)
    assert s._paired_rate("gen_tokens", "decode_seconds") == 50.0
    assert s._paired_rate("gen_tokens", "missing") is None


class _SavedStore:
    def __init__(self, saved):
        self.saved = saved

    def get_counter_raw(self, _id):
        return self.saved


def test_restart_restores_baseline_when_a_signal_was_dropped():
    """A baseline written while llama.cpp still mapped `iterations` must still
    restore once that mapping is gone — or downtime traffic is swallowed."""
    s = _mk_lcpp()
    now = time.time()
    s.store = _SavedStore((now - 120, {"prompt_tokens": 1000, "gen_tokens": 1500,
                                       "cached_tokens": 0, "req_success": 0,
                                       "iterations": 1800}))
    s._rate_minute(now, {"prompt_tokens": 1000, "gen_tokens": 1700, "cached_tokens": 0})
    assert s._rate_prev["gen_tokens"] == 1500 and s._rate_prev["iterations"] == 0
    assert s._rate_prev_t == now - 120


def test_restart_still_rejects_a_live_counter_that_went_backwards():
    s = _mk_lcpp()
    now = time.time()
    s.store = _SavedStore((now - 120, {"prompt_tokens": 5000, "gen_tokens": 1500}))

    class _Empty(_SavedStore):
        def counter_totals(self, *_a):
            return {"empty": False}
    s.store = _Empty(s.store.saved)
    s._rate_minute(now, {"prompt_tokens": 1000, "gen_tokens": 1700})
    assert s._rate_prev_t == now and s._rate_prev["prompt_tokens"] == 1000


def _vllm_counters(t):
    """A vLLM engine serving 2000 prompt tok/s (60% from cache) and 200 gen tok/s."""
    return {"prompt_tokens": 1_000_000.0 + 2000 * t, "gen_tokens": 200_000.0 + 200 * t,
            "cached_tokens": 600_000.0 + 1200 * t, "req_success": 500.0 + t}


def test_a_scrape_missing_the_cached_series_is_not_a_counter_reset(tmp_path):
    """One /metrics body without the cached series read as 0, i.e. a counter reset: that
    minute's tokens were dropped and the next minute billed the engine's lifetime cache."""
    from app.store import Store
    s = _mk()
    s.store = Store(str(tmp_path / "s.db"))
    t0 = time.time() // 60 * 60
    for t in (0, 60, 60.5, 120, 180, 180.5, 240, 240.5):
        c = _vllm_counters(t)
        if t in (60, 180):                 # the minute closes on a half-written scrape
            del c["cached_tokens"]
        s._rate_minute(t0 + t, c)
    first, last = _vllm_counters(0), s._rate_prev
    p, cached, g = s.store._con.execute(
        "SELECT SUM(prompt), SUM(cached), SUM(gen) FROM counter_buckets WHERE synthetic=0"
    ).fetchone()
    assert cached == last["cached_tokens"] - first["cached_tokens"], \
        f"billed {cached:.0f} cached tokens, engine served {last['cached_tokens'] - first['cached_tokens']:.0f}"
    assert p == last["prompt_tokens"] - first["prompt_tokens"], "a minute's prompt was dropped"
    assert g == last["gen_tokens"] - first["gen_tokens"]
    # a real restart still rebuilds the baseline, even before the new process exports cached
    s._rate_minute(t0 + 300.5, {"prompt_tokens": 500.0, "gen_tokens": 50.0})
    assert s._rate_prev_t == t0 + 300.5 and s._rate_prev["prompt_tokens"] == 500.0
    s._rate_minute(t0 + 360.5, {"prompt_tokens": 900.0, "gen_tokens": 90.0})
    assert s._rate_prev_t == t0 + 360.5, "the new process's minutes are never closed"


def test_a_counter_that_stays_missing_does_not_stall_the_ledger(tmp_path):
    """The wait for a complete scrape is bounded: if the cached series never comes back while
    prompt/gen keep climbing, the baseline is rebuilt without it and minutes close again."""
    from app.store import Store
    s = _mk()
    s.store = Store(str(tmp_path / "s.db"))
    t0 = time.time() // 60 * 60
    s._rate_minute(t0, _vllm_counters(0))
    for t in range(60, 421, 60):
        c = _vllm_counters(t)
        del c["cached_tokens"]
        s._rate_minute(t0 + t, c)
    assert s._rate_prev_t == t0 + 420, "minute closes stopped for good"
    p, cached = s.store._con.execute(
        "SELECT SUM(prompt), SUM(cached) FROM counter_buckets WHERE synthetic=0").fetchone()
    assert (p, cached) == (2 * 2000 * 60, 0)    # the two minutes after the rebuild at 300 s


def test_served_model_is_re_detected_when_the_port_serves_another_model():
    """A port re-served with a different model must not keep the first model's id."""
    import json
    s = _mk()
    served = {"id": "Qwen/Qwen3.8-Flash-Next"}
    s._fetch = lambda path, timeout=1.5: json.dumps({"data": [dict(served)]}).encode()
    t0 = 1000.0
    s._refresh_model(t0)
    assert s.model == "Qwen/Qwen3.8-Flash-Next"
    served["id"] = "dsv41"
    s._refresh_model(t0 + 1)
    assert s.model == "Qwen/Qwen3.8-Flash-Next", "re-read is rate-limited"
    s._refresh_model(t0 + s.MODEL_REFRESH_S + 1)
    assert s.model == "dsv41"


def test_engine_restart_re_reads_the_model_at_once():
    import json
    s = _mk()
    s._fetch = lambda path, timeout=1.5: json.dumps({"data": [{"id": "a"}]}).encode()
    s._refresh_model(1000.0)
    t0 = time.time() // 60 * 60
    s._rate_minute(t0, {"prompt_tokens": 900.0, "gen_tokens": 90.0, "cached_tokens": 0.0})
    s._rate_minute(t0 + 60.5, {"prompt_tokens": 10.0, "gen_tokens": 1.0, "cached_tokens": 0.0})
    assert s._models_next == 0.0


def test_decode_avg_leaves_idle_ticks_out():
    """User report: the Gen speed avg moved too fast and counted idle time. 20 s decoding at
    30 tok/s then 60 s idle must still read 30, and keep reading it inside the 5 min window."""
    s = _mk()
    now = time.time() - 80
    for i in range(40):
        s._gen_ticks.append((now + i * 0.5, 15, 0.5))
    assert abs(s._gen_active_rate(now + 80) - 30.0) < 1e-6
    assert s._gen_active_rate(now + 20 + s.decode_avg_win_s + 1) is None


def test_loop_warning_needs_every_draft_accepted_for_loop_warn_s():
    """User report: an exact loop shows on the decode chart as a flat band at the ceiling. Its
    cause is MTP acceptance pinned at the maximum; normal decode accepts about half the drafts."""
    s = _mk()
    t0 = time.time() - 100
    def tick(t, acc_ratio):
        row = {}
        for lg, d in (("drafts", 8), ("draft_tokens", 40), ("accepted_tokens", 40 * acc_ratio)):
            s._win_delta(lg, 1000 + d, 1000, t, 0.5)
        s._loop_signal(t, row)
        return row
    for i in range(80):                       # 40 s of normal decode
        r = tick(t0 + i * 0.5, 0.45)
    assert r["spec_full_accept_s"] == 0.0 and r["loop_suspect"] is False
    t1 = t0 + 40
    for i in range(1, 81):                    # then 40 s of full acceptance
        r = tick(t1 + i * 0.5, 1.0)
        if i * 0.5 < 25:
            assert r["loop_suspect"] is False
    assert r["loop_suspect"] is True and 30 <= r["spec_full_accept_s"] <= 41
    r = tick(t1 + 41, 0.5)                    # one ordinary stretch clears it once it fills the window
    for i in range(1, 12):
        r = tick(t1 + 41 + i * 0.5, 0.5)
    assert r["loop_suspect"] is False


def test_kv_tile_holds_the_peak_only_for_listed_models():
    """DSV4.1 flips 0.70 <-> 1.02 % for ~1 s every 128 tokens: its tile shows the 10 s peak,
    every other model keeps the raw reading."""
    for model, want in (("dsv41", 1.02), ("Qwen3.8-Flash-Next", 0.7)):
        s = _mk()
        s.model = model
        t0 = time.time()
        for i, v in enumerate((0.70, 1.02, 0.70, 0.70)):
            row = {"kv_pct": v}
            s._kv_peak(t0 + i * 0.5, row)
        assert row["kv_pct_shown"] == want, (model, row)
        assert (row["kv_pct_peak_s"] is not None) == (model == "dsv41")
    row = {"kv_pct": 0.70}
    s = _mk(); s.model = "dsv41"
    s._kv_peak(t0, {"kv_pct": 1.02})
    s._kv_peak(t0 + 11, row)                       # the spike left the 10 s window
    assert row["kv_pct_shown"] == 0.70


def test_kv_pool_uses_vllms_own_token_capacity_when_published():
    """Hybrid models: blocks x block_size said 603K tokens, vLLM's own figure is 959,768."""
    import re
    body = ('vllm:cache_config_info{block_size="128",kv_cache_size_tokens="959768",'
            'num_gpu_blocks="4712"} 1.0\n')
    m_tok = re.search(r'kv_cache_size_tokens="(\d+)"', body)
    assert m_tok and int(m_tok.group(1)) == 959768
    src = open("app/sampler.py", encoding="utf-8").read()
    assert src.index("kv_cache_size_tokens") < src.index("int(m_nb.group(1)) * int(m_bs.group(1))")


# --- tandemllm live board: aggregate decode speed + KV in tokens ------------

def _mk_tandem():
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    return EngineSampler(cfg, {"id": "q", "url": "http://x", "kind": "tandemllm"},
                         Broker(), store=None)


def test_live_json_only_for_dialects_that_declare_it():
    s = _mk()                                   # vllm: no live board, no request
    called = []
    s._fetch = lambda path, timeout=1.5: called.append(path) or b"{}"
    assert s._live_json({"kind": "vllm"}) is None
    assert called == []


def test_live_json_parses_sse_frame_and_caches():
    # the tandemllm board is an SSE stream: the sampler must take the first
    # data frame and close, never read the open stream to timeout
    s = _mk_tandem()
    calls = []

    def fake_open(req, timeout=None):
        calls.append((req.full_url, timeout))

        class R:
            closed = False
            def __enter__(_self):
                return _self
            def __exit__(_self, *a):
                _self.closed = True
            def __iter__(_self):
                yield b"event: live\n"
                yield b'id: 42\n'
                yield b'data: {"now": {"decode_tps": 42.0}}\n'
                raise AssertionError("must stop at the first data frame")
            def read(_self, n=-1):
                raise AssertionError("must not read() an SSE stream")
            def close(_self):
                _self.closed = True
        return R()
    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_open
    try:
        dial = {"live_json": "/v1/dashboard/live"}
        assert s._live_json(dial) == {"now": {"decode_tps": 42.0}}
        s._live_json(dial)                          # 0.25 s cache: no second call
        assert len(calls) == 1
    finally:
        urllib.request.urlopen = orig


def test_live_json_failure_is_silent():
    s = _mk_tandem()
    import urllib.request
    orig = urllib.request.urlopen
    def boom(req, timeout=None):
        raise OSError("no route")
    urllib.request.urlopen = boom
    try:
        assert s._live_json({"live_json": "/v1/dashboard/live"}) is None
    finally:
        urllib.request.urlopen = orig


def test_live_kv_maps_context_tokens_and_state_gib():
    from app import prom
    from app.sampler import _live_kv
    live = {"engine": {"kv": {"length": 172797, "max_len": 262144}}}
    series = prom.parse('qse_cache_bytes{cache="state"} 1.0e9\n'
                        'qse_state_store_budget_bytes 2147483648.0\n')
    out = _live_kv(live, series)
    assert out["kv_used_tokens"] == 172797 and out["kv_pool_tokens"] == 262144
    assert out["kv_free_tokens"] == 262144 - 172797
    assert out["kv_pct"] == round(100.0 * 172797 / 262144, 3)   # RAW per-lane
    # reading; the TILE % is re-anchored to the pool the sub-line prints by
    # _anchor_tile_pct (one denominator per tile, 2026-11-09 — pinned in
    # test_kv_tile_denominator.py), this stays the fallback when no ref set.
    assert out["kv_pool_gib"] == 2.0 and out["kv_used_gib"] == round(1e9 / 2**30, 2)
    # a board without the kv block yields nothing, never a fake zero
    assert _live_kv({}, {}) == {}


def test_live_kv_shared_pool_committed_rows():
    # the shared KV pool is dynamic — the running engine publishes what it
    # HOLDS (committed rows per lane), not the wizard's planning estimate
    from app.sampler import _live_kv
    health = {"kv_pool": {"slots": [
        {"slot": 0, "committed_rows": 98304}, {"slot": 1, "committed_rows": 200704},
        {"slot": 2, "committed_rows": 4096}]}}
    out = _live_kv({}, {}, health)
    assert out["kv_committed_rows"] == 303104
    assert "303,104 rows committed across 3 lanes" == out["kv_pool_note"]
    assert "kv_pool_note" not in _live_kv({}, {}, {})       # no health, no claim


def test_live_kv_states_the_memory_gate_headroom():
    """The engine's peak gate makes a long prefill WAIT rather than fail (the
    2026-11-07 stall: reserved 73.1 + workspace 5.3 > peak 75.8, 34.9 s of
    waiting, client abandoned at 45 s). The headroom under the peak is the
    number that says the next long request is already queued."""
    from app.sampler import _live_kv
    gib = 2 ** 30
    series = {"qse_memory_bytes": [
        ("a", {"part": "allocated"}, 73.4 * gib),
        ("b", {"part": "peak"}, 75.82 * gib),
        ("c", {"part": "ceiling"}, 64.82 * gib)]}
    health = {"kv_pool": {"slots": [{"committed_rows": 10}], "mem_available_gib": 13.4}}
    out = _live_kv({}, series, health)
    assert out["mem_alloc_gib"] == 73.4
    assert out["mem_peak_gib"] == 75.82
    assert out["mem_gate_headroom_gib"] == 2.42
    assert out["mem_avail_gib"] == 13.4
    bare = _live_kv({}, {}, {})
    assert "mem_gate_headroom_gib" not in bare          # no metric, no claim
    assert "mem_avail_gib" not in bare


def test_tandem_decode_aggregate_is_window_rate_without_live_board():
    # no live board on older builds: the aggregate falls back to the window
    # rate of gen_tokens (METRICS.md's own recommendation), never to the
    # per-request histogram mean (one lane's speed, not the box's)
    s = _mk_tandem()
    now = time.time()
    s._win_delta("gen_tokens", 3000, 0, now - 10.0, 10.0)   # 300 tok/s over 10 s
    assert abs(s._win_rate("gen_tokens") - 300.0) < 1e-6


# --- per-engine KV reference line + live-board flag -------------------------

def test_kv_ref_tokens_is_per_engine_and_optional():
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    s = EngineSampler(cfg, {"id": "q", "url": "http://x", "kind": "tandemllm",
                            "kv_ref_tokens": 663179}, Broker(), store=None)
    snap = s.snapshot()
    assert snap["kv_ref_tokens"] == 663179
    assert snap["kv_ref_source"] == "static"       # typed by hand, not recomputed
    assert "kv_ref_tokens" in __import__("app.sampler", fromlist=["SERIES_KEYS"]).SERIES_KEYS
    other = EngineSampler(cfg, {"id": "v", "url": "http://y", "kind": "vllm"},
                          Broker(), store=None)
    assert other.snapshot()["kv_ref_tokens"] is None      # no key, no line
    assert other.snapshot()["kv_ref_source"] is None


def test_kv_ref_cmd_recomputes_the_marker():
    """The recompute path: the wizard's figure is computed on demand and never
    stored, so a script that reads the engine's registry + launch is what keeps
    the line true after a restart with different knobs."""
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    cmd = ("echo '{\"context_tokens_fit\": 663179, \"launch_context_tokens_fit\": 299960, "
           "\"source\": \"recomputed\"}'")
    s = EngineSampler(cfg, {"id": "q", "url": "http://x", "kind": "tandemllm",
                            "kv_ref_cmd": cmd}, Broker(), store=None)
    assert s.kv_ref_tokens is None
    s._kv_ref_refresh(cmd)                       # the thread body, run inline
    assert s.kv_ref_tokens == 663179.0           # the wizard's figure is the marker
    assert s.kv_ref_source == "recomputed"
    assert s.snapshot()["kv_ref_launch"] == 299960   # the launch env's price rides along
    assert s.kv_ref_error is None


def test_kv_ref_cmd_failure_keeps_the_last_good_marker():
    """A drift gate or a dead script must not blank or invent the line: the old
    number stays and the reason is recorded."""
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    bad = "echo 'this port disagrees with the pinned cases' >&2; exit 3"
    s = EngineSampler(cfg, {"id": "q", "url": "http://x", "kind": "tandemllm",
                            "kv_ref_tokens": 836140, "kv_ref_cmd": bad}, Broker(), store=None)
    s._kv_ref_refresh(bad)
    assert s.kv_ref_tokens == 836140
    assert "pinned cases" in (s.kv_ref_error or "")
    assert s.snapshot()["kv_ref_tokens"] == 836140


def test_kv_ref_cmd_rejects_a_useless_number():
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    cmd = "echo '{\"context_tokens_fit\": 0}'"
    s = EngineSampler(cfg, {"id": "q", "url": "http://x", "kind": "tandemllm",
                            "kv_ref_cmd": cmd}, Broker(), store=None)
    s._kv_ref_refresh(cmd)
    assert s.kv_ref_tokens is None
    assert "context_tokens_fit" in (s.kv_ref_error or "")


def test_kv_ref_maybe_waits_for_its_ttl():
    """The recompute costs a subprocess and reads the engine's registry: it must
    never run on the 2 Hz tick's clock, and never twice at once."""
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    s = EngineSampler(cfg, {"id": "q", "url": "http://x", "kind": "tandemllm",
                            "kv_ref_cmd": "true", "kv_ref_ttl_s": 900}, Broker(), store=None)
    s._kv_ref_at = time.time()
    s._kv_ref_maybe()
    assert s._kv_ref_busy is False               # still fresh: nothing kicked
    s.kv_ref_cmd = None
    s._kv_ref_at = 0.0
    s._kv_ref_maybe()
    assert s._kv_ref_busy is False               # no command configured: nothing to run


def test_snapshot_flags_live_board_engines():
    cfg = {"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900}
    t = EngineSampler(cfg, {"id": "q", "url": "http://x", "kind": "tandemllm"},
                      Broker(), store=None)
    t.kind = "tandemllm"
    v = EngineSampler(cfg, {"id": "v", "url": "http://y", "kind": "vllm"},
                      Broker(), store=None)
    v.kind = "vllm"
    assert t.snapshot()["live"] is True       # client opens the relay only here
    assert v.snapshot()["live"] is False


def test_live_kv_sums_all_inflight_contexts():
    # engine.kv.length is ONE lane; the shared used figure is the sum of every
    # running request's context — 'done' rows linger in the frame and must not
    # count (they already released their rows)
    from app.sampler import _live_kv
    live = {"engine": {"kv": {"length": 66500, "max_len": 262144}},
            "requests": [
                {"phase": "decode", "prompt_tokens": 66000, "tokens": 500},
                {"phase": "prefill", "prompt_tokens": 85000, "tokens": 0},
                {"phase": "done", "prompt_tokens": 70000, "tokens": 900}]}
    out = _live_kv(live, {})
    assert out["kv_used_tokens"] == 66500 + 85000
    assert out["kv_pool_tokens"] == 262144          # per-lane window unchanged
    assert out["kv_pct"] == round(100.0 * 66500 / 262144, 3)   # RAW per-lane
    # reading only: the tile's big % no longer uses it when a shared-pool ref
    # is configured — _anchor_tile_pct re-derives it against the fraction's
    # denominator (the operator's 37.1%-over-73.5% bug; see test_kv_tile_denominator)
    # no requests block (old contract) → the lane figure stands alone
    assert _live_kv({"engine": {"kv": {"length": 10, "max_len": 100}}}, {})["kv_used_tokens"] == 10


def test_decode_prefers_live_field_over_sampled():
    # now.decode_tps is built from the engine's 1 Hz history samples; the
    # tile must follow decode_live_tps, which moves every frame
    from app.sampler import _live_decode_agg
    assert _live_decode_agg({"decode_tps": 40.0, "decode_live_tps": 91.5}) == 91.5
    assert _live_decode_agg({"decode_tps": 40.0}) == 40.0      # old board build
    assert _live_decode_agg({"decode_tps": None, "decode_live_tps": None}) is None
    assert _live_decode_agg({}) is None


# --- cache-hit tile vs token counters (operator report 2026-11-09: tile 99.8% vs
# counters 96.8% on TandemLLM — the hour median of per-tick ratios is not
# volume-weighted; the tile headline must be the same Σ/Σ the counters use)

def test_cache_hit_agg_is_the_counters_math():
    s = _mk()
    now = time.time()
    # tandemllm shape: prefix_hits = qse_prompt_tokens_cached_total (TOKENS),
    # prefix_queries = qse_prompt_tokens_total — fed as two ticks of deltas
    s._win_delta("prefix_hits", 90, 0, now, 1.0)
    s._win_delta("prefix_queries", 100, 0, now, 1.0)
    s._win_delta("prefix_hits", 100, 90, now, 1.0)     # +10 hits
    s._win_delta("prefix_queries", 110, 100, now, 1.0)  # +10 queries
    agg = s._cache_hit_agg(3600.0)
    # counters.py computes 100*Σcached/Σprompt over the same totals:
    assert agg == round(100 * 100 / 110, 10) or abs(agg - 100 * 100 / 110) < 1e-9
    # the median of the two per-tick ratios (90%, 100%) would have said 95% —
    # volume weighting is the point
    assert abs(agg - 95.0) > 1.0


def test_cache_hit_agg_none_without_traffic():
    assert _mk()._cache_hit_agg(3600.0) is None


def test_tile_and_counters_share_the_pair():
    """Locking contract (operator report 2026-11-09): the tile's numerator must be one
    of the counters' cached-token alternatives for EVERY dialect — TandemLLM
    additionally shares the full pair (cached/prompt tokens), so its tile and
    counters are the same fraction by construction; vLLM keeps its own
    block-space hit-rate definition (hits ⊆ cached alternatives)."""
    from app.dialects import DIALECTS
    for kind, d in DIALECTS.items():
        c = d["counter"]
        ph, ct = c.get("prefix_hits"), c.get("cached_tokens")
        if ph is None:
            continue
        alts = ct if isinstance(ct, list) else [ct]
        assert ph == ct or ph in alts, kind
    t = DIALECTS["tandemllm"]["counter"]
    assert t["prefix_hits"] == t["cached_tokens"]
    assert set(t["prefix_queries"]) <= set(t["prompt_tokens"])


def _bkt(p, c, g=10, reqs=1):
    return {"prompt": p, "cached": c, "gen": g, "reqs": reqs, "iters": 1,
            "dec_min": None, "dec_peak": None, "spec_sum": None, "spec_n": 0}


def test_tile_equals_counters_across_a_restart(tmp_path):
    """Restart-mid-window case: the sampler's in-memory window is empty after
    a dashboard restart, but the tile must still show the counters' 1-hour
    number — both read the persisted minute buckets through the same
    store.counter_totals(engine, since, real_only=True) call."""
    from app.store import Store
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    # minute-aligned anchor: the store aligns every bucket ts // 60 * 60 and
    # ON CONFLICT replaces measured-vs-measured, so unaligned offsets collide
    # in one minute bucket for half of all wall-clock seconds — the old
    # now-120/-60/-30 writes lost the -30 bucket into the -60 minute whenever
    # now % 60 < 30, and the tile then read 900/11000 instead of 1700/12000
    # (the known flake; pinned by test_tile_buckets_minute_align_no_collision).
    base = now // 60 * 60
    st.save_counter_bucket("t", base - 180, _bkt(1000, 900))
    st.save_counter_bucket("t", base - 120, _bkt(1000, 800))
    s = EngineSampler({"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900},
                      {"id": "t", "url": "http://x", "kind": "tandemllm"},
                      Broker(), store=st)
    # fresh process: the live-delta path has nothing to aggregate yet
    assert s._cache_hit_agg(3600.0) is None
    v = s._cache_hit_persisted(time.time())
    t = st.counter_totals("t", int(time.time() - 3600), real_only=True)
    assert v == 100.0 * t["cached"] / t["prompt"]      # the counters' own fraction
    assert abs(v - 85.0) < 1e-9                        # 1700/2000
    # 60 s TTL (the counters endpoint's own): a bucket written right after the
    # read is not picked up mid-TTL...
    st.save_counter_bucket("t", base - 60, _bkt(10000, 0, g=1))
    assert s._cache_hit_persisted(time.time() + 1.0) == v
    # ...but lands once the TTL passes
    assert abs(s._cache_hit_persisted(time.time() + 61.0) - 100 * 1700 / 12000) < 1e-9


def test_tile_buckets_minute_align_no_collision(monkeypatch, tmp_path):
    """Regression pin for the 2026-11-09 flake, at the WORST clock phase.

    The store minute-aligns every bucket ts (store.save_counter_bucket) and
    ON CONFLICT(engine,ts) DO UPDATE replaces measured-vs-measured, so three
    unaligned offsets (-120/-60/-30) collapse into two buckets whenever
    now % 60 < 30 — half of all wall-clock seconds. That made
    test_tile_equals_counters_across_a_restart fail ~50% of runs reading
    900/11000 instead of 1700/12000. Pin the killer phase with a fixed clock
    at second-of-minute 20: the minute-aligned write pattern must give the
    same answer at every phase, and this test fails the moment unaligned
    offsets come back."""
    from app.store import Store
    fixed = time.time() // 60 * 60 + 20.0     # second-of-minute 20: old killer
    monkeypatch.setattr(time, "time", lambda: fixed)
    st = Store(str(tmp_path / "c.db"))
    base = int(fixed) // 60 * 60
    st.save_counter_bucket("t", base - 180, _bkt(1000, 900))
    st.save_counter_bucket("t", base - 120, _bkt(1000, 800))
    s = EngineSampler({"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900},
                      {"id": "t", "url": "http://x", "kind": "tandemllm"},
                      Broker(), store=st)
    assert abs(s._cache_hit_persisted(time.time()) - 85.0) < 1e-9
    st.save_counter_bucket("t", base - 60, _bkt(10000, 0, g=1))
    assert abs(s._cache_hit_persisted(time.time() + 61.0) - 100 * 1700 / 12000) < 1e-9
    # and the collision itself stays visible as store behaviour (the product
    # is right: buckets align by design) — an unaligned second write into the
    # same minute REPLACES the first, which is exactly why tests must align.
    st.save_counter_bucket("t", base - 119, _bkt(7, 7))
    t = st.counter_totals("t", int(fixed - 3600), real_only=True)
    assert t["prompt"] == 7 + 1000 + 10000    # base-120 minute now holds only the 7


def test_tile_persisted_zero_is_not_falsy_fallthrough(tmp_path):
    """A true 0.0% hour (no cached tokens) must show 0.0, not fall through
    to the live-delta path through an `or`."""
    from app.store import Store
    st = Store(str(tmp_path / "z.db"))
    st.save_counter_bucket("z", int(time.time()) - 60,
                           _bkt(500, 0, g=5))
    s = EngineSampler({"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900},
                      {"id": "z", "url": "http://x", "kind": "tandemllm"},
                      Broker(), store=st)
    assert s._cache_hit_persisted(time.time()) == 0.0


def test_tile_persisted_reads_the_model_split_key(tmp_path):
    """Per-model buckets (id@Model) must be found: the counters panel reads
    via smp.skey, and a tile reading the bare id gets an empty ledger and
    silently falls back to the live window — tile != counters again (caught
    live on the vLLM :8000 flip 2026-11-09, empty 'flashnext' rows while
    flashnext@Qwen/Qwen3.8-Flash-Next held the hour)."""
    from app.store import Store
    st = Store(str(tmp_path / "m.db"))
    now = int(time.time())
    st.save_counter_bucket("t@M1", now - 60, _bkt(1000, 250))
    s = EngineSampler({"live_hz": 2.0, "window_s": 30.0, "ring_seconds": 900},
                      {"id": "t", "url": "http://x", "kind": "vllm"},
                      Broker(), store=st)
    assert s._cache_hit_persisted(time.time()) is None   # bare id: no buckets
    s.model = "M1"
    assert abs(s._cache_hit_persisted(time.time() + 61.0) - 25.0) < 1e-9
