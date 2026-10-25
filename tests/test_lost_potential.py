"""Lost-potential bubble backend (best-hour anchored): sliding 60-minute peak
window (tokens + hot-tier $), utilisation vs recorded avg hour, projections,
synthetic exclusion, biggest peak-hour engine owns the headline.

Honesty gates (added after the user saw a 37k USD/year alert fire with no
engine connected and two minutes of data):
  * an engine must be ALIVE to contribute   -> ok:false reason=no_engine
  * >= 60 distinct recorded minutes          -> ok:false reason=recorded_lt_1h
  * the best hour must hold >= 15 active min -> a burst is not a demonstrated rate
Every test below therefore seeds a real hour of activity; the assertions are
the same maths they always were, just no longer anchored on a lone minute.
"""
from app.counters import lost_potential
from app.store import Store

PRICE = {"input": 1.0, "cache_write": 1.0, "cache_read": 0.1,
         "cache_read_hot": 0.1, "output": 2.0, "slug": "test"}
BASE = 1_800_000_000 // 60 * 60          # minute-aligned fixed ts, no clock
LIVE = [{"id": "e1", "model": "M", "alive": True}]


def _b(p=0.0, c=0.0, g=0.0, **kw):
    d = {"prompt": p, "cached": c, "gen": g, "reqs": 1, "iters": 1,
         "dec_min": None, "dec_peak": None, "spec_sum": None, "spec_n": 0}
    d.update(kw)
    return d


def _m(st, eid, minute, p=0.0, c=0.0, g=0.0, **kw):
    st.save_counter_bucket(eid, BASE + minute * 60, _b(p=p, c=c, g=g, **kw))


def _hour(st, eid, start=0, n=60, p=0.0, c=0.0, g=0.0, **kw):
    """Seed n consecutive active minutes — the minimum an honest hour is."""
    for m in range(start, start + n):
        _m(st, eid, m, p=p, c=c, g=g, **kw)


def test_best_hour_window_and_pct(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    for m in range(30):                      # light first hour: 1M/min
        _m(st, "e1", m, p=1e6)
    for m in range(30, 90):                  # busy second hour: 3M/min
        _m(st, "e1", m, p=3e6)
    r = lost_potential(st, LIVE, PRICE)
    assert r["ok"] and r["model"] == "M"
    assert r["peak_hour_tokens"] == 180_000_000       # minutes 30..89
    assert r["total_tokens"] == 210_000_000
    assert r["avg_hour_tokens"] == round(210e6 / 1.5)  # 90 min = 1.5 h span
    assert abs(r["pct_usage"] - round(100.0 * (210e6 / 1.5) / 180e6, 1)) < 0.05
    # capacity projections: the BEST hour scaled to a year at full tilt (what
    # the model showed it can do), not the recorded pace (user spec; pace stays
    # in pace_* fields).
    # capacity claim = the demonstrated hour at full tilt (28M/h -> 245B/year).
    # This is a CAPACITY figure and must not be scaled by a working day; the
    # duty cycle only ever touches the money numbers.
    assert r["proj_month_tokens"] == round(180e6 * 24 * 30.4)
    assert r["proj_year_tokens"] == round(180e6 * 24 * 365)
    assert r["ceiling_year_tokens"] == r["proj_year_tokens"]
    assert r["duty_year_tokens"] == round(180e6 * 365 * 1.5)   # measured 1.5 h/day
    assert r["pace_month_tokens"] == round(140e6 * 730)
    assert r["pace_year_tokens"] == round(140e6 * 8760)
    # LOST POTENTIAL, per the user's definition: CAPACITY (best recorded hour
    # priced x 8760 h, the machine never stopping) MINUS the money the dash
    # REGISTERED as saved (hot-tier $ actually recorded, annualised over the
    # days it saw traffic). Both sides ship in the payload; check by hand:
    #   capacity  = $180/h  x 8760 h        = $1,576,800
    #   registered= $210/day x 365 days     = $76,650
    #   on table  = $1,500,150
    assert r["duty_basis"] == "measured"
    assert r["duty_hours_observed_per_day"] == 1.5   # 90 active min / 1 day
    assert r["capacity_year_cost"] == round(180.0 * 8760, 2)
    assert r["ceiling_year_cost"] == r["capacity_year_cost"]
    assert r["saved_year_cost"] == round(210.0 * 365, 2)
    assert r["loss_year_cost"] == round(180.0 * 8760 - 210.0 * 365, 2)
    assert r["capacity_month_cost"] == round(180.0 * 24 * 30.4, 2)
    assert r["saved_month_cost"] == round(210.0 * 30.4, 2)
    assert r["loss_month_cost"] == round(180.0 * 24 * 30.4 - 210.0 * 30.4, 2)
    # gap over the recorded period = (best hour - avg hour) x hours watched
    assert abs(r["loss_period_cost"] - (180.0 - 140.0) * 1.5) < 1.0
    assert r["recorded_days"] == 1


def test_no_engine_connected_is_not_an_alert(tmp_path):
    """User report: the alert fired with no inference engine connected."""
    st = Store(str(tmp_path / "t.db"))
    _hour(st, "e1", p=1e6)                   # a real, honest hour of history
    r = lost_potential(st, [{"id": "e1", "model": "M", "alive": False}], PRICE)
    assert r["ok"] is False and r["reason"] == "no_engine"


def test_needs_an_hour_of_history_before_it_claims(tmp_path):
    """User report: it also fired with far less than an hour to calculate from."""
    st = Store(str(tmp_path / "t.db"))
    for m in range(20):                      # 20 minutes: not yet an hour
        _m(st, "e1", m, p=1e6)
    r = lost_potential(st, LIVE, PRICE)
    assert r["ok"] is False and r["reason"] == "recorded_lt_1h"
    # A single hour cannot show a utilisation gap (its best hour IS its average
    # hour), but it is a measured hour, so it may carry a capacity claim.
    for m in range(20, 60):
        _m(st, "e1", m, p=1e6)
    # a full hour is enough to price a capacity claim (user formula: capacity
    # minus registered saved), so the gate that protects the user is the 60
    # distinct minutes above, not a longer window
    r = lost_potential(st, LIVE, PRICE)
    assert r["ok"] is True
    for m in range(60, 120):                 # second, busier hour
        _m(st, "e1", m, p=3e6)
    r = lost_potential(st, LIVE, PRICE)
    assert r["ok"] is True
    assert r["peak_hour_tokens"] == 180_000_000     # the 3M/min hour


def test_a_single_flat_hour_is_still_a_capacity_claim(tmp_path):
    """Utilisation and capacity are different questions. One uniform hour is
    100% utilised (best hour IS the average hour), yet the headline is the
    user's formula — capacity (best hour x 8760 h) minus the money registered
    as saved — and an hour of running leaves a year of hours unrun."""
    st = Store(str(tmp_path / "t.db"))
    _hour(st, "e1", p=1e6)
    r = lost_potential(st, LIVE, PRICE)
    assert r["ok"] is True
    assert r["pct_usage"] == 100.0
    assert r["capacity_year_cost"] == round(60.0 * 8760, 2)
    assert r["saved_year_cost"] == round(60.0 * 365, 2)
    assert r["loss_year_cost"] == round(60.0 * 8760 - 60.0 * 365, 2)


def test_a_burst_minute_is_not_a_demonstrated_peak(tmp_path):
    """One busy minute must not define the 'best hour' it prices claims on."""
    st = Store(str(tmp_path / "t.db"))
    _m(st, "e1", 0, p=60e6)                  # $60 in a single minute
    for m in range(1, 60):                   # an ordinary hour around it
        _m(st, "e1", m, p=1e6)               # $1 each
    for m in range(60, 120):                 # a second, ordinary hour
        _m(st, "e1", m, p=1e6)
    r = lost_potential(st, LIVE, PRICE)
    assert r["ok"] is True
    # the honest hour is the burst + the 59 ordinary minutes, not a lone spike
    assert abs(r["best_hour_cost"] - 119.0) < 0.01
    # period is hours, not days: NOTHING longer than the hour may claim
    # "best" (user rule) — month/year are pace projections
    assert r["best_day_cost"] is None
    assert r["best_month_cost"] is None and r["best_year_cost"] is None
    # pace projections ride the recorded day ($179 billed over 1 recorded day),
    # never the best hour multiplied out
    assert abs(r["proj_day_cost"] - 179.0) < 0.01
    assert abs(r["proj_month_cost"] - 179.0 * 30.4) < 1
    assert abs(r["proj_year_cost"] - 179.0 * 365) < 1
    assert abs(r["loss_period_cost"] - (119.0 - 179.0 / 2.0) * 2.0) < 1.0


def test_isolated_spikes_are_rejected(tmp_path):
    """60 minutes spread over days, one per day, is not an hour of traffic."""
    st = Store(str(tmp_path / "t.db"))
    for d in range(60):
        _m(st, "e1", d * 1440, p=10e6)       # one busy minute each day
    r = lost_potential(st, LIVE, PRICE)
    assert r["ok"] is False and r["reason"] == "peak_too_thin"


def test_best_month_only_when_a_month_was_watchable(tmp_path):
    # 21 recorded days (20 active minutes each): no measured best-month/best-year
    st = Store(str(tmp_path / "t.db"))
    for d in range(21):
        _hour(st, "e1", start=d * 1440, n=20, p=5e5)   # 20 x $0.50 = $10/day
    r = lost_potential(st, LIVE, PRICE)
    assert r["ok"] is True
    assert r["best_day_cost"] == 10.0                # days were watched
    assert r["best_month_cost"] is None
    assert r["best_year_cost"] is None
    assert abs(r["proj_month_cost"] - (210 / 21) * 30.4) < 1
    assert abs(r["proj_year_cost"] - (210 / 21) * 365) < 1


def test_best_month_measured_after_31_days(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    for d in range(46):                      # 46-day span crosses months
        _hour(st, "e1", start=d * 1440, n=20,
              p=5e5 if d != 40 else 2.5e6)   # $10/day, $50 on day 40
    r = lost_potential(st, LIVE, PRICE)
    # with >=31 days watched the month claim is allowed, and the richer
    # month (contains the $50 day) must win
    assert r["ok"] is True
    assert r["best_month_cost"] is not None
    assert r["best_month_cost"] >= 50.0
    assert r["best_year_cost"] is None       # <366 days: still projected
    assert r["proj_year_cost"] is not None


def test_synthetic_excluded_from_window(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    for m in range(60):                      # two hours with a real contrast,
        _m(st, "e1", m, p=1e6)               # so the bubble has something to say
    for m in range(60, 120):
        _m(st, "e1", m, p=3e6)
    _m(st, "e1", 70, p=99e6, synthetic=1)    # lifetime seed must not inflate
    r = lost_potential(st, LIVE, PRICE)
    assert r["ok"] is True
    assert r["peak_hour_tokens"] == 180_000_000   # the 3M/min hour, seed ignored
    assert r["total_tokens"] == 240_000_000


def test_biggest_peak_wins_and_no_price(tmp_path, monkeypatch):
    from app import pricing as pm
    monkeypatch.setattr(pm, "resolve", lambda m: None)   # no default fallback
    st = Store(str(tmp_path / "t.db"))
    _hour(st, "small", p=1e6)                # 60M tokens in its best hour
    _hour(st, "huge", p=5e6)                 # 300M tokens in its best hour
    r = lost_potential(st, [{"id": "small", "model": "S", "alive": True},
                            {"id": "huge", "model": "H", "alive": True}], {})
    assert r["model"] == "H" and r["peak_hour_tokens"] == 300_000_000
    assert r["best_hour_cost"] is None and r["best_year_cost"] is None


def test_synthetic_seed_never_eats_a_real_minute(tmp_path):
    """Regression: the lifetime seed used to INSERT OR REPLACE over the real
    bucket of that minute, and synthetic rows are filtered on read — so each
    restart quietly destroyed one minute of recorded traffic."""
    st = Store(str(tmp_path / "t.db"))
    _m(st, "e1", 30, p=1e6)                       # a real minute of traffic
    _m(st, "e1", 30, p=99e9, synthetic=1)         # a restart seed lands on it
    assert st.minute_rows("e1") == [(BASE + 30 * 60, 1e6, 0.0, 0.0)]
    _m(st, "e2", 30, p=99e9, synthetic=1)         # seed first, then real data
    _m(st, "e2", 30, p=7e6)
    assert st.minute_rows("e2") == [(BASE + 30 * 60, 7e6, 0.0, 0.0)]


def test_projections_carry_the_duty_cycle_assumption(tmp_path):
    """User: the headline is capacity minus registered money saved, so it must
    not depend on anyone's schedule; the supplementary duty figures do, and they
    default to the occupancy the dash MEASURED (active minutes / recorded days),
    never to an invented working day."""
    st = Store(str(tmp_path / "t.db"))
    for m in range(60):
        _m(st, "e1", m, p=1e6)
    for m in range(60, 120):
        _m(st, "e1", m, p=3e6)                 # best hour = 180M tokens / $180
    r8 = lost_potential(st, LIVE, PRICE, duty_hours=8)
    r24 = lost_potential(st, LIVE, PRICE, duty_hours=24)
    assert r8["ok"] and r8["duty_hours_per_day"] == 8
    assert r8["duty_year_tokens"] == round(180e6 * 365 * 8)
    assert r8["duty_year_cost"] == round(180.0 * 365 * 8, 2)
    assert r24["duty_year_tokens"] == round(180e6 * 365 * 24)
    assert r8["duty_basis"] == "configured"
    # with no override, the basis is what the dash measured, not a guess:
    # 120 active minutes on 1 recorded day = 2 h/day of real traffic
    r0 = lost_potential(st, LIVE, PRICE)
    assert r0["duty_basis"] == "measured"
    assert r0["duty_hours_per_day"] == r0["duty_hours_observed_per_day"] == 2.0
    # the capacity claim does not move with the assumed working day
    assert r8["proj_year_tokens"] == r24["proj_year_tokens"]
    # the ceiling does not move with the duty cycle — it IS the 24/7 number
    assert r8["ceiling_year_tokens"] == r24["ceiling_year_tokens"]
    assert r8["ceiling_year_cost"] == round(180.0 * 8760, 2)
    # the headline is the user's own formula — CAPACITY minus REGISTERED saved —
    # so it does not move when someone pins a duty cycle; only the supplementary
    # duty_* figures do
    assert r8["loss_year_cost"] == r24["loss_year_cost"] == r0["loss_year_cost"]
    #   capacity   = $180/h x 8760 h      = $1,576,800
    #   registered = $240/day x 365 days  = $87,600   (60 min x $1 + 60 min x $3)
    assert r8["saved_year_cost"] == round(240.0 * 365, 2)
    assert r8["loss_year_cost"] == round(180.0 * 8760 - 240.0 * 365, 2)
    assert r8["loss_week_cost"] > 0
    # a nonsense duty cycle is clamped into a sane range, not obeyed
    assert lost_potential(st, LIVE, PRICE, duty_hours=9999)["duty_hours_per_day"] == 24
    assert lost_potential(st, LIVE, PRICE, duty_hours=0)["duty_hours_per_day"] == 0.25
    junk: float = "junk"          # type: ignore[assignment]
    # junk is ignored rather than trusted: back to the measured basis
    assert lost_potential(st, LIVE, PRICE, duty_hours=junk)["duty_hours_per_day"] == 2.0


def test_empty_store():
    st = Store(":memory:")
    assert lost_potential(st, [{"id": "x", "model": "X", "alive": True}],
                          PRICE)["ok"] is False
    assert lost_potential(st, [], PRICE)["ok"] is False
