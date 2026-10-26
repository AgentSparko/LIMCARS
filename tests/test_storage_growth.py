"""Ring retention and compaction.

The 1 s engine/hardware ring IS the database: on a real 3-day history the file
measured 615 MB with freelist_count = 0, i.e. every page live, while the year-long
per-day summary the UI actually shows comes from the hourly counter_buckets and
costs 3.7 ms. These tests pin the two knobs that keep that from growing on the
operator: the window shrinks live, and shrinking actually returns space.
"""
import os
import sqlite3
import time

from app.store import Store


def _fill(st, engine, n, span_s):
    """n 1 s rows spread over span_s seconds ending now."""
    now = time.time()
    st.save_engine_samples(engine,
                           [(now - span_s + i, {"decode_tps_now": 10.0 + i})
                            for i in range(n)])


def test_shrinking_the_window_prunes_without_a_restart(tmp_path):
    st = Store(str(tmp_path / "t.db"), persist_seconds=604800)
    _fill(st, "e1", 600, span_s=3600)
    con = sqlite3.connect(str(tmp_path / "t.db"))
    assert con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0] == 600

    # 5 minutes of history: everything older than that must go, immediately
    applied = st.set_persist_window(300)
    assert applied == 300.0
    left = con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0]
    assert left < 600, f"window change did not prune ({left} rows left)"


def test_window_is_clamped_so_a_typo_cannot_mean_all_the_history(tmp_path):
    st = Store(str(tmp_path / "t.db"), persist_seconds=3600)
    assert st.set_persist_window(1) == 300.0            # 5 min floor
    assert st.set_persist_window(10 ** 9) == 604800.0   # 7 d ceiling


def _footprint(path):
    """Total bytes on disk. Under WAL the main file can be 4 KB while the whole
    dataset sits in -wal, so measuring path alone reports a fiction — which is
    how this test first failed. The WAL is checkpointed+truncated by compact()."""
    return sum(os.path.getsize(p) for p in (path, path + "-wal", path + "-shm")
               if os.path.exists(p))


def test_compaction_returns_pages_to_the_filesystem(tmp_path):
    """DELETE leaves pages on the freelist; without this the file never shrinks,
    which is exactly the 585 MB complaint. VACUUM must actually reduce size."""
    path = str(tmp_path / "t.db")
    st = Store(path, persist_seconds=604800)
    _fill(st, "e1", 8000, span_s=8000)
    before = st.compact()
    assert before["vacuumed"] is False, "nothing was freed, so VACUUM must not run"

    st.set_persist_window(300)                       # drop almost all rows
    size_after_delete = _footprint(path)
    assert size_after_delete > 200_000, "fixture too small to prove anything"

    res = st.compact()
    assert res["vacuumed"] is True, "freelist was large but compaction skipped"
    assert res["bytes_after"] < res["bytes_before"]
    assert _footprint(path) < size_after_delete, "on-disk footprint did not shrink"
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_compaction_leaves_a_healthy_file_alone(tmp_path):
    """No freelist => VACUUM is pure I/O for nothing. Must be a no-op."""
    path = str(tmp_path / "t.db")
    st = Store(path, persist_seconds=604800)
    _fill(st, "e1", 200, span_s=200)
    size = _footprint(path)
    res = st.compact()
    assert res["vacuumed"] is False
    assert _footprint(path) == size


def test_ring_data_survives_until_the_window_says_otherwise(tmp_path):
    """Retention must not eat the operator's recent history: a chart reading the
    last hour needs the rows to still be there."""
    st = Store(str(tmp_path / "t.db"), persist_seconds=172800)
    _fill(st, "e1", 300, span_s=300)
    rows = st.history("engine_samples", "e1", 3600)
    assert len(rows) >= 250, f"recent ring rows missing: {len(rows)}"
