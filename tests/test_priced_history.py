"""Money must be history, not a function of today's price file.

Cloud rates move: pricing.json is refreshed by the fetcher and a user can edit it by hand.
Before this, every $ figure was recomputed at read time, so a provider price cut silently
rewrote what the dashboard claimed it had saved last month. Buckets now carry the cost frozen
at write time plus the rates that produced it. These tests are the contract:

  * a bucket records its cost and its price identity when it is written;
  * changing the price afterwards must not move the recorded money;
  * the stamp is write-once, so no re-save (restart, gap backfill, importer) can re-price it;
  * a row with no price known must read as UNKNOWN, never as $0.00;
  * a window mixing priced and unpriced rows says so instead of quietly blending bases.
"""
import sqlite3
import time

import pytest

from app import pricing
from app.counters import daily_savings, report
from app.store import Store

# two distinct, valid price tables — the whole point is that they differ
P_OLD = {"input": 1.0, "cache_write": 1.0, "cache_read": 0.10,
         "cache_read_hot": 0.10, "output": 2.0, "slug": "old"}
P_NEW = {"input": 4.0, "cache_write": 4.0, "cache_read": 0.90,
         "cache_read_hot": 0.90, "output": 8.0, "slug": "new"}

BUCKET = {"prompt": 1_000_000, "cached": 400_000, "gen": 100_000,
          "reqs": 3, "iters": 7, "dec_min": None, "dec_peak": None,
          "spec_sum": None, "spec_n": 0}


def _minute(ts, price, **over):
    b = dict(BUCKET, **over)
    b.update(pricing.money_stamped(b["prompt"], b["cached"], b["gen"], price))
    return b


def _raw_cost(db, ts):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT cost_hot, cost_base, cost_nocache, price_slug, "
                           "price_in, price_src FROM counter_buckets WHERE ts=?",
                           (int(ts) // 60 * 60,)).fetchone()
    finally:
        con.close()


def test_a_bucket_carries_its_cost_and_price_identity(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    ts = now - now % 60
    st.save_counter_bucket("e1", ts, _minute(ts, P_OLD))
    hot, base, nc, slug, pin, src = _raw_cost(str(tmp_path / "t.db"), ts)
    expected = pricing.hot_costs(BUCKET["prompt"], BUCKET["cached"], BUCKET["gen"], P_OLD)
    assert abs(hot - round(expected["hot"], 6)) < 1e-9
    assert abs(nc - round(expected["nocache"], 6)) < 1e-9
    assert slug == "old" and pin == 1.0 and src == "live"
    # the basis itself: uncached at write rate, cached at hot-read rate, gen at output
    assert abs(hot - (0.6 * 1.0 + 0.4 * 0.10 + 0.1 * 2.0)) < 1e-6


def test_editing_the_price_afterwards_does_not_rewrite_history(tmp_path):
    """THE regression this whole change exists for."""
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    ts = now - now % 60
    st.save_counter_bucket("e1", ts, _minute(ts, P_OLD))

    before = report(st, "e1", "all", "M", P_OLD)["totals"]
    after = report(st, "e1", "all", "M", P_NEW)["totals"]   # prices quadrupled
    assert after["cost_hot"] == pytest.approx(before["cost_hot"])
    assert after["money_basis"] == "as_written"
    # and it is genuinely 4x away from what a re-priced read would have said
    re_priced = pricing.hot_costs(BUCKET["prompt"], BUCKET["cached"], BUCKET["gen"], P_NEW)
    assert after["cost_hot"] < re_priced["hot"] / 2

    d = daily_savings(st, [{"id": "e1", "model": "M"}], P_NEW, 7)["totals"]
    assert d["priced"] == "as_written"
    assert d["cost"] == pytest.approx(round(before["cost_hot"], 4), abs=1e-4)


def test_the_stamp_is_write_once(tmp_path):
    """A minute gets re-saved on restart, gap backfill and import. Re-saving must
    refresh the tokens but must never re-price money that is already earned."""
    db = str(tmp_path / "t.db")
    st = Store(db)
    now = int(time.time())
    ts = now - now % 60
    st.save_counter_bucket("e1", ts, _minute(ts, P_OLD))
    first = _raw_cost(db, ts)[0]
    # same minute again, this time at quadrupled prices and with more traffic
    st.save_counter_bucket("e1", ts, _minute(ts, P_NEW, prompt=2_000_000))
    hot, _, _, slug, _, _ = _raw_cost(db, ts)
    assert hot == first, "money was re-priced by a re-save"
    assert slug == "old", "price provenance was overwritten"


def test_unknown_money_is_never_reported_as_zero(tmp_path):
    """Legacy rows (and rows written while no price was known) must read as
    'today's rate, not history' — a silent 0.0 would look like a free hour."""
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    ts = now - now % 60
    st.save_counter_bucket("e1", ts, dict(BUCKET))          # no money keys at all
    t = report(st, "e1", "all", "M", P_OLD)["totals"]
    assert t["money_basis"] == "current"
    assert t["unpriced_rows"] == 1 and t["priced_rows"] == 0
    assert t["cost_hot"] > 0, "unpriced history must not collapse to $0"
    assert t["cost_hot"] == pytest.approx(
        pricing.hot_costs(BUCKET["prompt"], BUCKET["cached"], BUCKET["gen"], P_OLD)["hot"])
    assert sqlite3.connect(str(tmp_path / "t.db")).execute(
        "SELECT cost_hot FROM counter_buckets").fetchone()[0] is None


def test_no_price_means_no_fabricated_number():
    assert pricing.money_stamped(1e6, 1e5, 1e4, None) == {}
    assert pricing.money_stamped(1e6, 1e5, 1e4, {}) == {}


def test_a_mixed_window_reports_its_mixed_basis(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    t1 = now - now % 60 - 120
    t2 = now - now % 60 - 60
    st.save_counter_bucket("e1", t1, _minute(t1, P_OLD))
    st.save_counter_bucket("e1", t2, dict(BUCKET))           # unpriced row
    t = report(st, "e1", "all", "M", P_NEW)["totals"]
    assert t["money_basis"] == "mixed"
    assert (t["priced_rows"], t["unpriced_rows"]) == (1, 1)
    stored = pricing.hot_costs(BUCKET["prompt"], BUCKET["cached"], BUCKET["gen"], P_OLD)["hot"]
    re_priced = pricing.hot_costs(BUCKET["prompt"], BUCKET["cached"], BUCKET["gen"], P_NEW)["hot"]
    assert t["cost_hot"] == pytest.approx(stored + re_priced)


def test_daily_chart_labels_each_day_by_its_money_basis(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    today = now - now % 86400
    for m in range(3):                                        # priced day
        ts = today + m * 60
        st.save_counter_bucket("e1", ts, _minute(ts, P_OLD))
    for m in range(3):                                        # unpriced yesterday
        st.save_counter_bucket("e1", today - 86400 + m * 60, dict(BUCKET))
    r = daily_savings(st, [{"id": "e1", "model": "M"}], P_NEW, 7)
    modes = sorted({e["priced"] for d in r["days"] for e in d["engines"].values()})
    assert modes == ["as_written", "current"]
    assert r["totals"]["priced"] == "mixed"
    assert (r["totals"]["priced_rows"], r["totals"]["unpriced_rows"]) == (3, 3)


def test_synthetic_seed_stays_unpriced_rather_than_guessed(tmp_path):
    """The lifetime seed bucket summarises engine history from before LIMCARS
    existed; stamping it with today's rate would dress a guess up as fact."""
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    ts = now - 3660
    st.save_counter_bucket("e1", ts, dict(BUCKET, synthetic=1))
    row = sqlite3.connect(str(tmp_path / "t.db")).execute(
        "SELECT cost_hot, synthetic FROM counter_buckets").fetchone()
    assert row[0] is None and row[1] == 1
