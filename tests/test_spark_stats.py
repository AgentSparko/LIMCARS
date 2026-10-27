"""The sparkline stats table: what the dashboard shows past the ring horizon.

The rings are a one-hour buffer because nothing can render a 1 s sample older than that. The one
exception measured in the code is the pair of bubble sparklines, which draw ~200 points of a
derived stat out of the ring JSON over a 6 h and a 24 h span. Storing the displayed stat at the
stride it is displayed at — instead of the 2 Hz samples behind it — is what makes the one-hour
ring honest: the sparklines keep their history and the file stops paying for invisible resolution.

These tests pin the three things that make that safe: identical points before and after the
samples are gone, the same capture predicate `series_tail` used to apply in SQL, and a backfill
that harvests pre-upgrade rows before anything prunes them.
"""
from __future__ import annotations

import json
import pathlib
import time

import pytest

from app.store import SPARK_SERIES, Store

T0 = 1_800_000_000.0


def _store(tmp_path, **kw):
    kw.setdefault("persist_seconds", 3600)
    kw.setdefault("ring_full_seconds", 3600)
    return Store(str(tmp_path / "s.db"), **kw)


def _seed(store, engine, t_start, secs, stride=1, month=None, year=None):
    """Write rows at `stride` seconds; values make t%60 / t%120 points identifiable."""
    rows = []
    for i in range(0, secs, stride):
        t = t_start + i
        rows.append((t, {
            "prefill_tps_now": 100.0,
            "cache_hit_pct": (t % 997) / 10.0,
            "savings_month": (t % 1000) / 10.0 if month is None else month,
            "savings_year": (t % 777) / 10.0 if year is None else year,
        }))
    store.save_engine_samples(engine, rows)
    return rows


# ------------------------------------------------------- what the sparkline gets to keep

def test_spark_points_survive_the_rings_being_pruned(tmp_path):
    """The whole point: delete the samples, the sparkline must be byte-for-byte unchanged."""
    store = _store(tmp_path)
    now = time.time()
    _seed(store, "e1", now - 3 * 3600, 3 * 3600)         # three hours at 1 Hz
    before = store.series_tail("e1", "savings_month", 6 * 3600, 60)
    assert before, "no points harvested at all"
    assert all(int(t) % 60 == 0 for t, _ in before)

    store.set_persist_window(3600)                        # rings = one hour only
    kept = store._con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0]
    rows_left = store._con.execute(
        "SELECT COUNT(*) FROM engine_samples WHERE t >= ?", (now - 3600,)).fetchone()[0]
    assert rows_left == kept, "prune left rows older than the hour behind"

    after = store.series_tail("e1", "savings_month", 6 * 3600, 60)
    assert after == before, "the sparkline changed when the samples were deleted"


def test_year_spark_reaches_24_hours_without_any_samples(tmp_path):
    store = _store(tmp_path, persist_seconds=60)          # rings hold ~nothing
    now = time.time()
    _seed(store, "e1", now - 26 * 3600, 26 * 3600)
    pts = store.series_tail("e1", "savings_year", 24 * 3600, 120)
    assert len(pts) > 20 * 60 // 2, f"year spark collapsed to {len(pts)} points"
    assert min(t for t, _ in pts) < now - 20 * 3600, "year spark lost its own history"
    assert store._con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0] < 200


def test_capture_uses_the_same_predicate_the_reader_used(tmp_path):
    """series_tail filtered on `CAST(t AS INTEGER) % stride = 0`, non-null, cap-checked. The
    capture must match it exactly or the sparkline silently gains or loses points."""
    store = _store(tmp_path)
    base = T0 - (T0 % 60)                                  # land exactly on a minute boundary
    store.save_engine_samples("e1", [
        (base, {"savings_month": 1.0, "savings_year": 9.0}),        # t%60==0, t%120==? -> month yes
        (base + 1, {"savings_month": 2.0, "savings_year": 9.0}),    # never shown
        (base + 60, {"savings_month": 3.0, "savings_year": 9.0}),
        (base + 120, {"savings_month": None, "savings_year": 5.0}),  # month null -> not stored
    ])
    got = dict(store.series_tail("e1", "savings_month", 10 * 3600, 60))
    assert got == {int(base): 1.0, int(base + 60): 3.0}, got
    yrs = dict(store.series_tail("e1", "savings_year", 10 * 3600, 120))
    assert int(base + 1) not in yrs and yrs.get(int(base + 120)) == 5.0, yrs


def test_spark_stats_cost_a_fraction_of_the_samples(tmp_path):
    """The claim being made, measured: ~200 displayed points are not 2 Hz of raw rows."""
    store = _store(tmp_path, persist_seconds=2 * 86400)
    now = time.time()
    _seed(store, "e1", now - 26 * 3600, 26 * 3600)        # 26 h at 1 Hz, 3 keys per row
    ring = store._con.execute("SELECT COALESCE(SUM(LENGTH(data)),0) FROM engine_samples").fetchone()[0]
    spark = store._con.execute("SELECT COALESCE(SUM(LENGTH(v)),0) FROM spark_stats").fetchone()[0]
    rows_ring = store._con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0]
    rows_spark = store._con.execute("SELECT COUNT(*) FROM spark_stats").fetchone()[0]
    assert rows_spark < rows_ring / 10, (rows_spark, rows_ring)
    assert spark < ring / 100, (spark, ring)


def test_pre_upgrade_rows_are_harvested_before_they_can_be_pruned(tmp_path):
    """Rows written by the old code (raw INSERT, no capture) must still feed the sparkline after
    a restart, or the upgrade erases 23 hours of history the operator has always seen."""
    path = str(tmp_path / "s.db")
    seed = Store(path, persist_seconds=172800, ring_full_seconds=3600)
    now = time.time()
    rows = [(now - 5 * 3600 + i, {"savings_month": (i % 97) / 7.0, "savings_year": 1.5})
            for i in range(0, 5 * 3600)]
    with seed._lock:                                       # bypass the new capture on purpose
        seed._con.executemany("INSERT INTO engine_samples(t,engine,data) VALUES (?,?,?)",
                              [(t, "e1", json.dumps(r)) for t, r in rows])
        seed._con.commit()
    seed._con.close()

    reopened = Store(path, persist_seconds=3600, ring_full_seconds=3600)
    pts = reopened.series_tail("e1", "savings_month", 6 * 3600, 60)
    assert len(pts) >= 4 * 60, f"backfill lost points: {len(pts)}"
    expect = {int(t): r["savings_month"] for t, r in rows if int(t) % 60 == 0}
    assert all(v == expect[int(t)] for t, v in pts if int(t) in expect)


def test_a_finer_stride_falls_back_to_the_samples(tmp_path):
    """spark_stats only holds the display stride; asking for something finer must not fabricate
    points, so it keeps reading the rings (and gets nothing once they are pruned)."""
    store = _store(tmp_path)
    now = time.time()
    _seed(store, "e1", now - 700, 700)
    fine = store.series_tail("e1", "savings_month", 3600, 30)   # 30 % 60 != 0
    assert all(int(t) % 30 == 0 for t, _ in fine)
    assert store.series_tail("e1", "savings_month", 3600, 60)   # routed to spark_stats


def test_spark_table_is_bounded_by_its_own_window(tmp_path):
    store = _store(tmp_path, spark_hours=2)
    now = time.time()
    _seed(store, "e1", now - 6 * 3600, 6 * 3600)
    store.backfill_spark_stats()
    assert store.series_tail("e1", "savings_year", 30 * 86400, 120), "table emptied itself"
    oldest = min(t for t, _ in store.series_tail("e1", "savings_year", 30 * 86400, 120))
    assert oldest > now - 3 * 3600, "spark stats outlived their configured window"


def test_the_seven_day_prefix_cache_spark_survives_the_hour_buffer(tmp_path):
    """dashboard.js fetchCacheLife() asks /api/history for up to 7 days and reads only t +
    cache_hit_pct. With the rings at one hour that window has to come from the stats table or the
    bubble's whole lifetime sparkline silently becomes the last hour."""
    store = _store(tmp_path)
    now = time.time()
    # capture only records points at t % stride == 0 (the predicate series_tail used), so the
    # seed has to land on that grid or it is testing nothing
    start = int(now - 3 * 86400)
    start -= start % 60
    _seed(store, "e1", float(start), 3 * 86400, stride=60)        # three days, one row/minute
    rows = store.history("engine_samples", "e1", 7 * 86400)
    assert rows, "lifetime sparkline served nothing"
    assert any(r.get("cache_hit_pct") is not None for r in rows)
    beyond = [r for r in rows if r["t"] < now - 3600]
    assert len(beyond) > 100, f"only {len(beyond)} points older than the ring buffer"
    assert all(r.get("cache_hit_pct") is not None for r in beyond)


def test_a_short_ring_window_leaves_no_hole_before_the_stats_take_over(tmp_path):
    """Settings may set persist_seconds as low as 5 min. If history() assumed the raw rows always
    reached a full hour, the minutes between the prune horizon and that assumption would be served
    by neither source -- a silent gap in the middle of the lifetime sparkline."""
    store = Store(str(tmp_path / "hole.db"), persist_seconds=60, ring_full_seconds=3600)
    now = time.time()
    start = int(now - 3 * 3600)
    start -= start % 60
    _seed(store, "e1", float(start), 3 * 3600, stride=60)
    assert store.raw_coverage_s == 60.0
    rows = store.history("engine_samples", "e1", 7 * 86400)
    served = {int(r["t"]) // 60 for r in rows if r.get("cache_hit_pct") is not None}
    wanted = {int(start) // 60 + i for i in range(180)}
    missing = sorted(wanted - served)
    assert not missing, f"{len(missing)} minutes served by neither source, e.g. {missing[:5]}"


def test_the_boot_harvest_is_incremental_but_never_misses_a_point(tmp_path):
    """The watermark exists so a 1 GB ring does not get re-scanned at every boot. It must stay
    invisible to the user: rows written after the last harvest still reach the sparkline."""
    path = str(tmp_path / "wm.db")
    store = _store(tmp_path) if False else Store(path, persist_seconds=3600, ring_full_seconds=3600)
    now = time.time()
    first = int(now - 40 * 60); first -= first % 60
    for i in range(20):
        store.save_engine_samples("e1", [(first + i * 60, {"savings_month": float(i),
                                                          "cache_hit_pct": 90.0 + i})])
    after_first = len(store.series_tail("e1", "savings_month", 6 * 3600, 60))
    store._con.close()

    reopened = Store(path, persist_seconds=3600, ring_full_seconds=3600)   # boot #2: harvest again
    late = int(now - 5 * 60); late -= late % 60
    for i in range(6):
        reopened.save_engine_samples("e1", [(late + i * 60, {"savings_month": 100.0 + i,
                                                            "cache_hit_pct": 80.0})])
    pts = dict(reopened.series_tail("e1", "savings_month", 6 * 3600, 60))
    assert len(pts) == after_first + 6, (len(pts), after_first)   # nothing skipped by the watermark
    assert pts[int(late)] == 100.0
    # and a genuinely fresh database still harvests its whole history
    reopened._con.execute("DELETE FROM spark_stats")
    reopened._con.commit(); reopened._con.close()
    third = Store(path, persist_seconds=3600, ring_full_seconds=3600)
    assert len(third.series_tail("e1", "savings_month", 6 * 3600, 60)) == after_first + 6
    third._con.close()


def test_spark_series_matches_the_consumer(tmp_path):
    """If counters.py ever changes a stride or a key, this table stops matching what is drawn."""
    import inspect

    from app import counters
    src = inspect.getsource(counters)
    for key, (stride, _keep) in SPARK_SERIES.items():
        if key == "cache_hit_pct":
            # drawn by the client straight off /api/history, not by counters.py
            js = (pathlib.Path(__file__).resolve().parent.parent / "web" / "js"
                  / "dashboard.js").read_text()
            assert "cache_hit_pct" in js and "api/history" in js
            continue
        assert f'"{key}"' in src and f"{stride}" in src, f"{key}@{stride} no longer drawn"
