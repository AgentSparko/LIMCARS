"""The retention ladder: raw minutes fold into hours, hours into days, and nothing older is
deleted until the aggregate provably reproduces it.

Rules these tests pin down (each one is a way an engineer has actually lost revenue history):

  * totals must be byte-identical across a prune,
  * a wrong aggregate must BLOCK the prune rather than lose the minutes,
  * measured and synthetic rows never merge into one number,
  * peaks are extremes, never averages,
  * a ratio (spec accept) is re-derived from summed numerator/denominator, not averaged,
  * the minute/aggregate seam is counted exactly once — no double count, no gap.
"""
from __future__ import annotations

import time

import pytest

from app import tiers
from app.store import Store

DAY, HOUR = 86400, 3600


def _bkt(p, synth=0, cost=None, spec_sum=4.0, spec_n=2.0, dec_min=0.1, dec_peak=900.0):
    b = {"prompt": p, "cached": p * 0.8, "gen": p * 0.05, "reqs": 2, "iters": 7,
         "dec_min": dec_min, "dec_peak": dec_peak,
         "spec_sum": spec_sum, "spec_n": spec_n, "synthetic": synth}
    if cost is not None:
        b.update({"cost_hot": cost, "cost_base": cost * 1.2, "cost_nocache": cost * 3,
                  "price_slug": "glm-5-3", "price_in": 1.0, "price_out": 2.0,
                  "price_cw": 1.25, "price_cr": 0.125, "price_crhot": 0.25,
                  "priced_at": time.time(), "price_src": "file"})
    return b


def _mk(tmp_path, minute_days: float = 3650, history_years: float = 1):
    """A store whose reader horizon is explicit, so a test can prune with the SAME window it
    reads with — the pair has to agree or aged rows merely disappear from the reader's view."""
    return Store(str(tmp_path / f"ladder-{minute_days}-{history_years}.db"),
                 persist_seconds=3600, minute_days=minute_days, history_years=history_years)


@pytest.fixture
def store(tmp_path):
    # minute_days huge so the write-path never prunes behind a test's back; every prune here
    # is an explicit tiers.enforce_retention(...) call the test can inspect.
    return _mk(tmp_path)


def _seed(store, ages, per_hour=6, p=1000.0, engine="e1", now=None):
    """One full hour of `per_hour` minute buckets, `per_hour*60` s wide, at each age in days."""
    now = int(now or time.time())
    written = 0
    for age in ages:
        base = (now - int(age * DAY)) // HOUR * HOUR
        for k in range(per_hour):
            ts = base + k * 60
            store.save_counter_bucket(
                engine, ts,
                _bkt(p + (ts % 37), synth=1 if (ts // 60) % 7 == 0 else 0,
                     cost=(ts % 100) / 100000.0 if (ts // 60) % 3 else None,
                     spec_sum=4.0 + k, spec_n=2.0 + k,
                     dec_min=0.1 + k / 100.0, dec_peak=800.0 + 100.0 * k))
            written += 1
    return written


def _hour_row(store, engine, hour):
    return store._con.execute(
        "SELECT * FROM counter_hour WHERE engine=? AND hour=?", (engine, hour)).fetchone()


def _cols(store, table):
    return [r[1] for r in store._con.execute(f"PRAGMA table_info({table})")]


def _row(store, table, where, params, name):
    r = _cols(store, table)
    row = store._con.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
    return dict(zip(r, row)) if row else {k: None for k in r}


# ---------------------------------------------------------------- ladder shape

def test_schema_is_created_on_boot(store):
    cols = _cols(store, "counter_hour")
    for c in ("real_prompt", "synth_prompt", "real_cost_hot", "synth_cost_hot",
              "busy_minutes", "real_rows", "synth_rows"):
        assert c in cols, f"ladder is missing {c}"
    assert "day" in _cols(store, "counter_day")


def test_rollup_is_idempotent(store):
    _seed(store, [40, 41, 42])
    before = tiers.rollup_all(store._con)
    again = tiers.rollup_all(store._con)
    assert before["hours"] == again["hours"]
    assert store._con.execute("SELECT COUNT(*) FROM counter_hour").fetchone()[0] == again["hours"]


def test_measured_and_synthetic_never_merge(store):
    """A seed/gap bucket is honest history but it is not measured traffic — the two must stay
    in separate columns or a seeding bug silently inflates every rate anchor."""
    now = int(time.time())
    base = (now - 45 * DAY) // HOUR * HOUR
    for k in range(4):
        store.save_counter_bucket("e1", base + k * 60, _bkt(100.0, synth=k % 2))
    tiers.rollup_hours(store._con)
    h = _row(store, "counter_hour", "engine='e1' AND hour=?", (base,), "counter_hour")
    direct = store._con.execute(
        "SELECT COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 THEN prompt END),0),"
        " COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=1 THEN prompt END),0),"
        " COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 THEN 1 ELSE 0 END),0),"
        " COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=1 THEN 1 ELSE 0 END),0)"
        " FROM counter_buckets WHERE engine='e1' AND ts>=? AND ts<?",
        (base, base + 4 * 60)).fetchone()
    assert h["real_prompt"] == direct[0] and h["synth_prompt"] == direct[1]
    assert h["real_rows"] == direct[2] and h["synth_rows"] == direct[3]


def test_peaks_are_extremes_not_averages(store):
    now = int(time.time())
    base = (now - 50 * DAY) // HOUR * HOUR
    for k, (lo, hi) in enumerate([(0.5, 500.0), (0.2, 1400.0), (0.9, 300.0)]):
        store.save_counter_bucket("e1", base + k * 60, _bkt(100.0, dec_min=lo, dec_peak=hi))
    tiers.rollup_hours(store._con)
    h = _row(store, "counter_hour", "engine='e1' AND hour=?", (base,), "counter_hour")
    assert h["dec_peak"] == 1400.0, "peak must be the MAX, an averaged peak under-reports capacity"
    assert h["dec_min"] == 0.2, "floor must be the MIN"


def test_ratio_is_rederived_from_numerator_and_denominator(store):
    """Averaging a percentage is wrong unless every sample has equal weight — so the ladder
    stores numerator and denominator and re-derives the ratio."""
    now = int(time.time())
    base = (now - 51 * DAY) // HOUR * HOUR
    # 1 accepted of 1 drafted (100%) then 1 of 9 (11.1%): naive mean = 55.5%, true = 20%
    store.save_counter_bucket("e1", base, _bkt(100.0, spec_sum=1.0, spec_n=1.0))
    store.save_counter_bucket("e1", base + 60, _bkt(100.0, spec_sum=1.0, spec_n=9.0))
    tiers.rollup_hours(store._con)
    h = _row(store, "counter_hour", "engine='e1' AND hour=?", (base,), "counter_hour")
    assert h["spec_sum"] == 2.0 and h["spec_n"] == 10.0
    # the ratio is NOT stored — a percentage at a coarse tier is a lie waiting to be averaged.
    # Deriving it from the summed numerator/denominator is the only correct reading.
    assert h["hour"] == base, "rollup did not produce the hour row"
    assert abs(h["spec_sum"] / h["spec_n"] - 0.2) < 1e-9, \
        "must be sum/sum, not the mean of the minute ratios"


def test_busy_minutes_is_measured(store):
    """Occupancy must survive aggregation: the hour keeps how many of its minutes produced a
    token, so 'hours a day this box worked' is still a measurement a year later."""
    now = int(time.time())
    base = (now - 52 * DAY) // HOUR * HOUR
    for k in range(60):
        p = 100.0 if k in (3, 7, 11, 40) else 0.0
        store.save_counter_bucket("e1", base + k * 60,
                                  _bkt(p, cost=None if p == 0 else 0.001))
    tiers.rollup_hours(store._con)
    h = _row(store, "counter_hour", "engine='e1' AND hour=?", (base,), "counter_hour")
    assert h["busy_minutes"] == 4, "only minutes that produced a token count as busy"


# ---------------------------------------------------------------- the gate

def test_prune_is_refused_when_aggregates_are_wrong(store, monkeypatch):
    """The load-bearing test: if the fold step never runs (fresh upgrade, a crash between the
    two statements, a bad deploy), the minutes must survive the prune attempt.

    Deleting counter_hour is not a valid way to stage this — the prune rolls the ladder up
    first, so it heals its own aggregates. Breaking the fold is the honest simulation.
    """
    _seed(store, [40, 41, 60, 90])
    first = store.bucket_row_count()
    assert first > 0
    monkeypatch.setattr(tiers, "rollup_hours", lambda con, from_ts=None: 0)
    rep = tiers.enforce_retention(store._con, minute_days=30, history_years=1)
    assert rep.get("blocked"), "refusing to prune must be the default when the fold is missing"
    assert store.bucket_row_count() == first, "a blocked prune must not delete anything"
    # and once the fold actually runs, the same prune is allowed
    monkeypatch.undo()
    rep2 = tiers.enforce_retention(store._con, minute_days=30, history_years=1)
    assert rep2.get("blocked") is None, rep2


def test_prune_happens_and_loses_nothing(tmp_path):
    # seeded with a wide horizon, then narrowed: this is what happens when an operator lowers
    # minute_bucket_days in Settings, and it must cost nothing
    store = _mk(tmp_path)
    _seed(store, [2, 5, 10, 40, 41, 60, 90, 400])
    pre = store.counter_totals("e1", None)
    assert pre["empty"] is False
    store.minute_days = 30.0
    rep = tiers.enforce_retention(store._con, minute_days=store.minute_days,
                                  history_years=store.history_years)
    assert rep.get("blocked") is None, rep
    assert rep["pruned_buckets"] > 0, "400 days of minutes should have been trimmed"
    post = store.counter_totals("e1", None)
    # tiered summation is not bitwise identical to summing the minutes directly: the hour rung
    # sums its own minutes, the day rung sums those sums. Tolerance is the float floor, not a
    # licence for drift — 1e-9 relative would have caught every real bug found in this ladder.
    for k in ("prompt", "cached", "gen", "reqs", "iters", "spec_sum"):
        assert post[k] == pytest.approx(pre[k], rel=1e-9, abs=1e-6), \
            f"{k} changed across a prune: {pre[k]} -> {post[k]}"
    for k in ("money_rows", "money_legacy_rows"):
        assert post[k] == pre[k], f"{k} changed across a prune: {pre[k]} -> {post[k]}"
    assert post["cost_hot"] == pytest.approx(pre["cost_hot"])
    assert post["cost_base"] == pytest.approx(pre["cost_base"])
    assert post["cost_nocache"] == pytest.approx(pre["cost_nocache"])
    assert post["dec_peak"] == pre["dec_peak"], "best-hour capacity figure must survive"
    assert post["dec_min"] == pre["dec_min"]
    # the whole point: minutes are gone, the ladder still answers
    hor = tiers.horizon_ts(time.time(), 30)
    assert store._con.execute("SELECT COUNT(*) FROM counter_buckets WHERE ts < ?",
                              (hor,)).fetchone()[0] == 0
    assert post["source"] == "buckets+counter_hour+counter_day", post["source"]


def test_seam_is_counted_exactly_once(store):
    """The double-count/gap regression: with an un-snapped boundary, minutes at the edge of the
    cut-off hour are owned by neither reader (or by both). Totals must equal a full-table SUM."""
    _seed(store, [2, 5, 31, 33])
    tiers.rollup_all(store._con)
    whole = store._con.execute(
        "SELECT COALESCE(SUM(prompt),0), COALESCE(SUM(gen),0), COALESCE(SUM(reqs),0),"
        " COALESCE(SUM(iters),0) FROM counter_buckets WHERE engine='e1'").fetchone()
    got = store.counter_totals("e1", None)
    assert (got["prompt"], got["gen"], got["reqs"], got["iters"]) == whole, \
        "readers must partition history, not overlap or leave a hole"


def test_minutes_kept_longer_than_hours_are_counted_once(store):
    """minute_bucket_days may outlast history_years (this fixture: 3650 d vs 1 y).

    The day leg then ended at the hourly cap while the minute leg started at the minute horizon,
    years below it, so every day in between was read from both the minutes and its day row:
    double the traffic the moment a day row existed.
    """
    noon = int(time.time()) // DAY * DAY + 12 * HOUR
    _seed(store, [400 + k / 24 for k in range(6)], per_hour=4, now=noon)
    whole = store._con.execute(
        "SELECT COALESCE(SUM(prompt),0) FROM counter_buckets WHERE engine='e1'").fetchone()[0]
    tiers.rollup_all(store._con)
    assert store._con.execute("SELECT COUNT(*) FROM counter_day").fetchone()[0] > 0
    assert store.counter_totals("e1", None)["prompt"] == pytest.approx(whole)


def test_retention_never_rebuilds_a_day_from_part_of_its_hours(store):
    """Same setting, write side: hours were trimmed at history_years while that day's minutes
    were still kept, so the next fold rebuilt the day containing the minute horizon from the
    minutes left above it and replaced the whole day with that fragment."""
    d1 = (int(time.time()) - 400 * DAY) // DAY * DAY
    for h in range(-24, 48):                           # three full days, d1 in the middle
        store.save_counter_bucket("e1", d1 + h * HOUR, _bkt(1000.0))
    tiers.rollup_all(store._con)
    t0 = d1 + 400 * DAY + 12 * HOUR                    # minute horizon at noon on d1
    for step in range(0, 19, 6):                       # ... then walking into the next day
        rep = tiers.enforce_retention(store._con, minute_days=400, history_years=1,
                                      now=t0 + step * HOUR)
        assert rep.get("blocked") is None, rep
    row = store._con.execute("SELECT real_rows, real_prompt FROM counter_day"
                             " WHERE engine='e1' AND day=?", (d1 // DAY,)).fetchone()
    assert row is not None and row[0] == 24, f"day row holds {row} of 24 minutes"
    assert row[1] == pytest.approx(24000.0)


def test_recent_only_window_reads_just_minutes(store):
    _seed(store, [2, 5, 90])
    tiers.rollup_all(store._con)
    since = int(time.time()) - 7 * DAY
    expected = store._con.execute(
        "SELECT COALESCE(SUM(prompt),0) FROM counter_buckets WHERE engine='e1' AND ts>=?",
        (since,)).fetchone()[0]
    assert store.counter_totals("e1", since)["prompt"] == expected


def test_legacy_bucket_without_costs_is_not_repriced(tmp_path):
    """Old rows have no $ columns. They must surface as legacy/unpriced, never as $0 earned."""
    store = _mk(tmp_path, minute_days=30)
    now = int(time.time())
    old = (now - 70 * DAY) // 60 * 60
    store.save_counter_bucket("e1", old, _bkt(5000.0))          # no cost fields
    new = (now - 2 * DAY) // 60 * 60
    store.save_counter_bucket("e1", new, _bkt(5000.0, cost=0.25))
    rep = tiers.enforce_retention(store._con, minute_days=store.minute_days,
                                  history_years=store.history_years)
    assert rep.get("blocked") is None, rep
    t = store.counter_totals("e1", None)
    assert t["money_rows"] == 1 and t["money_legacy_rows"] == 1
    assert t["cost_hot"] == pytest.approx(0.25), "unpriced history must not dilute the total"
    assert t["legacy_prompt"] == 5000.0


def test_daily_totals_survive_hour_pruning(tmp_path):
    """Days are the forever rung: prune past the hour horizon and the daily figures stay exact."""
    store = _mk(tmp_path)                                      # seeded wide, then narrowed
    _seed(store, [400, 401, 402], per_hour=3)
    since = int(time.time()) - 500 * DAY
    before = [(r[0], r[1]) for r in store.daily_bucket_totals("e1", since)]
    store.minute_days, store.history_years = 30.0, 0.5         # 6 months of hourly rows
    rep = tiers.enforce_retention(store._con, minute_days=store.minute_days,
                                  history_years=store.history_years)
    assert rep.get("blocked") is None, rep
    assert rep["pruned_hours"] > 0, "hourly rows older than history_years should have aged out"
    after = [(r[0], r[1]) for r in store.daily_bucket_totals("e1", since)]
    assert before and after
    assert after == before, "daily totals must not move when hourly rows age out"


def test_a_day_row_sums_every_hour_of_that_day(tmp_path):
    """Several busy hours in one UTC day must fold into one day row holding all of them, not
    just the last hour written. The test above seeds one hour per day, which can't tell."""
    store = _mk(tmp_path)
    noon = int(time.time()) // DAY * DAY + 12 * HOUR          # 6 hours back stay on one day
    _seed(store, [400 + k / 24 for k in range(6)], per_hour=4, now=noon)
    day = (noon - 400 * DAY) // DAY
    minutes = store._con.execute(
        "SELECT SUM(prompt), SUM(gen) FROM counter_buckets WHERE engine='e1' AND ts/86400=?",
        (day,)).fetchone()
    tiers.rollup_all(store._con)
    row = store._con.execute(
        "SELECT real_prompt + synth_prompt, real_gen + synth_gen, real_rows + synth_rows"
        " FROM counter_day WHERE engine='e1' AND day=?", (day,)).fetchone()
    assert row is not None, "the day was never folded"
    assert row[2] == 24, f"day row holds {row[2]} of 24 minutes"
    assert row[0] == pytest.approx(minutes[0]) and row[1] == pytest.approx(minutes[1])
    # and the figure must survive the hour rows ageing out, which is when the day row is all
    # that is left of that day
    since = noon - 500 * DAY
    before = [(r[0], r[1]) for r in store.daily_bucket_totals("e1", since)]
    store.minute_days, store.history_years = 30.0, 0.5
    rep = tiers.enforce_retention(store._con, minute_days=store.minute_days,
                                  history_years=store.history_years)
    assert rep.get("blocked") is None, rep
    assert rep["pruned_hours"] > 0
    assert [(r[0], r[1]) for r in store.daily_bucket_totals("e1", since)] == before
    # only this one day was seeded, so the all-time total is that day's minute sum
    assert store.counter_totals("e1", None)["prompt"] == pytest.approx(minutes[0])


def test_history_years_setting_bounds_the_hour_tier(store):
    _seed(store, [2, 100, 300, 900], per_hour=2)
    rep = tiers.enforce_retention(store._con, minute_days=30, history_years=1)
    assert rep.get("blocked") is None, rep
    cutoff = int(time.time() - 1 * 365.25 * DAY)
    assert store._con.execute("SELECT COUNT(*) FROM counter_hour WHERE hour*3600 < ?",
                              (cutoff,)).fetchone()[0] == 0, \
        "hourly rows older than history_years should have been trimmed once days proved equal"
    assert store._con.execute("SELECT COUNT(*) FROM counter_day").fetchone()[0] > 0, \
        "the daily rung must hold what the hourly rung gave up"


def test_a_reader_never_trusts_an_unbuilt_ladder(tmp_path):
    """The first read after an upgrade must not under-report.

    The reader answers rows below the horizon from the aggregate rungs. If nothing has folded
    them yet (fresh process, scheduler never ran), trusting an empty ladder silently drops every
    aged row out of the money totals — measured live on the operator's dashboard: the headline
    fell 2.3% (68.9 M prompt tokens) immediately after restarting with the ladder installed.
    """
    path = str(tmp_path / "unbuilt.db")
    s1 = Store(path, persist_seconds=3600, minute_days=3650, history_years=6)
    _seed(s1, [40, 60, 90], per_hour=4)
    whole = s1._con.execute(
        "SELECT COALESCE(SUM(prompt),0) FROM counter_buckets WHERE engine='e1'").fetchone()[0]
    assert whole > 0
    # the state a brand-new process sees: minutes exist, the ladder has never been built
    s1._con.execute("DELETE FROM counter_hour")
    s1._con.execute("DELETE FROM counter_day")
    s1._con.commit()
    s2 = Store(path, persist_seconds=3600, minute_days=30, history_years=6)
    assert s2._con.execute("SELECT COUNT(*) FROM counter_hour").fetchone()[0] == 0
    got = s2.counter_totals("e1", None)
    assert got["prompt"] == pytest.approx(whole, rel=1e-9), \
        "an unbuilt ladder must not silently subtract history from the money"
    assert s2._con.execute("SELECT COUNT(*) FROM counter_hour").fetchone()[0] > 0, \
        "the read should have healed the ladder itself"


def test_idle_box_goes_blank_rather_than_serving_the_ladder(tmp_path):
    """An idle box must not project money from an aggregate.

    The cards are documented to the operator as "the last active hour of real minute buckets".
    A fallback that answered from the hourly tier kept a $4,792/yr projection alive through a
    two-hour idle period (observed live), which reads as traffic that did not happen.
    """
    store = _mk(tmp_path, minute_days=30)
    now = int(time.time())
    busy = (now - 5 * 3600) // HOUR * HOUR          # a genuinely busy hour, still in minutes
    for k in range(50):
        store.save_counter_bucket("e1", busy + k * 60, _bkt(20000.0))
    idle = now // 60 * 60                            # the box is idle NOW: rows exist, no tokens
    for k in range(55):
        store.save_counter_bucket("e1", idle - k * 60, _bkt(0.0))
    got = store.last_active_hour("e1")
    assert got["empty"] is True, \
        f"idle box must report no active hour, got {got}"


def test_recent_hour_rows_do_not_freeze(tmp_path):
    """The fold used to be reachable only through the prune, so once nothing had aged out the
    hourly rows stopped updating and reported a partial hour forever."""
    store = _mk(tmp_path, minute_days=30)
    now = int(time.time())
    h0 = (now - 2 * 3600) // HOUR * HOUR
    for k in range(3):
        store.save_counter_bucket("e1", h0 + k * 60, _bkt(1000.0))
    # nothing older than the horizon exists, so the write path takes the cheap branch
    store.save_counter_bucket("e1", h0 + 3 * 60, _bkt(1000.0))
    store.save_counter_bucket("e1", h0 + 4 * 60, _bkt(1000.0))
    row = store._con.execute(
        "SELECT real_rows, real_prompt FROM counter_hour WHERE engine='e1' AND hour=?",
        (h0,)).fetchone()
    assert row is not None and row[0] == 5, f"hourly row froze at {row}"
    assert row[1] == pytest.approx(5000.0)


def test_long_pause_still_has_a_rate_to_project_from(tmp_path):
    """The case the fallback IS for: everything recent is gone from the minutes, and the last
    busy hour survives only as an aggregate — the headline must not vanish."""
    # seeded wide, then the window is narrowed — the realistic catch-up path. Writing old rows
    # one minute at a time under a narrow window is the pathological case; it is covered by
    # test_a_straggler_backfill_can_never_shrink_an_hour below.
    store = _mk(tmp_path)
    now = int(time.time())
    old = (now - 40 * DAY) // HOUR * HOUR
    for k in range(30):
        store.save_counter_bucket("e1", old + k * 60, _bkt(30000.0))
    store.minute_days = 30.0
    tiers.enforce_retention(store._con, minute_days=30, history_years=6)
    assert store._con.execute("SELECT COUNT(*) FROM counter_buckets WHERE ts < ?",
                              (tiers.horizon_ts(time.time(), 30),)).fetchone()[0] == 0
    got = store.last_active_hour("e1")
    assert got["empty"] is False and got["basis"] == "aged_out_hourly_tier"
    assert got["prompt"] > 0 and got["buckets"] == 30 and got["hour_ts"] == old


def test_a_straggler_backfill_can_never_shrink_an_hour(tmp_path):
    """An hour that has been folded and aged out must not be rewritten from a partial sample.

    A restore or late seed can land one old minute of an already-summarised hour. Folding the
    survivors alone would replace the rich aggregate with a fragment and silently lose the rest
    of that hour's tokens — the exact class of loss the ladder exists to prevent.
    """
    store = _mk(tmp_path)
    now = int(time.time())
    h0 = (now - 45 * DAY) // HOUR * HOUR
    for k in range(40):
        store.save_counter_bucket("e1", h0 + k * 60, _bkt(1000.0))
    store.minute_days = 30.0
    tiers.enforce_retention(store._con, minute_days=30, history_years=6)
    before = store._con.execute(
        "SELECT real_rows, real_prompt FROM counter_hour WHERE engine='e1' AND hour=?",
        (h0,)).fetchone()
    assert before[0] == 40 and before[1] == pytest.approx(40000.0), before
    # the straggler: one more minute of that hour arrives long after the hour was summarised
    store._con.execute("INSERT OR REPLACE INTO counter_buckets(engine, ts, prompt, cached, gen,"
                       " reqs, iters, dec_min, dec_peak, spec_sum, spec_n, synthetic)"
                       " VALUES ('e1', ?, 7000.0, 0, 0, 0, 0, NULL, NULL, 0, 0, 0)",
                       (h0 + 55 * 60,))
    store._con.commit()
    tiers.rollup_hours(store._con)
    after = store._con.execute(
        "SELECT real_rows, real_prompt FROM counter_hour WHERE engine='e1' AND hour=?",
        (h0,)).fetchone()
    assert after[0] >= before[0] and after[1] >= before[1], \
        f"aggregate shrank from a partial fold: {before} -> {after}"
    # and nothing of that hour may be deleted while its numbers disagree with the aggregate
    rep = tiers.enforce_retention(store._con, minute_days=30, history_years=6)
    assert store._con.execute("SELECT COUNT(*) FROM counter_buckets WHERE engine='e1' AND ts>=?"
                              " AND ts<?", (h0, h0 + HOUR)).fetchone()[0] >= 1


def test_a_busier_engine_never_blocks_another_engines_hour(tmp_path):
    """The never-shrink guard is per engine. Two engines sharing an hour are two aggregates.

    Comparing a light engine's 20 minutes against a busy engine's 60 in the same hour reads as
    a shrink, so the light engine's hour was never written: the gate then reported "no
    aggregate row" and blocked every prune, and once the hour aged out the light engine's
    traffic was gone from its totals.
    """
    store = _mk(tmp_path)
    now = int(time.time())
    h0 = (now - 45 * DAY) // HOUR * HOUR
    for k in range(60):
        store.save_counter_bucket("e2", h0 + k * 60, _bkt(1000.0))
    tiers.rollup_all(store._con)                       # e2's hour is folded first
    for k in range(20):
        store.save_counter_bucket("e1", h0 + k * 60, _bkt(500.0))
    pre = store.counter_totals("e1", None)["prompt"]
    tiers.rollup_all(store._con)
    row = store._con.execute(
        "SELECT real_rows, real_prompt FROM counter_hour WHERE engine='e1' AND hour=?",
        (h0,)).fetchone()
    assert row is not None and row[0] == 20, f"e1's hour was not folded: {row}"
    assert row[1] == pytest.approx(10000.0)
    store.minute_days = 30.0
    rep = tiers.enforce_retention(store._con, minute_days=30, history_years=6)
    assert rep.get("blocked") is None, rep
    assert store.counter_totals("e1", None)["prompt"] == pytest.approx(pre)


def test_a_strided_refresh_never_folds_a_partial_hour(tmp_path):
    """`from_ts` is a cost control, not a licence to summarise half an hour.

    The refresh folds the last few hours on every bucket write; with a raw timestamp cutoff
    inside a GROUP BY hour, the oldest hour in range was rebuilt from only its late minutes and
    published as the whole hour (observed: 53 rows for a 59-row hour).
    """
    store = _mk(tmp_path)
    now = int(time.time())
    h0 = (now - 3 * HOUR) // HOUR * HOUR          # the oldest hour the 3 h refresh touches
    for k in range(59):
        store.save_counter_bucket("e1", h0 + k * 60, _bkt(1000.0))
    tiers.rollup_hours(store._con, from_ts=now - 3 * HOUR)
    row = store._con.execute(
        "SELECT real_rows, real_prompt FROM counter_hour WHERE engine='e1' AND hour=?",
        (h0,)).fetchone()
    assert row is not None and row[0] == 59, f"half an hour was folded: {row}"
    assert row[1] == pytest.approx(59000.0)


def test_hour_totals_real_only_excludes_seed(store):
    now = int(time.time())
    base = (now - 60 * DAY) // HOUR * HOUR
    store.save_counter_bucket("e1", base, _bkt(100.0, synth=0))
    store.save_counter_bucket("e1", base + 60, _bkt(999.0, synth=1))
    tiers.rollup_hours(store._con)
    hor = (now - 30 * DAY) // HOUR * HOUR
    row = tiers.hour_totals(store._con, "e1", None, hor, real_only=True)
    assert row["prompt"] == 100.0, "a rate anchor contaminated by seed rows is a fantasy number"
    both = tiers.hour_totals(store._con, "e1", None, hor, real_only=False)
    assert both["prompt"] == 1099.0
