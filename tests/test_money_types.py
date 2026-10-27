"""Money columns must hold numbers, enforced at both boundaries.

Why this file exists: SQLite does not police column types, so a REAL column can happily hold the
text "1200". Nothing complains at write time. Then two things go wrong at once — that row vanishes
from every `SUM()` (so the savings headline under-reports, silently, which is the single worst
failure a money dashboard can have) and the first time Python adds two of them
(`p + g` in counters.py) the endpoint 500s.

The write path coerces; the boot pass repairs what older builds already stored. Both are pinned
here, plus the crash that motivated it.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import counters                      # noqa: E402
from app.store import Store                   # noqa: E402

E = "flashnext"
PRICES = {"input": 0.15, "output": 0.47, "cache_write": 0.20, "cache_read": 0.016}


def _clean(path):
    for p in (path, path + "-wal", path + "-shm"):
        if os.path.exists(p):
            os.remove(p)


def _hour_ts():
    return int(time.time()) // 3600 * 3600


def _bucket(prompt=1_000_000, cached=800_000, gen=50_000, reqs=40):
    return {"prompt": prompt, "cached": cached, "gen": gen, "reqs": reqs, "iters": 60,
            "dec_min": 5, "dec_peak": 12, "spec_sum": 4, "spec_n": 10,
            "cost_hot": 42.0, "cost_base": 90.0, "cost_nocache": 100.0,
            "price_slug": "x", "price_in": 0.15, "price_out": 0.47,
            "price_cw": 0.2, "price_cr": 0.016, "price_crhot": 0.03,
            "priced_at": 1700000000, "price_src": "cloud"}


def _typeof(db, col):
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute(
            f"SELECT DISTINCT typeof({col}) FROM counter_buckets")]     # noqa: S608
    finally:
        con.close()


def test_a_formatted_number_never_becomes_text_money(tmp_path):
    """A dialect handing back "92.8"-style values must not leave text money behind."""
    db = str(tmp_path / "w.db")
    _clean(db)
    st = Store(db)
    dirty = _bucket()
    # values SQLite's REAL affinity cannot rescue on its own: "1200" it converts itself, so only a
    # separator/formatting artifact actually reaches the column as TEXT and tests the guard
    dirty.update(prompt="1,200", cached="8 00", reqs="7")
    st.save_counter_bucket(E, _hour_ts(), dirty)
    assert _typeof(db, "prompt") == ["real"], "text reached a REAL money column"
    tot = st.counter_totals(E, None)
    assert tot["prompt"] == 1200.0, f"formatted money was lost instead of recovered: {tot}"
    assert tot["cached"] == 800.0 and tot["reqs"] == 7.0, tot


def test_null_stays_null_because_null_means_not_priced(tmp_path):
    """Coercing NULL to 0.0 would turn "we never knew the price" into "$0 earned"."""
    db = str(tmp_path / "n.db")
    _clean(db)
    st = Store(db)
    b = _bucket()
    for k in ("cost_hot", "cost_base", "cost_nocache", "priced_at"):
        b[k] = None
    st.save_counter_bucket(E, _hour_ts(), b)
    con = sqlite3.connect(db)
    row = con.execute("SELECT cost_hot, cost_base, cost_nocache, priced_at FROM counter_buckets").fetchone()
    con.close()
    assert row == (None, None, None, None), row


def test_legacy_text_money_is_repaired_and_the_headline_comes_back(tmp_path):
    """The point of the boot pass: a row an older build stored as text must count again, at face
    value. Written through a raw connection on purpose — that is what a previous version did to a
    user's database, and reopening the Store is what the update does.
    """
    db = str(tmp_path / "l.db")
    _clean(db)
    ts = _hour_ts()
    good = {"prompt": 1_000_000, "cached": 0, "gen": 0, "reqs": 1, "iters": 1,
            "dec_min": 0, "dec_peak": 0, "spec_sum": 0, "spec_n": 0}
    st = Store(db)
    st.save_counter_bucket(E, ts - 3600, good)          # one honest bucket

    con = sqlite3.connect(db)                            # legacy damage: 500k as TEXT
    con.execute("INSERT INTO counter_buckets (engine, ts, prompt, cached, gen, reqs, iters,"
                " dec_min, dec_peak, spec_sum, spec_n) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (E, ts, "500,000", 0, 0, 2, 0, 0, 0, 0, 0))
    con.commit()
    assert "text" in _typeof(db, "prompt")
    before = sqlite3.connect(db).execute(
        "SELECT SUM(prompt) FROM counter_buckets WHERE engine=?", (E,)).fetchone()[0]
    con.close()
    # measured, not assumed: SUM() does NOT drop the text row, it reads the leading digits — so the
    # headline was understating this minute by 1000x (500,000 counted as 500) with nothing on screen
    assert before == 1_000_500, f"expected the 1000x understatement, got {before}"

    st = Store(db)                                        # the update: reopening repairs it
    assert _typeof(db, "prompt") == ["real"], "boot pass left text money behind"
    assert st.repair_numeric_columns() == {}, "the repair is not idempotent — it runs twice"
    tot = st.counter_totals(E, None)
    assert tot["prompt"] == 1_500_000, f"money headline lost the repaired row: {tot}"
    # the sharp half: a repair must not invent money — "500,000" is 500000, not 500 (what CAST gave)
    con = sqlite3.connect(db)
    assert con.execute("SELECT prompt FROM counter_buckets WHERE ts=?", (ts,)).fetchone()[0] == 500_000
    con.close()


def test_dirty_money_cannot_500_the_money_endpoint(tmp_path):
    """counters.py writes `p + g`; one text column used to raise there."""
    db = str(tmp_path / "d.db")
    _clean(db)
    ts = _hour_ts()
    st = Store(db)
    st.save_counter_bucket(E, ts - 3600, _bucket())
    con = sqlite3.connect(db)
    con.execute("UPDATE counter_buckets SET prompt='1000abc' WHERE ts=?", (ts - 3600,))
    con.commit()
    con.close()

    st = Store(db)                                        # repair runs on open
    rows = st.minute_rows(E)
    assert rows, "the repaired row disappeared"
    assert all(isinstance(v, float) for _, p, c, g, _bm in rows for v in (p, c, g)), rows
    rep = counters.report(st, E, "all", E, override=PRICES)
    assert rep["totals"]["prompt"] >= 0
