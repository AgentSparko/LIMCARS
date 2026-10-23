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
    """The 'lost potential' bubble, anchored on the BEST RECORDED HOUR.

    The capability claim is what the engine actually sustained in its best
    60-minute window (tokens + hot-tier $), so a user who installed LIMCARS
    two hours ago still sees meaningful numbers on day one. Utilisation is
    the recorded average hour over the recorded span vs that best hour.
    Projections multiply the two honest anchors:
      * tokens  : recorded avg hour x 730/8760 h     ("where you're heading")
      * money   : BEST hour x 24/730/8760 h          ("where you could be")
    Sliding window is a two-pointer over minute indices — O(n) — and cost is
    summed from per-minute hot bills (the pricing function is linear, so the
    window sum equals hot_costs of the window totals)."""
    from . import pricing as pricing_mod

    best = None
    for eng in engines:
        eid, model = eng["id"], eng.get("model")
        rows = [(ts, p or 0.0, c or 0.0, g or 0.0)
                for ts, p, c, g in store.minute_rows(eid)
                if (p or 0) + (g or 0) > 0]
        if not rows:
            continue
        price = pricing_mod.effective_price(model, pricing_cfg) or {}
        price = price if (price.get("output") or price.get("cache_write")
                          or price.get("cache_read") or price.get("input")) else {}
        # minute-aligned: (minute_idx, tokens, hot_bill)
        mins = []
        for ts, p, c, g in rows:
            cost = pricing_mod.hot_costs(p, c, g, price)["hot"] if price else 0.0
            mins.append((ts // 60, p + g, cost))
        mins.sort()
        # sliding best 60-minute window (tokens and $ independently)
        peak_tok_w = peak_cost_w = cur_tok = cur_cost = 0.0
        start = 0
        for end in range(len(mins)):
            cur_tok += mins[end][1]
            cur_cost += mins[end][2]
            while mins[end][0] - mins[start][0] > 59:
                cur_tok -= mins[start][1]
                cur_cost -= mins[start][2]
                start += 1
            if cur_tok > peak_tok_w:
                peak_tok_w = cur_tok
            if cur_cost > peak_cost_w:
                peak_cost_w = cur_cost
        total_tok = sum(t for _, t, _ in mins)
        # "recorded period" = time the dash was ACTUALLY watching: sum of
        # contiguous minute-clusters (gaps > 2 days split clusters), so a
        # backfill from two separate eras doesn't dilute usage% with months
        # of nobody running the model, while a 90-minute fresh install still
        # measures a 90-minute span
        days = sorted({m // 1440 for m, _, _ in mins})
        span_hours = 0.0
        c0 = prev = mins[0][0]
        for m, _, _ in mins[1:] + [(None,) * 3]:
            if m is None or m - prev > 2 * 1440:
                span_hours += (prev - c0 + 1) / 60.0
                if m is not None:
                    c0 = prev = m
            else:
                prev = m
        span_hours = max(1.0, span_hours)
        avg_hour = total_tok / span_hours
        cand = {
            "engine": eid, "model": model or eid,
            "recorded_days": len(days),
            "period_days": round(span_hours / 24.0, 1),
            "peak_hour_tokens": round(peak_tok_w),
            "avg_hour_tokens": round(avg_hour),
            "pct_usage": round(100.0 * avg_hour / peak_tok_w, 1) if peak_tok_w else None,
            "total_tokens": round(total_tok),
        }
        if price:
            cand["best_hour_cost"] = round(peak_cost_w, 2)
            cand["best_day_cost"] = None
            # MEASURED calendar claims are only allowed when the period was
            # actually watched end-to-end: a "best month" over 21 recorded
            # days measures nothing (user audit). Below that bar, month/year
            # become pace-based PROJECTIONS from the recorded average day.
            def _cal(keyfn):
                cal: dict[int, float] = {}
                for m, _, c in mins:
                    k = keyfn(m * 60)
                    cal[k] = cal.get(k, 0.0) + c
                return round(max(cal.values()), 2) if cal else None

            def _month(ts):
                tm = time.gmtime(ts)
                return tm.tm_year * 12 + tm.tm_mon - 1

            def _year(ts):
                return time.gmtime(ts).tm_year
            rec_days = len({m // 1440 for m, _, _ in mins})
            period_days = max((mins[-1][0] // 1440) - (mins[0][0] // 1440) + 1, 1)
            # gates count days ACTUALLY seen, not the calendar span: a
            # backfill that leaves idle gaps must not unlock "best month"
            if period_days >= 2 and rec_days >= 2:      # >=2 real days watched
                cand["best_day_cost"] = _cal(lambda ts: ts // 86400)
            cand["best_month_cost"] = _cal(_month) if rec_days >= 31 else None
            cand["best_year_cost"] = _cal(_year) if rec_days >= 366 else None
            tot_cost = sum(c for _, _, c in mins)
            # pace anchored on recorded CALENDAR days, floored at 1, so a
            # two-hour-old install can't project its one busy hour across a
            # month (1440x inflation)
            avg_day_cost = tot_cost / max(rec_days, 1)   # pace on days actually used
            cand["proj_day_cost"] = round(avg_day_cost, 2)
            cand["proj_month_cost"] = round(avg_day_cost * 30.4, 2)
            cand["proj_year_cost"] = round(avg_day_cost * 365, 2)
            # "Your token usage rate makes you lose $X USD a year" (user
            # spec): best-hour ceiling (best hour x 8760) minus the year
            # annualized at the RECORDED pace. Same formula the old dash
            # showed (~$12K) — it is a CEILING minus actual, never "actual x
            # 8760".
            cand["loss_year_cost"] = round(peak_cost_w * 8760 - avg_day_cost * 365, 2)
        else:
            for _k in ("best_hour_cost", "best_day_cost", "best_month_cost",
                       "best_year_cost", "proj_day_cost",
                       "proj_month_cost", "proj_year_cost", "loss_year_cost"):
                cand[_k] = None
        # "This model is projected to do" = what it DEMONSTRATED it can do:
        # the best recorded hour scaled to a month/year (25M/h -> 600M/day,
        # matching the old dashboard's daily projection). The recorded-pace
        # numbers are kept for the tooltip.
        cand["proj_month_tokens"] = round(peak_tok_w * 24 * 30.4)
        cand["proj_year_tokens"] = round(peak_tok_w * 24 * 365)
        cand["pace_month_tokens"] = round(avg_hour * 730)
        cand["pace_year_tokens"] = round(avg_hour * 8760)
        if best is None or cand["peak_hour_tokens"] > best["peak_hour_tokens"]:
            best = cand
    if best is None:
        return {"ok": False}
    return {"ok": True, **best}
