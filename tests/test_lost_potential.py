"""Lost-potential bubble backend (best-hour anchored): sliding 60-minute peak
window (tokens + hot-tier $), utilisation vs recorded avg hour, projections,
synthetic exclusion, fresh-install (single bucket) still renders, biggest
peak-hour engine owns the headline."""
from app.counters import lost_potential
from app.store import Store

PRICE = {"input": 1.0, "cache_write": 1.0, "cache_read": 0.1,
         "cache_read_hot": 0.1, "output": 2.0, "slug": "test"}
BASE = 1_800_000_000 // 60 * 60          # minute-aligned fixed ts, no clock


def _b(p=0.0, c=0.0, g=0.0, **kw):
    d = {"prompt": p, "cached": c, "gen": g, "reqs": 1, "iters": 1,
         "dec_min": None, "dec_peak": None, "spec_sum": None, "spec_n": 0}
    d.update(kw)
    return d


def _m(st, eid, minute, p=0.0, c=0.0, g=0.0, **kw):
    st.save_counter_bucket(eid, BASE + minute * 60, _b(p=p, c=c, g=g, **kw))


def test_best_hour_window_and_pct(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    for m in range(30):                      # light first hour: 1M/min
        _m(st, "e1", m, p=1e6)
    for m in range(30, 90):                  # busy second hour: 3M/min
        _m(st, "e1", m, p=3e6)
    r = lost_potential(st, [{"id": "e1", "model": "M"}], PRICE)
    assert r["ok"] and r["model"] == "M"
    assert r["peak_hour_tokens"] == 180_000_000       # minutes 30..89
    assert r["total_tokens"] == 210_000_000
    assert r["avg_hour_tokens"] == round(210e6 / 1.5)  # 90 min = 1.5 h span
    assert abs(r["pct_usage"] - round(100.0 * (210e6 / 1.5) / 180e6, 1)) < 0.05
    assert r["proj_month_tokens"] == round(140e6 * 730)
    assert r["proj_year_tokens"] == round(140e6 * 8760)
    assert r["recorded_days"] == 1


def test_fresh_install_single_bucket(tmp_path):
    # "installed a couple of hours ago" must still show relevant data
    st = Store(str(tmp_path / "t.db"))
    _m(st, "e1", 0, p=4e6, g=1e6)
    r = lost_potential(st, [{"id": "e1", "model": "M"}], PRICE)
    assert r["ok"]
    assert r["peak_hour_tokens"] == 5_000_000
    # span floored at 1 h so a lone minute can't claim a 60x avg hour
    assert r["avg_hour_tokens"] == 5_000_000 and r["pct_usage"] == 100.0


def test_money_projections_from_best_hour(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    _m(st, "e1", 0, p=60e6)                  # one minute, $60 hot bill
    r = lost_potential(st, [{"id": "e1", "model": "M"}], PRICE)
    assert abs(r["best_hour_cost"] - 60.0) < 0.01
    assert abs(r["proj_day_cost"] - 1440.0) < 0.01
    assert abs(r["proj_month_cost"] - 60.0 * 730) < 0.01
    assert abs(r["proj_year_cost"] - 60.0 * 8760) < 0.01


def test_synthetic_excluded_from_window(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    _m(st, "e1", 0, p=1e6)
    _m(st, "e1", 1, p=99e6, synthetic=1)     # lifetime seed must not inflate
    _m(st, "e1", 2, p=1e6)
    r = lost_potential(st, [{"id": "e1", "model": "M"}], PRICE)
    assert r["peak_hour_tokens"] == 2_000_000
    assert r["total_tokens"] == 2_000_000


def test_biggest_peak_wins_and_no_price(tmp_path, monkeypatch):
    from app import pricing as pm
    monkeypatch.setattr(pm, "resolve", lambda m: None)   # no default fallback
    st = Store(str(tmp_path / "t.db"))
    _m(st, "small", 0, p=5e6)
    _m(st, "small", 1, p=5e6)
    _m(st, "huge", 0, p=50e6)
    r = lost_potential(st, [{"id": "small", "model": "S"},
                            {"id": "huge", "model": "H"}], {})
    assert r["model"] == "H" and r["peak_hour_tokens"] == 50_000_000
    assert r["best_hour_cost"] is None and r["proj_year_cost"] is None


def test_empty_store():
    st = Store(":memory:")
    assert lost_potential(st, [{"id": "x", "model": "X"}], PRICE) == {"ok": False}
    assert lost_potential(st, [], PRICE) == {"ok": False}
