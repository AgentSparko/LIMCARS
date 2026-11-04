"""Ledger check: the minutes LIMCARS stored must add up to how far the engine's own lifetime
counters moved over the same span, and a minute lost or counted twice must show up."""
import time

from app.store import Store
from test_sampler import _mk, _vllm_counters


def _run(s, t0, ts):
    for t in ts:
        s._rate_minute(t0 + t, _vllm_counters(t))


def test_stored_minutes_match_the_engine_counters(tmp_path):
    s = _mk()
    s.store = Store(str(tmp_path / "a.db"))
    t0 = time.time() // 60 * 60 - 3600
    _run(s, t0, (0, 60.5, 121, 181.5, 242))
    au = s.counter_audit
    assert au["ok"] and not au["off"]
    assert au["engine"]["prompt"] == au["stored"]["prompt"] == 2000 * 242
    assert au["engine"]["gen"] == 200 * 242 and au["engine"]["cached"] == 1200 * 242


def test_a_minute_counted_twice_is_flagged(tmp_path):
    s = _mk()
    s.store = Store(str(tmp_path / "b.db"))
    t0 = time.time() // 60 * 60 - 3600
    _run(s, t0, (0, 60, 120))
    # an import writing a minute the live sampler also covers (disjoint key, same traffic)
    s.store.save_counter_bucket("t", int(t0 + 600), {"prompt": 5000.0, "cached": 0.0, "gen": 70.0,
                                                     "reqs": 1, "iters": 1, "dec_min": None, "dec_peak": None,
                                                     "spec_sum": 0.0, "spec_n": 0, "synthetic": 0})
    _run(s, t0, (180,))
    au = s.counter_audit
    assert not au["ok"] and au["off"] == {"prompt": 5000.0, "gen": 70.0}


def test_engine_restart_starts_a_new_span(tmp_path):
    s = _mk()
    s.store = Store(str(tmp_path / "c.db"))
    t0 = time.time() // 60 * 60 - 3600
    _run(s, t0, (0, 60, 120))
    s._rate_minute(t0 + 180, {"prompt_tokens": 10.0, "gen_tokens": 1.0, "cached_tokens": 0.0})
    assert s.counter_audit is None
    s._rate_minute(t0 + 240, {"prompt_tokens": 410.0, "gen_tokens": 41.0, "cached_tokens": 0.0})
    au = s.counter_audit
    assert au["ok"] and au["engine"]["prompt"] == 400.0 and au["since"] == t0 + 180


def test_a_verified_span_rolls_forward_and_keeps_its_total(tmp_path):
    s = _mk()
    s.store = Store(str(tmp_path / "d.db"))
    t0 = time.time() // 60 * 60 - 3 * 86400
    _run(s, t0, (0, 86460, 86520, 86580))
    au = s.counter_audit
    assert au["ok"] and au["since"] == t0 and au["engine"]["prompt"] == 2000 * 86580
    assert s._audit["t"] > t0
