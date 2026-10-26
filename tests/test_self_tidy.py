"""The dashboard must reclaim its own space, and must not lose anything doing it.

This path is what makes the fix reach real users: they update by pulling and restarting, and
the installs that never installed a tidy timer are the ones still carrying a high-water-mark
file. Measured on a live install: 322 MB of an 818 MB database (39%) was freelist pages that
only a VACUUM returns.

The interesting assertions are the negative ones — declining to run when running would be
reckless — plus the one people forget: the database has to still answer queries afterwards,
because the compaction closes and reopens the connection out from under the running server.
"""
import json
import os
import sqlite3
import time

import pytest

from app.maintenance import Maintenance
from app.store import Store

CFG = {"compact_min_bytes": 1, "compact_reclaim_ratio": 0.10,
       "compact_delay_s": 5, "compact_interval_s": 3600}


def _fat_store(path, rows=4000):
    """A database whose pages were already freed, i.e. the post-prune state a real
    install sits in after retention deletes a day of rings."""
    st = Store(path, persist_seconds=3600)
    now = time.time()
    blob = json.dumps({f"k{i}": float(i) for i in range(60)})
    st._con.executemany(
        "INSERT INTO engine_samples(t, engine, data) VALUES (?,?,?)",
        [(now - i, "e1", blob) for i in range(rows)])
    st._con.commit()
    st._con.execute("DELETE FROM engine_samples")
    st._con.commit()
    st._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    st._con.commit()
    return st


def test_it_vacuums_when_the_freelist_is_worth_it(tmp_path):
    db = str(tmp_path / "t.db")
    st = _fat_store(db)
    before = os.path.getsize(db)
    m = Maintenance(CFG, st)
    rep = m.run_once()
    assert rep.get("vacuumed") is True, rep
    after = os.path.getsize(db)
    assert after < before * 0.7, f"only reclaimed {before - after} of {before} bytes"
    assert m.status["freed_bytes_total"] > 0
    assert m.status["last_vacuum"]
    # WAL drained: a VACUUM whose pages stay in the WAL has not actually shrunk anything
    assert rep.get("wal_bytes", 0) <= 64 * 1024, rep


def test_the_database_still_answers_after_the_reopen(tmp_path):
    """compact() closes and reopens the connection to drain the WAL. If that reopen
    is wrong, the dashboard keeps serving from a dead handle and every later write
    fails — hours after the tidy, far from its cause."""
    db = str(tmp_path / "t.db")
    st = _fat_store(db)
    Maintenance(CFG, st).run_once()
    st.save_counter_bucket("e1", int(time.time()) // 60 * 60, {
        "prompt": 1000, "cached": 250, "gen": 80, "reqs": 2, "iters": 3,
        "dec_min": None, "dec_peak": None, "spec_sum": None, "spec_n": 0})
    t = st.counter_totals("e1", None)
    assert t["prompt"] == 1000 and t["reqs"] == 2
    st.save_engine_samples("e1", [(time.time(), {"kv_pct": 12.5})])
    # history() flattens the JSON blob into the row: {"t":…, "kv_pct":…}
    assert st.history("engine_samples", "e1", 60)[0]["kv_pct"] == 12.5


def test_compaction_never_touches_history(tmp_path):
    db = str(tmp_path / "t.db")
    st = _fat_store(db)
    ts = int(time.time()) // 60 * 60
    for i in range(30):
        st.save_counter_bucket("e1", ts - i * 60, {
            "prompt": 5000, "cached": 1200, "gen": 300, "reqs": 4, "iters": 9,
            "dec_min": 0.2, "dec_peak": 900.0, "spec_sum": 10, "spec_n": 5})
    st.save_counter_bucket("e1", ts + 60, {
        "prompt": 5000, "cached": 1200, "gen": 300, "reqs": 4, "iters": 9,
        "dec_min": None, "dec_peak": None, "spec_sum": None, "spec_n": 0,
        "cost_hot": 1.234567, "price_slug": "test", "priced_at": time.time(),
        "price_src": "live"})
    pre = st.counter_totals("e1", None)
    Maintenance(CFG, st).run_once()
    post = st.counter_totals("e1", None)
    for k in ("prompt", "cached", "gen", "reqs", "iters", "dec_peak", "spec",
              "cost_hot", "money_rows", "money_legacy_rows", "first_ts", "last_ts"):
        assert pre[k] == post[k], f"{k} changed across compaction: {pre[k]} -> {post[k]}"
    # frozen money must survive the rewrite
    con = sqlite3.connect(db)
    assert con.execute("SELECT cost_hot FROM counter_buckets WHERE cost_hot IS NOT NULL"
                       ).fetchone()[0] == pytest.approx(1.234567)
    con.close()


def test_it_declines_to_run_on_a_small_file(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    m = Maintenance(dict(CFG, compact_min_bytes=1 << 20), st)   # a fresh file is a few KB
    rep = m.run_once()
    assert rep["vacuumed"] is False and rep["skipped"] == "small"
    assert "too small" in m.status["note"]


def test_it_declines_when_there_is_nothing_to_reclaim(tmp_path):
    st = _fat_store(str(tmp_path / "t.db"))
    m = Maintenance(CFG, st)
    assert m.run_once()["vacuumed"] is True           # first pass takes the pages
    rep = m.run_once()
    assert rep["vacuumed"] is False and "freelist too small" in m.status["note"]
    assert m.status["last_vacuum"], "a skipped pass must not erase the record of the real one"


def test_it_refuses_when_the_disk_cannot_hold_a_second_copy(tmp_path, monkeypatch):
    """VACUUM rewrites the file: without headroom it fills the disk mid-rewrite, so
    declining is the correct behaviour, not a failure."""
    st = _fat_store(str(tmp_path / "t.db"))
    m = Maintenance(CFG, st)
    # a stub with the one attribute the guard reads; the earlier
    # os.statvfs_result(...) fake had the wrong arity and never reached the guard
    monkeypatch.setattr("shutil.disk_usage",
                        lambda p: type("Usage", (), {"free": 1})())
    rep = m.run_once()
    assert rep["vacuumed"] is False and rep["skipped"] == "disk"
    assert "needs" in m.status["note"]


def test_it_respects_the_opt_out(tmp_path):
    st = _fat_store(str(tmp_path / "t.db"))
    cfg = dict(CFG, auto_compact=False)
    m = Maintenance(cfg, st)
    assert m.run_once()["skipped"] == "disabled"
    assert m.run_once(force=True)["vacuumed"] is True     # an operator asking wins


def test_the_invariant_gate_passes_and_would_stop_a_data_eating_compaction(tmp_path, monkeypatch):
    """A VACUUM is supposed to be unable to lose rows. 'Supposed to' is not a check, so
    the thread compares counts and money across the rewrite and stands down if anything
    moved — including against a concurrent writer, which is why both sides are measured
    against one frozen timestamp rather than as raw totals."""
    db = str(tmp_path / "t.db")
    st = _fat_store(db)
    ts = int(time.time()) // 60 * 60
    for i in range(20):
        st.save_counter_bucket("e1", ts - i * 60, {
            "prompt": 9000, "cached": 4000, "gen": 500, "reqs": 3, "iters": 6,
            "dec_min": None, "dec_peak": None, "spec_sum": None, "spec_n": 0,
            "cost_hot": 0.5, "price_slug": "s", "priced_at": time.time(), "price_src": "live"})
    m = Maintenance(CFG, st)
    assert m.run_once()["vacuumed"] is True
    assert "warning" not in m.status and m.enabled is True

    # now make compaction eat history: it must refuse to trust itself and opt out.
    # A fresh fat database, because the pass above already reclaimed the pages.
    st2 = _fat_store(str(tmp_path / "t2.db"))
    for i in range(20):
        st2.save_counter_bucket("e1", ts - i * 60, {
            "prompt": 9000, "cached": 4000, "gen": 500, "reqs": 3, "iters": 6,
            "dec_min": None, "dec_peak": None, "spec_sum": None, "spec_n": 0,
            "cost_hot": 0.5, "price_slug": "s", "priced_at": time.time(), "price_src": "live"})

    def hungry(**kw):
        r = Store.compact(st2, **kw)
        # destroy a minute that existed BEFORE the cap was taken: this is the
        # data loss the gate exists to catch
        st2._con.execute("DELETE FROM counter_buckets WHERE ts=(SELECT MIN(ts) FROM counter_buckets)")
        st2._con.commit()
        return r

    monkeypatch.setattr(st2, "compact", hungry)
    m2 = Maintenance(CFG, st2)
    rep = m2.run_once(force=True)
    assert rep.get("vacuumed") is True and "invariant_drift" in rep, rep
    assert m2.enabled is False, "it must not keep compacting after losing data"
    assert "invariant check failed" in m2.status["note"]
    assert "counter_buckets" in m2.status["warning"]


def test_a_concurrent_writer_is_not_mistaken_for_drift(tmp_path, monkeypatch):
    """The server keeps sampling during a VACUUM. Rows written after the cap must not
    read as corruption and permanently disable the tidy thread."""
    db = str(tmp_path / "t.db")
    st = _fat_store(db)
    real = st.compact

    def busy(**kw):
        r = real(**kw)
        # written AFTER the cap was taken, exactly like a live sampler would
        st.save_engine_samples("e1", [(time.time() + 5, {"kv_pct": 3.0})])
        st.save_counter_bucket("e1", (int(time.time()) + 5 * 60) // 60 * 60, {
            "prompt": 10, "cached": 1, "gen": 1, "reqs": 1, "iters": 1, "dec_min": None,
            "dec_peak": None, "spec_sum": None, "spec_n": 0})
        return r

    monkeypatch.setattr(st, "compact", busy)
    st.save_engine_samples("e1", [(time.time(), {"kv_pct": 2.0})])    # refill the freelist
    m = Maintenance(CFG, st)
    assert m.run_once(force=True)["vacuumed"] is True
    assert "warning" not in m.status, m.status
    assert m.enabled is True, "late writes must not switch the tidy thread off"


def test_storage_stats_reports_the_dead_space_it_would_reclaim(tmp_path):
    st = _fat_store(str(tmp_path / "t.db"))
    s = st.storage_stats()
    assert s["bytes"] > 0 and s["free_pages"] > 0
    assert s["dead_bytes"] == s["free_pages"] * s["page_size"]
    assert s["rows"]["counter_buckets"] == 0
    assert s["tables_bytes"] == {}, "dbstat must stay opt-in (it walks every page)"
    assert st.storage_stats(tables=True)["tables_bytes"], "?tables=1 should report table sizes"
