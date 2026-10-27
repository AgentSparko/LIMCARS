"""The rings: what is allowed on disk, and what thinning is allowed to remove.

The ring tables are 94% of this database's file size, so two things are pinned here rather
than trusted to a code read:

  * storage keeps only fields some consumer names — and the boundary is storage-only, so the
    live row the browser receives is untouched (a change that reached the live row is how the
    projected-savings bubble started reporting an aggregated hour as if it were measured);
  * thinning may collapse resolution, never coverage: every minute that had a sample keeps one.
"""
from __future__ import annotations

import json
import time

import pytest

from app.store import Store

T0 = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "r.db"), persist_seconds=172800, ring_full_seconds=3600)


# ---------------------------------------------------------------- what may be stored

def test_unread_fields_never_reach_the_database(store):
    row = {"prefill_tps_now": 1200.0, "kv_pct": 41.0,
           "savings": {"day": 4.53, "year": 1655.25, "tokens": {"prompt": 6680195}},
           "itl_avg_ms": 12.0, "itl_n_1h": 3, "decode_time_avg_ms": 8.0,
           "prefill_time_p50_1h_ms": 1.0, "queue_time_n": 2}
    store.save_engine_samples("e1", [(T0, row)])
    stored = json.loads(store._con.execute(
        "SELECT data FROM engine_samples WHERE engine='e1' AND t=?", (T0,)).fetchone()[0])
    assert stored["prefill_tps_now"] == 1200.0 and stored["kv_pct"] == 41.0
    for gone in ("savings", "itl_avg_ms", "itl_n_1h", "decode_time_avg_ms",
                 "prefill_time_p50_1h_ms", "queue_time_n"):
        assert gone not in stored, f"{gone} is stored but nothing reads it"


def test_the_caller_row_is_not_mutated(store):
    """The dict passed in is the same object the SSE broker hands to every browser. Stripping
    in place would silently remove the projected-savings bubble from the live dashboard."""
    row = {"prefill_tps_now": 1.0, "savings": {"day": 4.53}, "itl_avg_ms": 12.0}
    store.save_engine_samples("e1", [(T0, row)])
    assert row["savings"] == {"day": 4.53} and row["itl_avg_ms"] == 12.0


def test_dynamically_keyed_fields_are_kept(store):
    """histMs/histBasis build these names at runtime ("ttft" + "_avg_1h_ms"), so a literal grep
    finds no consumer and a grep-based cleanup would delete a working chart. They stay."""
    row = {"ttft_avg_1h_ms": 90.0, "ttft_n_1h": 12, "e2e_p50_1h_ms": 400.0,
           "e2e_avg_1h_ms": 410.0, "e2e_n_1h": 9}
    store.save_engine_samples("e1", [(T0, row)])
    stored = json.loads(store._con.execute(
        "SELECT data FROM engine_samples WHERE engine='e1' AND t=?", (T0,)).fetchone()[0])
    assert set(row) <= set(stored), "a dynamically-read field was dropped"


def test_hardware_sensor_spam_is_not_stored(store):
    """Only what no reader names. load/load5/swap/disk/net look like spam to a grep over the JS,
    but hw.series_keys() charts them from _BASE_KEYS — an earlier grep-based verdict here deleted
    working panel series, which is why tests/test_render_contract.py now decides this list."""
    row = {"cpu_pct": 1.5, "mem_total_gb": 120.0, "gpu0_util": 88.0,
           "load1": 1.74, "load5": 1.2, "swap_used_gb": 0.0, "net_r_mbs": 1.2,
           "temp_acpitz_temp3": 44.0, "temp_mt7925_phy0_temp1": 41.0}
    store.save_hw_samples([(T0, row)])
    stored = json.loads(store._con.execute(
        "SELECT data FROM hw_samples WHERE t=?", (T0,)).fetchone()[0])
    assert {"cpu_pct", "mem_total_gb", "gpu0_util"} <= set(stored)
    for charted in ("load1", "load5", "swap_used_gb", "net_r_mbs"):
        assert charted in stored, f"{charted} is charted by hw._BASE_KEYS and must be stored"
    for gone in ("temp_acpitz_temp3", "temp_mt7925_phy0_temp1"):
        assert gone not in stored


def test_every_deny_listed_field_is_actually_gone(store):
    """The lists are the policy, so the policy is what gets checked -- field by field. A name that
    silently stops being stripped (a rename, a refactor of _lean) is unread data being stored."""
    fat = {k: {"nested": {"deep": [1, 2, 3] * 20}} for k in Store._DROP_ENGINE}
    fat["prefill_tps_now"] = 1.0
    store.save_engine_samples("e1", [(T0, fat)])
    stored = json.loads(store._con.execute(
        "SELECT data FROM engine_samples WHERE t=?", (T0,)).fetchone()[0])
    assert set(stored) == {"prefill_tps_now"}, sorted(set(stored) - {"prefill_tps_now"})
    junk = {k: 44.0 for k in Store._DROP_HW}
    junk["cpu_pct"] = 1.0
    store.save_hw_samples([(T0, junk)])
    stored = json.loads(store._con.execute(
        "SELECT data FROM hw_samples WHERE t=?", (T0,)).fetchone()[0])
    assert set(stored) == {"cpu_pct"}, sorted(set(stored) - {"cpu_pct"})


def test_ring_stats_is_cached_so_a_poll_never_scans_the_rings_under_the_lock(store):
    """/api/storage polls this. Recomputing SUM(LENGTH(data)) per poll is a full ring scan while
    the sampler waits for the same lock — the cached value is the point, so it must be cached."""
    _tick(store, time.time() - 10)
    first = store.ring_stats()
    store.save_hw_samples([(time.time(), {"cpu_pct": 99.0})])          # real data changes...
    assert store.ring_stats() == first                                   # ...still the cached answer
    store._ring_stats_cache = None                                      # and fresh after the TTL
    assert store.ring_stats()["hw_samples"]["rows"] != first["hw_samples"]["rows"]


def test_a_text_metric_cannot_cost_the_engine_its_samples(store):
    """A dialect that publishes cache_hit_pct as "92.8" would raise inside the write path while
    the store lock was held — losing the whole batch, every batch, for that engine. Dropping the
    unplottable point is the only acceptable outcome here."""
    base = T0 - (T0 % 60)
    store.save_engine_samples("e1", [
        (base, {"prefill_tps_now": 100.0, "cache_hit_pct": "92.8", "savings_month": None,
                "savings_year": "n/a"}),
        (base + 60, {"prefill_tps_now": 110.0, "cache_hit_pct": 91.5, "savings_month": 399.0}),
    ])
    assert store._con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0] == 2
    pts = dict(store.series_tail("e1", "savings_month", 10 * 3600, 60))
    # savings_month is None on the first row -> no point; values are real floats, never strings
    assert pts == {int(base + 60): 399.0}, pts
    store._con.execute("INSERT INTO engine_samples(t,engine,data) VALUES (?,?,?)",
                       (base, "e1", json.dumps({"savings_month": "n/a"})))   # unparseable -> skipped
    store._con.commit()
    ch = dict(store.series_tail("e1", "cache_hit_pct", 10 * 3600, 60))
    assert ch == {int(base): 92.8, int(base + 60): 91.5}, ch


def test_a_stored_row_fits_the_byte_budget(store):
    """Budget, not aesthetics: this file is 94% rings, and the row used to carry a duplicated
    projection blob that was 24% of it."""
    row = {f"field_{i}": i * 1.5 for i in range(40)}
    row["savings"] = {"day": 1, "month": 2, "year": 3, "basis": "x" * 80,
                      "price": {"input": 0.15, "name": "Qwen" * 10}}
    row["itl_avg_ms"] = 1.0
    store.save_engine_samples("e1", [(T0, row)])
    blob = store._con.execute("SELECT data FROM engine_samples WHERE t=?", (T0,)).fetchone()[0]
    assert len(blob) < 1200, f"engine row grew to {len(blob)} B"


# ---------------------------------------------------------------- what thinning may remove

def _tick(store, ts, **extra):
    row = {"prefill_tps_now": 100.0, "decode_tps_now": 50.0, "kv_pct": 3.0}
    row.update(extra)
    store.save_engine_samples("e1", [(ts, row)])
    store.save_hw_samples([(ts, {"cpu_pct": 1.0})])


def test_thinning_collapses_old_rows_to_one_per_minute(store):
    now = time.time()
    old = now - 3 * 3600                       # three hours ago: below the full-resolution hour
    for sec in range(120):                     # two full minutes at one row per second
        _tick(store, old + sec)
    assert store._con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0] == 120
    def mins(table):
        return store._con.execute(
            f"SELECT COUNT(DISTINCT CAST(t AS INTEGER)/60) FROM {table}").fetchone()[0]
    expected = sum(mins(t) for t in ("engine_samples", "hw_samples"))
    rows_before = {t: store._con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in ("engine_samples", "hw_samples")}
    rep = store.thin_rings()
    assert rep.get("blocked") is None, rep
    # exactly one row survives per (minute, engine); every other sample in that minute loses
    assert rep["thinned"] == sum(rows_before.values()) - expected, rep
    assert rep["minutes_before"] == rep["minutes_after"] == expected, rep
    for table in ("engine_samples", "hw_samples"):
        assert store._con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == mins(table)


def test_the_full_resolution_hour_is_never_thinned(store):
    now = time.time()
    for sec in range(45):                      # inside the last hour: second resolution stays
        _tick(store, now - 30 - sec)
    rep = store.thin_rings()
    assert store._con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0] == 45
    assert rep["thinned"] == 0


def test_thinning_never_loses_a_minute(store):
    """The proof is the point: coverage survives even when resolution does not."""
    now = time.time()
    old = now - 5 * 3600
    minutes = [7, 19, 33, 41]                  # scattered minutes, uneven samples each
    for m in minutes:
        for sec in range(0, 60, 7):
            _tick(store, old // 60 * 60 + m * 60 + sec)
    def minute_keys():
        return sorted(int(m) for (m,) in store._con.execute(
            "SELECT DISTINCT CAST(t AS INTEGER)/60 FROM engine_samples"))
    before = minute_keys()
    assert len(before) == len(minutes), f"seed did not land in {len(minutes)} minutes: {before}"
    rep = store.thin_rings()
    after = minute_keys()
    assert before == after, (before, after, rep)      # coverage preserved exactly
    assert store._con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0] == len(minutes)


def test_thinning_does_not_touch_the_money(store):
    store.save_counter_bucket("e1", int(time.time()) - 3 * 3600, {
        "prompt": 5e6, "cached": 4e6, "gen": 5e4, "reqs": 9, "iters": 40,
        "dec_min": 0.2, "dec_peak": 900.0, "spec_sum": 3.0, "spec_n": 2.0, "synthetic": 0})
    before = store.counter_totals("e1", None)
    old = time.time() - 4 * 3600
    for sec in range(90):
        _tick(store, old + sec)
    store.thin_rings()
    after = store.counter_totals("e1", None)
    assert after["prompt"] == pytest.approx(before["prompt"])
    assert after["cost_hot"] == pytest.approx(before["cost_hot"])
