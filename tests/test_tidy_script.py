"""The tidy script is what runs on a user's install the moment they update — test it like it.

Two entry modes share one engine on purpose (a nightly job that differs from the one-shot
migration is how someone loses a year of revenue data), so these exercise the shared engine
plus the gates that decide whether a reclaim is allowed to happen at all.
"""
import importlib.util
import json
import os
import sqlite3
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("limcars_tidy",
                                               os.path.join(ROOT, "scripts", "limcars_tidy.py"))
tidy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tidy)

from app.store import Store          # noqa: E402 — same repo, builds a realistic database


def _fat_db(path, rows=4000):
    st = Store(path, persist_seconds=3600)
    ts = int(time.time()) // 60 * 60
    for i in range(40):
        st.save_counter_bucket("e1", ts - i * 60, {
            "prompt": 100_000, "cached": 80_000, "gen": 5_000, "reqs": 9, "iters": 20,
            "dec_min": 0.3, "dec_peak": 990.0, "spec_sum": 4, "spec_n": 2,
            "cost_hot": 0.0123, "price_slug": "test", "priced_at": time.time(),
            "price_src": "live"})
    blob = json.dumps({f"k{i}": float(i) for i in range(60)})
    st._con.executemany("INSERT INTO engine_samples(t, engine, data) VALUES (?,?,?)",
                        [(time.time() - i, "e1", blob) for i in range(rows)])
    st._con.commit()
    st._con.execute("DELETE FROM engine_samples WHERE t < ?", (time.time() - 60,))
    st._con.commit()
    st._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    st._con.commit()
    st._con.close()
    return st


def test_money_snapshot_covers_every_figure_the_dashboard_shows(tmp_path):
    db = str(tmp_path / "t.db")
    _fat_db(db)
    m = tidy.money(db)
    assert m["prompt"] == 4_000_000 and m["reqs"] == 360
    assert m["bucket_rows"] == 40 and m["dec_peak"] == 990.0
    assert m["cost_hot_sum"] == pytest.approx(0.492)   # 40 x 0.0123, frozen at write time
    assert m["cost_priced_rows"] == 40
    for key in ("prompt", "cached", "gen", "reqs", "iters", "dec_peak", "spec_sum",
                "spec_n", "bucket_rows", "bucket_first", "bucket_last", "eng_rows",
                "hw_rows", "events", "cost_hot_sum", "cost_base_sum", "cost_nocache_sum",
                "cost_priced_rows"):
        assert key in m, f"the gate does not look at {key}"


def test_dry_run_writes_nothing(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    _fat_db(db)
    # A dry run must not rewrite the database. Note the -wal/-shm sidecars are NOT
    # evidence of a write: opening a WAL-mode database read-only creates them. The
    # assertions are therefore about the main file's bytes and about no backup appearing.
    st = os.stat(db)
    before = (st.st_size, st.st_mtime_ns)
    assert tidy.main(["--check", "--db", db, "--quiet"]) == 0
    after = os.stat(db)
    assert (after.st_size, after.st_mtime_ns) == before, "dry run modified the database"
    assert not [f for f in os.listdir(tmp_path) if ".pretidy-" in f], "dry run made a backup"
    out = capsys.readouterr().out
    assert "dry run" in out and "VACUUM would return" in out


def test_apply_reclaims_space_and_keeps_the_backup(tmp_path):
    db = str(tmp_path / "t.db")
    _fat_db(db)
    pre = tidy.money(db)
    size_before = os.path.getsize(db)
    assert tidy.main(["--apply", "--db", db, "--allow-live", "--quiet"]) == 0
    assert os.path.getsize(db) < size_before * 0.8, "no space reclaimed"
    backups = [f for f in os.listdir(tmp_path) if ".pretidy-" in f]
    assert len(backups) == 1, "an apply must leave exactly one backup for rollback"
    post = tidy.money(db)
    assert post == pre, f"money moved across compaction: {_diff(pre, post)}"
    # the WAL must be drained, or the file only looks smaller after a restart
    assert not os.path.exists(db + "-wal") or os.path.getsize(db + "-wal") <= 64 * 1024
    # and the database must still be usable
    st = Store(db)
    assert st.counter_totals("e1", None)["prompt"] == 4_000_000


def test_a_corrupt_file_is_refused_not_installed(tmp_path, monkeypatch):
    """Integrity is a gate, not a nicety: if the rewritten file fails
    integrity_check, the original must come back and the run must fail loudly."""
    db = str(tmp_path / "t.db")
    _fat_db(db)
    real_integrity = tidy.integrity
    monkeypatch.setattr(tidy, "integrity",
                        lambda p: (False, "integrity_check=database disk image is malformed"))
    # a closed database: under --allow-live the backup is kept but never copied back
    monkeypatch.setattr(tidy, "server_holds", lambda p: None)
    rc = tidy.main(["--apply", "--db", db, "--quiet"])
    assert rc == 2, f"expected the gate to fail the run, got rc={rc}"
    monkeypatch.setattr(tidy, "integrity", real_integrity)
    # the restore must have left a working database
    assert tidy.money(db)["prompt"] == 4_000_000


def _server_writes_during_vacuum(monkeypatch, write):
    """The running server's sampler, which keeps writing while the nightly timer compacts."""
    real = tidy.vacuum

    def vacuum(db, *a, **kw):
        r = real(db, *a, **kw)
        write()
        return r
    monkeypatch.setattr(tidy, "vacuum", vacuum)


def test_live_ring_writes_do_not_fail_the_gate(tmp_path, monkeypatch):
    """The timer runs --apply --allow-live beside a server that writes its rings every few
    seconds. Those rows are not money and cannot hold still, so they must not fail the gate."""
    db = str(tmp_path / "t.db")
    _fat_db(db)
    live = Store(db, persist_seconds=3600)
    _server_writes_during_vacuum(
        monkeypatch, lambda: live.save_engine_samples("e1", [(time.time(), {"kv_pct": 1.0})]))
    assert tidy.main(["--apply", "--db", db, "--allow-live", "--quiet"]) == 0


def test_a_failed_gate_never_restores_over_a_live_database(tmp_path, monkeypatch, capsys):
    """restore() deletes the file and copies the backup in. A server holding the old file keeps
    writing to the deleted inode, so everything it records until a restart is lost (measured:
    the server's connection saw 3,049 rows while the file held 3,010)."""
    db = str(tmp_path / "t.db")
    _fat_db(db)
    live = Store(db, persist_seconds=3600)
    ts = int(time.time()) // 60 * 60 + 60
    _server_writes_during_vacuum(monkeypatch, lambda: live.save_counter_bucket("e1", ts, {
        "prompt": 7, "cached": 0, "gen": 1, "reqs": 1, "iters": 1, "dec_min": None,
        "dec_peak": None, "spec_sum": None, "spec_n": 0}))
    assert tidy.main(["--apply", "--db", db, "--allow-live", "--quiet"]) == 2
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM counter_buckets WHERE ts=?", (ts,)
                           ).fetchone()[0] == 1, "the backup was copied over the live database"
    finally:
        con.close()
    assert len([f for f in os.listdir(tmp_path) if ".pretidy-" in f]) == 1, "backup not kept"
    assert "NOT restoring" in capsys.readouterr().err


def test_it_refuses_an_in_use_database(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    _fat_db(db)
    monkeypatch.setattr(tidy, "server_holds", lambda p: "write lock held: test")
    assert tidy.main(["--apply", "--db", db, "--quiet"]) == 3
    # ...but a dry run is always safe, live or not
    assert tidy.main(["--check", "--db", db, "--quiet"]) == 0


def test_disk_headroom_is_checked_before_touching_anything(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    _fat_db(db)
    size = os.path.getsize(db)
    monkeypatch.setattr(tidy, "free_bytes", lambda p: size // 2)   # less than one copy free
    before = tidy.money(db)
    assert tidy.main(["--apply", "--db", db, "--allow-live", "--quiet"]) == 3
    assert tidy.money(db) == before
    assert not [f for f in os.listdir(tmp_path) if ".pretidy-" in f], \
        "it refused, so it must not have written a backup either"


def test_json_mode_is_machine_readable(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    _fat_db(db)
    assert tidy.main(["--json", "--db", db]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["money"]["prompt"] == 4_000_000 and d["size"] > 0 and d["dead_pages_mb"] >= 0


def test_it_works_on_a_pre_migration_database(tmp_path):
    """The target audience is an existing install, whose counter_buckets has no
    cost_hot / price_* columns at all. Refusing such a file — or crashing on it —
    would make the one script meant to rescue big old databases useless to them."""
    db = str(tmp_path / "old.db")
    _fat_db(db)
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE old_buckets(engine TEXT, ts INTEGER, prompt REAL, cached REAL, gen REAL,
        reqs REAL, iters REAL, dec_min REAL, dec_peak REAL, spec_sum REAL, spec_n INTEGER,
        synthetic INTEGER DEFAULT 0, PRIMARY KEY(engine, ts));
      INSERT INTO old_buckets(engine,ts,prompt,cached,gen,reqs,iters,dec_min,dec_peak,
        spec_sum,spec_n,synthetic)
        SELECT engine,ts,prompt,cached,gen,reqs,iters,dec_min,dec_peak,spec_sum,spec_n,
               synthetic FROM counter_buckets;
      DROP TABLE counter_buckets;
      ALTER TABLE old_buckets RENAME TO counter_buckets;
    """)
    con.commit()
    assert "cost_hot" not in {r[1] for r in con.execute("PRAGMA table_info(counter_buckets)")}
    con.close()

    m = tidy.money(db)                       # absent columns read as None, not 0
    assert m["prompt"] == 4_000_000 and m["cost_hot_sum"] is None
    assert tidy.main(["--check", "--db", db, "--quiet"]) == 0
    assert tidy.main(["--apply", "--db", db, "--allow-live", "--quiet"]) == 0
    assert tidy.money(db)["prompt"] == 4_000_000


def _diff(a, b):
    return {k: (a[k], b[k]) for k in a if a[k] != b[k]}
