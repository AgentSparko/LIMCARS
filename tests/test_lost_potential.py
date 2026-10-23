"""Lost-potential bubble backend: peak day/calendar-month/calendar-year vs
recorded-span averages (tokens + hot-tier $), synthetic bucket exclusion,
single-day engines skipped, biggest-peak-day engine owns the headline.
Days are relative to now (store enforces 366-day retention); expected month/
year grouping is computed with the same gmtime math, never hardcoded, so the
test can't flake across calendar boundaries."""
import time

from app.counters import lost_potential
from app.store import Store

PRICE = {"input": 1.0, "cache_write": 1.0, "cache_read": 0.1,
         "cache_read_hot": 0.1, "output": 2.0, "slug": "test"}

NOW_DAY = int(time.time()) // 86400
D_LIGHT = NOW_DAY - 40        # ~40 days ago
D_PEAK = NOW_DAY              # today


def _mi(day):
    tm = time.gmtime(day * 86400)
    return tm.tm_year * 12 + tm.tm_mon - 1


def _yr(day):
    return time.gmtime(day * 86400).tm_year


def _b(p=0.0, c=0.0, g=0.0, **kw):
    d = {"prompt": p, "cached": c, "gen": g, "reqs": 1, "iters": 1,
         "dec_min": None, "dec_peak": None, "spec_sum": None, "spec_n": 0}
    d.update(kw)
    return d


def _put(st, eid, day, off=60, **bkw):
    st.save_counter_bucket(eid, day * 86400 + off, _b(**bkw))


def test_peak_average_and_pct(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    _put(st, "e1", D_LIGHT, p=1e6, g=1e5)             # light day: 1.1M tokens
    _put(st, "e1", D_PEAK, p=9e6, c=4e6, g=1e6)       # peak day:  10M tokens
    # synthetic seed the same day must not inflate the peak (audit F7 parity);
    # distinct ts or INSERT OR REPLACE would clobber the real peak bucket
    _put(st, "e1", D_PEAK, off=120, p=5e7, g=0, synthetic=1)
    r = lost_potential(st, [{"id": "e1", "model": "M"}], PRICE)
    assert r["ok"] and r["model"] == "M"
    span = D_PEAK - D_LIGHT + 1
    assert r["recorded_days"] == 2 and r["span_days"] == span
    assert r["peak_day_tokens"] == 10_000_000
    # avg is over the SPAN (incl. idle days) — idle time counts against use
    avg = (1.1e6 + 10e6) / span
    assert r["avg_day_tokens"] == round(avg)
    assert abs(r["pct_day"] - round(100.0 * avg / 10e6, 1)) < 0.05
    months = _mi(D_PEAK) - _mi(D_LIGHT) + 1
    years = _yr(D_PEAK) - _yr(D_LIGHT) + 1
    assert r["months_spanned"] == months and r["years_spanned"] == years
    if months == 2:      # the two days in different calendar months
        assert r["peak_month_tokens"] == 10_000_000
        assert r["avg_month_tokens"] == (1.1e6 + 10e6) / 2
    if years == 1:       # one year bucket sees everything -> 100 % used
        assert r["peak_year_tokens"] == 11_100_000
        assert r["pct_year"] == 100.0


def test_money_is_hot_tier_bill(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    _put(st, "e1", D_LIGHT, p=1e6, g=0)               # $1.00 day
    _put(st, "e1", D_PEAK, p=1e6, g=1e6)               # $3.00 day
    r = lost_potential(st, [{"id": "e1", "model": "M"}], PRICE)
    assert abs(r["peak_day_cost"] - 3.0) < 0.01
    months = _mi(D_PEAK) - _mi(D_LIGHT) + 1
    if months == 2:
        assert abs(r["peak_month_cost"] - 3.0) < 0.01
        assert abs(r["avg_month_cost"] - 2.0) < 0.01
    if _yr(D_PEAK) == _yr(D_LIGHT):
        assert abs(r["pct_year_cost"] - 100.0) < 0.05


def test_no_price_yields_null_costs(tmp_path, monkeypatch):
    from app import pricing as pm
    # resolve() has a 'default' price fallback; force a genuine no-price model
    monkeypatch.setattr(pm, "resolve", lambda m: None)
    st = Store(str(tmp_path / "t.db"))
    _put(st, "e1", D_LIGHT, p=1e6, g=0)
    _put(st, "e1", D_PEAK, p=2e6, g=0)
    r = lost_potential(st, [{"id": "e1", "model": "UNKNOWN-MODEL"}], {})
    assert r["ok"] and r["peak_day_tokens"] == 2_000_000
    assert r["peak_day_cost"] is None and r["peak_month_cost"] is None
    assert r["peak_year_cost"] is None


def test_single_day_engine_skipped_and_biggest_peak_wins(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    _put(st, "small", D_LIGHT, p=5e5, g=0)
    _put(st, "small", D_PEAK, p=5e5, g=0)              # 2 days, 0.5M peak
    _put(st, "big", D_PEAK, p=5e7, g=0)                # single day: no history
    r = lost_potential(st, [{"id": "small", "model": "S"},
                            {"id": "big", "model": "B"}], PRICE)
    assert r["ok"] and r["model"] == "S"                # 'big' talked too little
    # a second real-history engine with a bigger peak takes the headline
    _put(st, "huge", D_LIGHT, p=4e6, g=0)
    _put(st, "huge", D_PEAK, p=4e7, g=0)
    r2 = lost_potential(st, [{"id": "small", "model": "S"},
                             {"id": "big", "model": "B"},
                             {"id": "huge", "model": "H"}], PRICE)
    assert r2["model"] == "H" and r2["peak_day_tokens"] == 40_000_000


def test_empty_store():
    st = Store(":memory:")
    assert lost_potential(st, [{"id": "x", "model": "X"}], PRICE) == {"ok": False}
    assert lost_potential(st, [], PRICE) == {"ok": False}
