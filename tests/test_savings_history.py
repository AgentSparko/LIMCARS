"""Daily money-saved chart backend: per-day per-model hot-tier cost from
persisted minute-buckets, day-boundary grouping, synthetic-bucket exclusion,
window clamping."""
import time

from app.counters import daily_savings
from app.store import Store

PRICE = {"input": 1.0, "cache_write": 1.0, "cache_read": 0.1,
         "cache_read_hot": 0.1, "output": 2.0, "slug": "test"}


def _b(p=0.0, c=0.0, g=0.0, **kw):
    d = {"prompt": p, "cached": c, "gen": g, "reqs": 1, "iters": 1,
         "dec_min": None, "dec_peak": None, "spec_sum": None, "spec_n": 0}
    d.update(kw)
    return d


def test_hot_costs_respects_the_cached_subset_contract():
    """`cached` is a SHARE of the prompt tokens, never extra tokens. A dialect
    whose cached counter is block-rounded (prefix_cache_hits_total on older
    vLLM builds) can report cached > prompt; billing must then stop at the
    prompt, or the dash charges read-rate for tokens nobody prompted."""
    from app.pricing import hot_costs
    pr = {"input": 1.0, "cache_write": 1.0, "cache_read": 0.1,
          "cache_read_hot": 0.1, "output": 2.0}
    honest = hot_costs(1_000_000, 400_000, 100_000, pr)
    assert abs(honest["hot"] - (0.6 * 1.0 + 0.4 * 0.1 + 0.1 * 2.0)) < 1e-9
    inflated = hot_costs(1_000_000, 1_400_000, 100_000, pr)
    assert abs(inflated["hot"] - (0.0 * 1.0 + 1.0 * 0.1 + 0.1 * 2.0)) < 1e-9
    assert inflated["hot"] <= honest["hot"] + 1e-9
    assert hot_costs(0, 0, 0, pr)["hot"] == 0.0
    assert hot_costs(None, None, None, pr)["hot"] == 0.0


def test_daily_savings_reports_measured_active_hours(tmp_path):
    """The occupancy stat next to the money line: hours a day the box was
    MEASURED producing tokens, averaged over the days that had traffic. It is
    read out of the minute rows — no assumed working day anywhere."""
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    today = now - now % 86400
    for m in range(90):                       # today: 90 active minutes
        st.save_counter_bucket("e1", today + m * 60, _b(p=1e6))
    for m in range(30):                       # yesterday: 30 active minutes
        st.save_counter_bucket("e1", today - 86400 + m * 60, _b(p=1e6))
    # idle minutes carry no rows, and synthetic seed rows must not count
    st.save_counter_bucket("e1", today + 500 * 60, _b(p=1e6, synthetic=1))

    r = daily_savings(st, [{"id": "e1", "model": "M"}], PRICE, 30)
    assert r["active_minutes"] == 120          # 90 + 30, synthetic excluded
    assert r["active_days"] == 2
    assert r["avg_active_hours_per_day"] == round(120 / 60.0 / 2, 2) == 1.0

    empty = daily_savings(Store(str(tmp_path / "e.db")),
                          [{"id": "e1", "model": "M"}], PRICE, 30)
    assert empty["avg_active_hours_per_day"] is None


def test_daily_savings_grouping_and_pricing(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    today = now - now % 86400
    st.save_counter_bucket("e1", today + 100, _b(1e6, 5e5, 2e5))
    # synthetic gap bucket same day: day chart measures LIVE usage only —
    # seed/gap buckets are excluded (audit F7: seed would be a lifetime bar)
    st.save_counter_bucket("e1", today + 200, _b(5e5, 0, 0, synthetic=1))
    # outside a 30-day window
    st.save_counter_bucket("e1", today - 40 * 86400 + 50, _b(1e6, 0, 0))

    r = daily_savings(st, [{"id": "e1", "model": "M"}], PRICE, 30)
    assert len(r["days"]) == 1
    e = r["days"][0]["engines"]["e1"]
    assert e["prompt"] == 1e6 and e["cached"] == 5e5 and e["gen"] == 2e5
    exp = (1e6 - 5e5) / 1e6 * 1.0 + 5e5 / 1e6 * 0.1 + 2e5 / 1e6 * 2.0
    assert abs(e["cost"] - round(exp, 4)) < 1e-6
    # A token total is prompt + gen: cached is a SUBSET of the prompt tokens
    # (vLLM counts each prompt token once whether served from the prefix cache
    # or recomputed), so adding it counted every cache-hit token twice — the
    # 1.9x the user spotted when he compared the two dashes.
    assert e["tokens"] == 1e6 + 2e5                       # prompt + gen
    assert e["cached"] == 5e5                              # kept as "of which"
    assert abs(r["totals"]["cost"] - round(exp, 4)) < 1e-6
    assert r["totals"]["tokens"] == e["tokens"]
    assert r["totals"]["cached"] == 5e5
    assert r["totals"]["tokens"] < e["prompt"] + e["cached"] + e["gen"]
    assert r["models"] == {"e1": "M"}

    # 365-day window picks up the old day too; totals are all-model sums
    r2 = daily_savings(st, [{"id": "e1", "model": "M"}], PRICE, 365)
    assert len(r2["days"]) == 2
    assert r2["totals"]["cost"] > r["totals"]["cost"]


def test_daily_savings_multi_model_merge(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    now = int(time.time())
    today = now - now % 86400
    st.save_counter_bucket("e1", today + 10, _b(1e6, 0, 0))
    st.save_counter_bucket("e2", today + 20, _b(0, 0, 1e6))
    r = daily_savings(st, [{"id": "e1", "model": "M1"},
                           {"id": "e2", "model": "M2"}], PRICE, 7)
    assert len(r["days"]) == 1
    eng = r["days"][0]["engines"]
    assert set(eng) == {"e1", "e2"}
    assert abs(eng["e1"]["cost"] - 1.0) < 1e-6      # 1M x $1/M write
    assert abs(eng["e2"]["cost"] - 2.0) < 1e-6      # 1M x $2/M output
    assert abs(r["totals"]["cost"] - 3.0) < 1e-6    # all models in total
    assert r["models"] == {"e1": "M1", "e2": "M2"}


def test_daily_savings_days_clamped(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    assert daily_savings(st, [], PRICE, 99999)["days_n"] == 400
    assert daily_savings(st, [], PRICE, -5)["days_n"] == 1
    assert daily_savings(st, [], PRICE, 7)["days_n"] == 7
