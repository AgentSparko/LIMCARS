"""Windowed Token counters report — old LIMCARS /v27/dash/counters parity.

Reads the 1-minute persisted counter buckets (366 d retention) and returns
token totals for 1h/1d/7d/1mo/1y/all plus the money-saved scenarios the old
dash rendered: hot cache (prefill@write + hits@read-hot + gen@output),
base cache, and no-API-cache (everything as input). $/hr and forward
projections always derive from the most recent active hour, identical on
every tab — old-dash rule so raising concurrency can never LOWER the rate.
"""
from __future__ import annotations

import time

from . import pricing as pricing_mod

WINDOWS = {"1h": 3600, "1d": 86400, "7d": 7 * 86400,
           "1mo": 30 * 86400, "1y": 365 * 86400, "all": None}


def report(store, engine: str, window: str, model: str | None,
           override: dict | None = None) -> dict:
    if window not in WINDOWS:
        window = "all"
    secs = WINDOWS[window]
    now = time.time()
    since = int(now - secs) if secs else None
    w = store.counter_totals(engine, since)
    life = store.counter_totals(engine, None)
    h1 = store.counter_totals(engine, int(now - 3600))
    if w.get("empty"):
        return {"empty": True, "window": window}
    price = pricing_mod.effective_price(model, override) or {}
    p_in = price.get("input") or 0.0
    p_out = price.get("output") or 0.0
    p_w = price.get("cache_write") or p_in
    p_r = price.get("cache_read") or 0.0
    p_rhot = price.get("cache_read_hot") or p_r

    def pack(t):
        if t.get("empty"):
            return None
        c = pricing_mod.hot_costs(t["prompt"], t["cached"], t["gen"], price)
        uncached = max(0.0, t["prompt"] - t["cached"])
        return {**t, "uncached": uncached,
                "cache_hit": round(100 * t["cached"] / t["prompt"], 1) if t["prompt"] else None,
                "per_batch": round(uncached / t["reqs"]) if t["reqs"] else None,
                "cost_hot": c["hot"], "cost_base": c["base"], "cost_nocache": c["nocache"]}

    out = {"empty": False, "window": window, "totals": pack(w), "all_time": pack(life),
           "price": {"input": p_in, "output": p_out, "cache_write": p_w,
                     "cache_read": p_r, "cache_read_hot": p_rhot,
                     "name": price.get("display_name"), "slug": price.get("slug"),
                     "fetched_at": price.get("fetched_at")},
           "tracking_since": life.get("first_ts"), "observed_ms": (
               (life["last_ts"] - life["first_ts"]) * 1000
               if life.get("first_ts") else 0)}
    # rate basis: most recent ACTIVE hour (old-dash rule) — the newest 60-min
    # span that actually has buckets, hot-tier pricing; lifetime/tracked only
    # as fallback. SAME basis as the savings bubbles (shared pricing helpers).
    ah = store.last_active_hour(engine)
    basis = "last active hour"
    if not ah.get("empty"):
        per_hour = pricing_mod.hot_costs(
            ah["prompt"], ah["cached"], ah["gen"], price)["hot"]
    else:
        basis = "lifetime ÷ tracked"
        lt = pack(life)
        per_hour = (lt["cost_hot"] / max(0.05, (life["last_ts"] - life["first_ts"]) / 3600.0)
                    if lt and life.get("first_ts") else 0)
    out["per_hour"] = per_hour
    out["rate_basis"] = basis
    out["per_day"] = per_hour * 24
    out["project"] = ([["1 day", per_hour * 24], ["1 month", per_hour * 730],
                       ["1 year", per_hour * 8760]] if per_hour > 0 else [])
    out["extrapolated"] = bool(secs and w.get("first_ts") and
                               (now - w["first_ts"]) < secs * 0.95)
    # ---- savings-bubble sparks ------------------------------------------
    # "day" bubble: trailing 60-min hot API cost ($/hr) per sample, from the
    # persisted 1-minute buckets (6 h window). "month"/"year" bubbles: the
    # history of the bubble's OWN last-active-hour projection (user: the
    # independent 3d/7d hot-cost windows "do not show the charts calculated
    # after the last hour rate" — they contradicted the displayed $ and went
    # sparse before the bucket data era). Each past sample = what the
    # projection read at that moment.
    sparks: dict[str, list] = {}
    try:
        rows = store.counter_bucket_rows(engine, 7 * 86400)
    except Exception:  # noqa: BLE001 — sparks are decoration, never fail report
        rows = []
    if rows:
        import bisect
        ts = [r[0] for r in rows]
        pp, pcl, pg = [0.0], [0.0], [0.0]
        for _, p_, c_, g_ in rows:
            pp.append(pp[-1] + p_)
            pcl.append(pcl[-1] + c_)
            pg.append(pg[-1] + g_)

        def _wsum(pre, lo, hi):
            return pre[bisect.bisect_left(ts, hi)] - pre[bisect.bisect_left(ts, lo)]

        for key, span, step in (("day", 6 * 3600, 180.0),):
            pts: list = []
            t, end = int(now), int(now) - span
            while t >= end:
                p_ = _wsum(pp, t - 3600, t)
                c_ = _wsum(pcl, t - 3600, t)
                g_ = _wsum(pg, t - 3600, t)
                if p_ or g_:
                    pts.append([t, round(
                        pricing_mod.hot_costs(p_, c_, g_, price)["hot"], 4)])
                t -= step
            # loop walked now->past (newest first); all charts draw
            # oldest-left, so hand the client ascending points
            pts.sort(key=lambda x: x[0])
            sparks[key] = pts

    # month/year bubbles: their chart is the history of the bubble's OWN
    # last-active-hour based projection (user: the independent 3d/7d hot-cost
    # windows "do not show the charts calculated after the last hour rate" —
    # they contradicted the displayed $ and went sparse before the bucket
    # data era). Each past sample = what the projection read at that moment.
    for key, series_key, span, stride in (("month", "savings_month", 6 * 3600, 60),
                                          ("year", "savings_year", 24 * 3600, 120)):
        try:
            # SQL-side stride + json_extract: full 2 Hz history fetchall +
            # 32k json.loads per poll under the store lock was too heavy
            # (audit F4); ~1 pt/min is all a 200 px sparkline can show
            vals = store.series_tail(engine, series_key, span, stride)
        except Exception:  # noqa: BLE001 — decoration, never fail the report
            vals = []
        step = max(1, len(vals) // 200)
        sparks[key] = [[t, v] for t, v in vals[::step]]

    out["sparks"] = sparks
    return out


def daily_savings(store, engines: list[dict], pricing_cfg, days: int) -> dict:
    """Per-day money saved PER MODEL from real persisted usage (counter
    buckets, hot-tier pricing — same pricing helpers as every other $ on the
    dash). feeds the daily money-saved chart (1w/1m/3m/1y switch)."""
    from . import pricing as pricing_mod
    days = max(1, min(400, int(days)))
    # UTC-midnight aligned so the server window == the client window: an
    # unaligned start leaks a partial ghost day into totals (audit F2)
    since = (int(time.time()) // 86400 - days + 1) * 86400
    out_days: dict[int, dict] = {}
    models: dict[str, str] = {}
    tot_cost = tot_tok = 0.0
    for eng in engines:
        eid, model = eng["id"], eng.get("model")
        price = pricing_mod.effective_price(model, pricing_cfg)
        # same guard as the savings bubbles: a price dict of all-zeros is
        # truthy but meaningless — skip it, and skip legend entries for
        # engines that can never render a bar (audit F6)
        if not price or not (price.get("output") or price.get("cache_write")
                             or price.get("cache_read") or price.get("input")):
            continue
        models[eid] = model or eid
        for day, p_, c_, g_ in store.daily_bucket_totals(eid, since):
            cost = pricing_mod.hot_costs(p_, c_, g_, price)["hot"]
            d = out_days.setdefault(day, {})
            d[eid] = {"cost": round(cost, 4), "prompt": p_, "cached": c_,
                      "gen": g_, "tokens": (p_ or 0) + (c_ or 0) + (g_ or 0)}
            tot_cost += cost
            tot_tok += (p_ or 0) + (c_ or 0) + (g_ or 0)
    return {"days": [{"day": d, "engines": e} for d, e in sorted(out_days.items())],
            "models": models, "days_n": days,
            "totals": {"cost": round(tot_cost, 4), "tokens": tot_tok}}


def lost_potential(store, engines: list[dict], pricing_cfg) -> dict:
    """The 'lost potential' bubble: the best day / calendar-month / calendar-year
    this engine ever recorded (non-synthetic buckets only) against the averages
    over the RECORDED span — tokens and $ — and what % of its own demonstrated
    capability the history actually uses. The engine with the biggest peak day
    owns the headline: it is the one leaving the most on the table.
    Averages divide by calendar months/years the record SPANS (not the number
    that saw traffic), so a box idle for six months carries them like a slow
    six months, which is the whole point of the bubble."""
    from . import pricing as pricing_mod

    def month_index(utc_day: int) -> int:
        tm = time.gmtime(utc_day * 86400)
        return tm.tm_year * 12 + tm.tm_mon - 1

    def year_of(utc_day: int) -> int:
        return time.gmtime(utc_day * 86400).tm_year

    best = None
    for eng in engines:
        eid, model = eng["id"], eng.get("model")
        rows = [(d, p, c, g) for d, p, c, g in store.daily_bucket_totals(eid, 0)]
        rows = [(d, p, c, g) for d, p, c, g in rows if (p or 0) + (g or 0) > 0]
        if len(rows) < 2:
            continue          # one day is not a history
        price = pricing_mod.effective_price(model, pricing_cfg) or {}
        price = price if (price.get("output") or price.get("cache_write")
                          or price.get("cache_read") or price.get("input")) else {}
        day = {}
        for d, p_, c_, g_ in rows:
            tok = (p_ or 0) + (g_ or 0)
            # $ saved == hot-tier bill for that traffic (local engine paid
            # $0; same basis as every other $ figure on the dash)
            cost = (pricing_mod.hot_costs(p_, c_, g_, price)["hot"]
                    if price else None)
            day[d] = (tok, cost)
        keys = sorted(day)
        span_days = keys[-1] - keys[0] + 1
        tot_tok = sum(t for t, _ in day.values())
        has_cost = any(c is not None for _, c in day.values())
        tot_cost = sum(c for _, c in day.values() if c) if has_cost else None

        def agg(keyfn, n_span, pick):
            buckets: dict[int, float] = {}
            for d in keys:
                k = keyfn(d)
                buckets[k] = buckets.get(k, 0.0) + pick(day[d])
            tot = sum(buckets.values())
            peak = max(buckets.values()) if buckets else 0.0
            avg = tot / max(1, n_span)
            return peak, avg, (round(100.0 * avg / peak, 1) if peak > 0 else None)

        months = month_index(keys[-1]) - month_index(keys[0]) + 1
        years = year_of(keys[-1]) - year_of(keys[0]) + 1
        tok_pick = lambda v: v[0]
        cost_pick = lambda v: v[1] or 0.0
        peak_d = max(t for t, _ in day.values())
        avg_d = tot_tok / span_days
        cand = {
            "engine": eid, "model": model or eid,
            "recorded_days": len(keys), "span_days": span_days,
            "months_spanned": months, "years_spanned": years,
            "peak_day_tokens": round(peak_d),
            "avg_day_tokens": round(avg_d),
            "pct_day": round(100.0 * avg_d / peak_d, 1) if peak_d else None,
        }
        cand["peak_month_tokens"], cand["avg_month_tokens"], cand["pct_month"] = \
            agg(month_index, months, tok_pick)
        cand["peak_year_tokens"], cand["avg_year_tokens"], cand["pct_year"] = \
            agg(year_of, years, tok_pick)
        if has_cost:
            cand["peak_day_cost"] = round(max(c for _, c in day.values() if c is not None), 2)
            (cand["peak_month_cost"], cand["avg_month_cost"], cand["pct_month_cost"],
             cand["peak_year_cost"], cand["avg_year_cost"], cand["pct_year_cost"]) = (
                agg(month_index, months, cost_pick) + agg(year_of, years, cost_pick))
        else:
            cand.update({"peak_day_cost": None,
                         "peak_month_cost": None, "avg_month_cost": None,
                         "pct_month_cost": None, "peak_year_cost": None,
                         "avg_year_cost": None, "pct_year_cost": None})
        if best is None or cand["peak_day_tokens"] > best["peak_day_tokens"]:
            best = cand
    if best is None:
        return {"ok": False}
    return {"ok": True, **best}
