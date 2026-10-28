"""Issue #22: `synthetic` meant two different things, and one of them was real traffic.

A lifetime SEED (the engine's whole counter history, credited on first sight) and a GAP
close (the counter delta across a sampler outage) both set synthetic=1. Every headline
reads `real_only=True`, so a LIMCARS restart in the middle of real load deleted those
tokens from the money lines — measured on the live database before the fix: 5 gap buckets
holding 528,514 prompt tokens that no total would show. `synth_kind` separates them:
0 = measured minute, 1 = lifetime estimate, 2 = gap (measured, spanning >1 minute).

What must NOT change is the reason `synthetic` existed (audit M1/M2): a bucket spanning
four minutes is not one busy minute. It must never set a per-minute anchor, a decode
peak, a spec rate, the busy-minute count, or the active hour a savings projection is
priced from. And the ladder must answer exactly what the minutes would have answered —
those two invariants are the whole file.
"""
from __future__ import annotations

import sqlite3
import time

from app import tiers
from app.store import Store

DAY, HOUR = 86400, 3600
BASE = 1_800_000_000 // 60 * 60          # minute-aligned fixed ts, no clock in assertions


def _bkt(p, kind=0, cost=None, dec_min=0.1, dec_peak=900.0, spec_sum=4.0, spec_n=2.0):
    b = {"prompt": p, "cached": p * 0.5, "gen": p * 0.1, "reqs": 2, "iters": 7,
         "dec_min": dec_min, "dec_peak": dec_peak,
         "spec_sum": spec_sum, "spec_n": spec_n,
         "synthetic": 1 if kind else 0, "synth_kind": kind}
    if cost is not None:
        b.update({"cost_hot": cost, "cost_base": cost * 1.2, "cost_nocache": cost * 3,
                  "price_slug": "glm-5-3", "price_in": 1.0, "price_out": 2.0,
                  "price_cw": 1.25, "price_cr": 0.125, "price_crhot": 0.25,
                  "priced_at": time.time(), "price_src": "file"})
    return b


def _store(tmp_path, name="gap", minute_days=3650.0, history_years=1.0):
    return Store(str(tmp_path / f"{name}.db"), persist_seconds=3600,
                 minute_days=minute_days, history_years=history_years)


def _put(st, minute, p, kind=0, **kw):
    st.save_counter_bucket("e1", BASE + minute * 60, _bkt(p, kind=kind, **kw))


# ---------------------------------------------------------------- what counts


def test_served_tokens_count_and_estimates_do_not(tmp_path):
    """The headline this whole change exists for."""
    st = _store(tmp_path)
    _put(st, 0, 1000.0)                     # a normal measured minute
    _put(st, 1, 500.0, kind=2)              # served, but across a sampler outage
    _put(st, 2, 900.0, kind=1)              # engine-lifetime estimate
    assert st.counter_totals("e1", None, real_only=True)["prompt"] == 1500.0
    # the panel's all-time view keeps the seed on purpose: it is honest history,
    # it is just not traffic this box measured, so it never feeds a rate
    assert st.counter_totals("e1", None)["prompt"] == 2400.0


def test_a_gap_never_sets_a_rate_anchor(tmp_path):
    """Counted in the total, invisible to every per-minute figure (audit M1)."""
    st = _store(tmp_path)
    _put(st, 0, 1000.0, dec_min=0.5, dec_peak=900.0, spec_sum=4.0, spec_n=2.0)
    _put(st, 1, 500.0, kind=2, dec_min=0.001, dec_peak=99_999.0,
         spec_sum=900.0, spec_n=2.0)
    t = st.counter_totals("e1", None, real_only=True)
    assert t["prompt"] == 1500.0            # the tokens are counted...
    assert t["dec_peak"] == 900.0           # ...the absurd 4-minute peak is not the box's peak
    assert t["dec_min"] == 0.5
    assert t["spec"] == 2.0                 # 4/2 — not (4+900)/(2+2)


def test_a_gap_is_not_a_busy_minute(tmp_path):
    st = _store(tmp_path)
    _put(st, 0, 1000.0)
    _put(st, 1, 500.0, kind=2)
    assert [r[0] for r in st.minute_rows("e1")] == [BASE]


def test_a_gap_alone_is_not_an_active_hour(tmp_path):
    """No real minute yet -> no rate basis, so no fantasy $/yr projection (audit M2)."""
    st = _store(tmp_path)
    _put(st, 0, 5e6, kind=2)
    assert st.last_active_hour("e1").get("empty")


def test_the_daily_chart_shows_what_was_served(tmp_path):
    st = _store(tmp_path)
    _put(st, 0, 1000.0)
    _put(st, 1, 500.0, kind=2)
    _put(st, 2, 900.0, kind=1)
    assert sum(d[1] for d in st.daily_bucket_totals("e1", BASE - DAY)) == 1500.0


# ---------------------------------------------------------------- the fold


def test_the_fold_keeps_the_three_classes_apart(tmp_path):
    st = _store(tmp_path)
    _put(st, 0, 1000.0)
    _put(st, 1, 500.0, kind=2)
    _put(st, 2, 900.0, kind=1)
    tiers.rollup_all(st._con)
    row = st._con.execute(
        "SELECT real_prompt, gap_prompt, synth_prompt, real_rows, gap_rows,"
        " synth_rows, busy_minutes FROM counter_hour WHERE engine='e1' AND hour=?",
        (BASE // HOUR * HOUR,)).fetchone()
    assert row == (1000.0, 500.0, 900.0, 1, 1, 1, 1)
    # one busy minute, not three: busy_minutes is the occupancy figure and a
    # downtime span must not inflate how busy this box looks


def test_a_gap_counts_the_same_whether_minutes_or_the_ladder_answer(tmp_path):
    """The money-line invariant: a figure must not move when its rows age out.

    Same shape written twice — once where the minute leg answers, once where the
    hourly rung does — and each half must agree with itself and with the total.
    """
    st = _store(tmp_path)
    old = (int(time.time()) - 400 * DAY) // HOUR * HOUR
    for off, p, kind in ((0, 1000.0, 0), (60, 500.0, 2), (120, 900.0, 1)):
        st.save_counter_bucket("e1", old + off, _bkt(p, kind=kind, cost=p / 1e6))
        st.save_counter_bucket("e1", BASE + off, _bkt(p, kind=kind, cost=p / 1e6))
    tiers.rollup_all(st._con)
    both = st.counter_totals("e1", None, real_only=True)
    assert both["prompt"] == 3000.0                       # measured+gap, both ranges
    assert both["cost_hot"] == 0.003                       # and the money with them
    rung = tiers.hour_totals(st._con, "e1", None, old + HOUR, True)
    assert rung is not None and rung["prompt"] == 1500.0           # the aged half alone
    day = tiers.day_totals(st._con, "e1", None, (BASE // DAY) * DAY, True)
    assert day is not None and day["prompt"] == 1500.0             # and the day rung agrees


def test_minutes_may_still_be_pruned_when_an_hour_holds_gap_rows(tmp_path):
    """The data-safety half: the ladder must PROVE it reproduces gap rows.

    If gap tokens were dropped from the redundancy proof, buckets_are_redundant
    would refuse every prune forever (retention silently stops working); if the
    row COUNT were dropped, an hour could be judged redundant while its gap
    minutes were still unreproduced and the DELETE would eat real history.
    """
    st = _store(tmp_path)                    # huge window: the write path must not prune
    old = (int(time.time()) - 10 * DAY) // HOUR * HOUR
    st.save_counter_bucket("e1", old, _bkt(1000.0, kind=0, cost=0.01))
    st.save_counter_bucket("e1", old + 60, _bkt(500.0, kind=2, cost=0.005))
    before = st.counter_totals("e1", None, real_only=True)
    assert before["prompt"] == 1500.0
    out = tiers.enforce_retention(st._con, minute_days=0.0, history_years=1.0,
                                 engines=["e1"])
    assert out.get("blocked") is None, out.get("gate")
    assert out["pruned_buckets"] == 2
    # read back through a horizon that MATCHES the prune, or the deleted range belongs to
    # neither leg and the rows would merely vanish from the reader's view (test_tiers rule)
    after = _store(tmp_path, minute_days=0.0).counter_totals("e1", None, real_only=True)
    assert after["prompt"] == before["prompt"] == 1500.0
    assert after["cost_hot"] == before["cost_hot"] == 0.015


# ---------------------------------------------------------------- migration & precedence


def test_legacy_rows_classify_themselves_by_their_own_shape(tmp_path):
    """No heuristic: the seed path stores no money, the gap path always does.

    Rows are written here the way pre-#22 code wrote them — synthetic=1, kind left at
    the column default — so this is the actual upgrade path of a running install.
    """
    p = str(tmp_path / "legacy.db")
    Store(p, persist_seconds=3600, minute_days=3650.0, history_years=1.0)
    con = sqlite3.connect(p)
    con.executemany(
        "INSERT INTO counter_buckets(engine,ts,prompt,cached,gen,reqs,iters,"
        "dec_min,dec_peak,spec_sum,spec_n,synthetic,synth_kind,cost_hot,price_slug)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("e1", BASE, 400.0, 0, 0, 1, 1, None, None, None, 0, 1, 0, 0.004, "glm-5-3"),
         ("e1", BASE + 60, 600.0, 0, 0, 1, 1, None, None, None, 0, 1, 0, None, None)])
    con.commit()
    con.close()

    st = Store(p, persist_seconds=3600, minute_days=3650.0, history_years=1.0)
    kinds = dict(st._con.execute("SELECT ts, synth_kind FROM counter_buckets"))
    assert kinds[BASE] == 2 and kinds[BASE + 60] == 1
    assert st.counter_totals("e1", None, real_only=True)["prompt"] == 400.0


def test_the_migration_is_idempotent(tmp_path):
    """Startup runs it every boot; a second pass must change nothing."""
    p = str(tmp_path / "legacy.db")
    Store(p, persist_seconds=3600, minute_days=3650.0, history_years=1.0)
    con = sqlite3.connect(p)
    con.execute("INSERT INTO counter_buckets(engine,ts,prompt,synthetic,synth_kind,"
                "cost_hot,price_slug) VALUES('e1',?,400.0,1,0,0.004,'glm-5-3')", (BASE,))
    con.commit()
    con.close()
    for _ in range(3):
        st = Store(p, persist_seconds=3600, minute_days=3650.0, history_years=1.0)
        assert st._con.execute(
            "SELECT synth_kind FROM counter_buckets WHERE ts=?", (BASE,)).fetchone()[0] == 2


def test_a_seed_cannot_eat_a_measured_span_and_nothing_eats_a_measured_minute(tmp_path):
    """Precedence measured(0) > gap(2) > seed(1), in either arrival order."""
    st = _store(tmp_path)
    st.save_counter_bucket("e1", BASE, _bkt(1000.0, kind=2))
    st.save_counter_bucket("e1", BASE, _bkt(9e9, kind=1))          # seed loses
    assert st.counter_totals("e1", None, real_only=True)["prompt"] == 1000.0
    st.save_counter_bucket("e1", BASE, _bkt(1200.0))               # the fact wins
    assert st.counter_totals("e1", None, real_only=True)["prompt"] == 1200.0
    st.save_counter_bucket("e1", BASE, _bkt(5e6, kind=2))          # gap cannot demote it
    assert st.counter_totals("e1", None, real_only=True)["prompt"] == 1200.0


def test_a_synthetic_row_with_no_kind_stated_is_treated_as_the_weaker_class(tmp_path):
    """Legacy callers (and the importer) pass synthetic=1 only — that must mean estimate."""
    st = _store(tmp_path)
    st.save_counter_bucket("e1", BASE, _bkt(900.0, kind=1))
    b = _bkt(700.0)
    b.pop("synth_kind")
    b["synthetic"] = 1
    st.save_counter_bucket("e1", BASE + 60, b)
    assert st.counter_totals("e1", None, real_only=True).get("empty")   # nothing measured
    assert st.counter_totals("e1", None)["prompt"] == 1600.0
