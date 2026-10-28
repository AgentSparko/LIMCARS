"""Long-horizon history: hourly and daily aggregates, so history outlives the raw minutes.

Why this exists
---------------
Every money figure on the dashboard comes from `counter_buckets`, one row per busy minute per
engine. That table is what the operator wants to keep for years, and it is also the thing that
used to be deleted at 366 days — while the 1-second rings, which nothing reads past 24 h,
dominated the file. Keeping minutes for six years is wasteful (1,440 rows/engine/day, and the
UI never asks for minute granularity beyond a day) and keeping nothing is worse.

So history moves down a ladder as it ages:

    minute buckets   30 d      (granular: per-minute figures, sparklines, occupancy)
    counter_hour      history_years   (the money history: sums, peaks, basis counts)
    counter_day       forever  (~365 rows/engine/year — the long tail, chart-scale)

Correctness rules that make this safe rather than merely smaller
----------------------------------------------------------------
* Only additive measures are aggregated: token/request sums, cost sums, iteration sums,
  spec-accept sums. Averages are derived at read time from sums, never averaged again.
  `dec_peak` is a MAX and `dec_min` a MIN — never means.
* Percentages are never stored. Cache-hit etc. are re-derived from numerator and denominator.
* Synthetic seed/gap minutes are kept SEPARATE from measured minutes (`real_*` vs `synth_*`),
  because the two mean different things and every existing reader chooses between them
  (`counter_totals(real_only=...)`). Collapsing them would quietly change rate anchors.
* Money basis survives aggregation: each hour counts how many of its minutes carried a frozen
  cost and how many did not, plus the unpriced tokens, so a reader can still tell an
  as-earned figure from one re-priced at today's rate.
* Nothing is deleted until proven redundant: `enforce_retention()` rolls the ladder up first
  and compares the aggregates against the rows they claim to summarise. Any mismatch blocks
  the prune instead of trusting the rollup.
"""
from __future__ import annotations

import sqlite3
import time

DAY = 86400
HOUR = 3600

# additive measures, all-rows semantics split into measured vs synthetic
_MEASURES = ("prompt", "cached", "gen", "reqs", "iters", "cost_hot", "cost_base", "cost_nocache")

_REAL = "".join(f"  real_{m} REAL,\n" for m in _MEASURES)
_SYNTH = "".join(f"  synth_{m} REAL,\n" for m in _MEASURES[:5]) + "  synth_cost_hot REAL,\n"

DDL = f"""
CREATE TABLE IF NOT EXISTS counter_hour(
  engine TEXT, hour INTEGER,
{_REAL}  real_rows INTEGER DEFAULT 0,
{_SYNTH}  synth_rows INTEGER DEFAULT 0,
  dec_min REAL, dec_peak REAL, spec_sum REAL, spec_n INTEGER,
  priced_rows INTEGER DEFAULT 0, unpriced_rows INTEGER DEFAULT 0,
  unp_prompt REAL DEFAULT 0, unp_cached REAL DEFAULT 0, unp_gen REAL DEFAULT 0,
  busy_minutes INTEGER DEFAULT 0,
  price_slug TEXT,
  PRIMARY KEY(engine, hour));
CREATE INDEX IF NOT EXISTS ix_ch_hour ON counter_hour(hour);
CREATE TABLE IF NOT EXISTS counter_day(
  engine TEXT, day INTEGER,
{_REAL}  real_rows INTEGER DEFAULT 0,
{_SYNTH}  synth_rows INTEGER DEFAULT 0,
  dec_min REAL, dec_peak REAL, spec_sum REAL, spec_n INTEGER,
  priced_rows INTEGER DEFAULT 0, unpriced_rows INTEGER DEFAULT 0,
  unp_prompt REAL DEFAULT 0, unp_cached REAL DEFAULT 0, unp_gen REAL DEFAULT 0,
  busy_minutes INTEGER DEFAULT 0, busy_hours INTEGER DEFAULT 0,
  price_slug TEXT,
  PRIMARY KEY(engine, day));
CREATE INDEX IF NOT EXISTS ix_cd_day ON counter_day(day);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


# --------------------------------------------------------------------------- rollup

def _bucket_select() -> str:
    """One SELECT expression set that turns minutes into an aggregate row.

    measured vs synthetic is kept apart with CASE rather than a WHERE, so a single pass
    produces both and neither can be silently dropped.
    """
    def s(col: str, synth: bool = False) -> str:
        kind = "!=0" if synth else "=0"
        return (f"COALESCE(SUM(CASE WHEN COALESCE(synthetic,0){kind} "
                f"THEN COALESCE({col},0) END),0)")
    parts = []
    for m in _MEASURES:
        parts.append(s(m, synth=False))
    for m in ("prompt", "cached", "gen", "reqs", "iters"):
        parts.append(s(m, synth=True))
    parts.append(s("cost_hot", synth=True))
    parts += [
        "MIN(CASE WHEN COALESCE(synthetic,0)=0 THEN dec_min END)",
        "MAX(CASE WHEN COALESCE(synthetic,0)=0 THEN dec_peak END)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 THEN COALESCE(spec_sum,0) END),0)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 THEN COALESCE(spec_n,0) END),0)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 AND cost_hot IS NOT NULL "
        "THEN 1 ELSE 0 END),0)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 AND cost_hot IS NULL "
        "THEN 1 ELSE 0 END),0)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 AND cost_hot IS NULL "
        "THEN COALESCE(prompt,0) END),0)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 AND cost_hot IS NULL "
        "THEN COALESCE(cached,0) END),0)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 AND cost_hot IS NULL "
        "THEN COALESCE(gen,0) END),0)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 AND (COALESCE(prompt,0)"
        "+COALESCE(gen,0))>0 THEN 1 ELSE 0 END),0)",     # busy minutes: measured traffic only
        "MIN(price_slug)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)=0 THEN 1 ELSE 0 END),0)",
        "COALESCE(SUM(CASE WHEN COALESCE(synthetic,0)!=0 THEN 1 ELSE 0 END),0)",
    ]
    return ", ".join(parts)


# MUST match the emission order of _bucket_select() exactly: the INSERT names these columns and
# binds the SELECT's positional output, so a mismatch here silently writes the wrong figure into
# the wrong column (observed: dec_peak holding a row count, busy_minutes landing in price_slug).
_HOUR_COLS = (["engine", "hour"]
              + [f"real_{m}" for m in _MEASURES]
              + [f"synth_{m}" for m in ("prompt", "cached", "gen", "reqs", "iters")]
              + ["synth_cost_hot",
                 "dec_min", "dec_peak", "spec_sum", "spec_n",
                 "priced_rows", "unpriced_rows", "unp_prompt", "unp_cached", "unp_gen",
                 "busy_minutes", "price_slug",
                 "real_rows", "synth_rows"])


def rollup_hours(con: sqlite3.Connection, from_ts: int | None = None) -> int:
    """Fold minutes into completed hours. Idempotent: a row is recomputed, not added.

    The current, still-open hour is included on purpose — recomputing it from the minutes that
    exist so far is harmless because every reader only trusts hours strictly older than the
    bucket horizon, and it means a restart mid-hour never leaves a permanent hole.
    """
    q = (f"SELECT engine, {int(HOUR)} * (ts / {int(HOUR)}) AS hour, " + _bucket_select() +
         " FROM counter_buckets")
    params: tuple = ()
    if from_ts is not None:
        # Floor to an hour boundary. Rows are grouped BY hour, so a mid-hour cutoff would fold
        # an hour from only its late minutes and — because the fold is a REPLACE — publish a
        # partial hour as if it were the whole thing (observed: 53 of 59 minutes).
        from_ts = (int(from_ts) // HOUR) * HOUR
        q += " WHERE ts>=?"
        params = (int(from_ts),)
    q += " GROUP BY engine, ts / 3600"
    rows = con.execute(q, params).fetchall()
    if not rows:
        return 0
    # An aggregate must never SHRINK. If some minutes of an hour vanished since the last fold —
    # a straggler backfill for a period that already aged out, a restore that lands old rows
    # one at a time — folding the survivors would overwrite the richer row and quietly drop the
    # rest of that hour. Skip those hours instead: the redundancy gate then refuses to delete
    # what is left of them and says so, rather than losing it without a word.
    covered = {int(h): int(rn or 0) for h, rn in con.execute(
        "SELECT hour, COALESCE(real_rows, 0) + COALESCE(synth_rows, 0) FROM counter_hour")}
    kept = [r for r in rows if int(r[-2]) + int(r[-1]) >= covered.get(int(r[1]), -1)]
    con.executemany(
        f"INSERT OR REPLACE INTO counter_hour({','.join(_HOUR_COLS)}) "
        f"VALUES ({','.join('?' * len(_HOUR_COLS))})", kept)
    con.commit()
    return len(kept)


def rollup_days(con: sqlite3.Connection) -> int:
    """Fold hours into days. Cheap and total: hours are ~24 rows/engine/day, so recomputing
    every day from scratch is safer than trying to be incremental about it."""
    cols = ([f"real_{m}" for m in _MEASURES]
            + [f"synth_{m}" for m in ("prompt", "cached", "gen", "reqs", "iters", "cost_hot")])
    sel = ", ".join(f"COALESCE(SUM({c}),0)" for c in cols)
    q = (f"SELECT engine, hour / {int(DAY)}, {sel},"
         " COALESCE(SUM(real_rows),0), COALESCE(SUM(synth_rows),0),"
         " MIN(dec_min), MAX(dec_peak), COALESCE(SUM(spec_sum),0), COALESCE(SUM(spec_n),0),"
         " COALESCE(SUM(priced_rows),0), COALESCE(SUM(unpriced_rows),0),"
         " COALESCE(SUM(unp_prompt),0), COALESCE(SUM(unp_cached),0), COALESCE(SUM(unp_gen),0),"
         " COALESCE(SUM(busy_minutes),0),"
         " COALESCE(SUM(CASE WHEN busy_minutes>0 THEN 1 ELSE 0 END),0), MIN(price_slug)"
         f" FROM counter_hour GROUP BY engine, hour / {int(DAY)}")
    rows = con.execute(q).fetchall()
    if not rows:
        return 0
    names = (["engine", "day"] + cols +
             ["real_rows", "synth_rows", "dec_min", "dec_peak", "spec_sum", "spec_n",
              "priced_rows", "unpriced_rows", "unp_prompt", "unp_cached", "unp_gen",
              "busy_minutes", "busy_hours", "price_slug"])
    con.executemany(
        f"INSERT OR REPLACE INTO counter_day({','.join(names)}) "
        f"VALUES ({','.join('?' * len(names))})", rows)
    con.commit()
    return len(rows)


def rollup_all(con: sqlite3.Connection) -> dict:
    """Refresh the whole ladder from what is still in minutes, then derive days from hours."""
    oldest = con.execute("SELECT COALESCE(MIN(ts),0) FROM counter_buckets").fetchone()[0]
    hours = rollup_hours(con, int(oldest) if oldest else None)
    days = rollup_days(con)
    return {"hours": hours, "days": days}


# --------------------------------------------------------------------------- gate

# cost_base / cost_nocache are display extras summarised only for measured minutes, so they are
# deliberately excluded from the redundancy proof: comparing a column that exists on one side
# only would block every prune forever, which is worse than not checking.
_GATE = ("prompt", "cached", "gen", "reqs", "iters", "cost_hot")


def _minute_hours(con, engine: str, cap: int) -> dict:
    """Per-hour sums of the minutes that still exist below `cap`, keyed by hour."""
    sel = ", ".join(f"COALESCE(SUM({c}),0)" for c in _GATE)
    return {int(r[0]): tuple(float(v or 0) for v in r[1:])
            for r in con.execute(
                f"SELECT 3600*(ts/{HOUR}) AS h, {sel}, COUNT(*) FROM counter_buckets"
                " WHERE engine=? AND ts < ? GROUP BY h", (engine, int(cap)))}


def _aggregate_hours(con, engine: str, cap: int) -> dict:
    """The same hours as the ladder holds. All-rows = measured + synthetic columns."""
    sel = ", ".join(f"COALESCE(real_{c},0) + COALESCE(synth_{c},0)" for c in _GATE)
    return {int(r[0]): tuple(float(v or 0) for v in r[1:])
            for r in con.execute(
                f"SELECT hour, {sel}, COALESCE(real_rows,0) + COALESCE(synth_rows,0)"
                " FROM counter_hour WHERE engine=? AND hour < ?", (engine, int(cap)))}


def buckets_are_redundant(con, engines, cap: int) -> tuple[bool, dict]:
    """Would deleting minutes below `cap` lose anything the hours cannot reproduce?

    Compared HOUR BY HOUR, not as a total. A grand total looks equivalent but is not: after a
    previous prune has already deleted older minutes, the ladder legitimately holds more than
    the surviving rows, so a total comparison would refuse every future prune forever (and an
    operator would watch retention silently stop working). Per-hour equality is the claim that
    actually matters — every hour about to be deleted must be reproduced by its own aggregate
    row, measure by measure, including its row count.

    Aggregate hours with no minutes left are accepted without re-checking: they were proven when
    their minutes were deleted, and re-proving them is by definition impossible.
    """
    detail: dict = {}
    ok = True
    names = list(_GATE) + ["rows"]
    for e in engines:
        mins = _minute_hours(con, e, cap)
        aggs = _aggregate_hours(con, e, cap)
        bad: dict = {}
        for h, mv in mins.items():
            av = aggs.get(h)
            if av is None:
                bad[h] = {"error": "no aggregate row"}
                continue
            diff = {n: (mv[i], av[i]) for i, n in enumerate(names)
                    if abs(mv[i] - av[i]) > 1e-6}
            if diff:
                bad[h] = diff
        detail[e] = bad or "equal"
        if bad:
            ok = False
    return ok, detail


# --------------------------------------------------------------------------- retention

def enforce_retention(con, *, minute_days: float, history_years: float,
                      now: float | None = None, engines=None, dry: bool = False) -> dict:
    """Roll the ladder up, prove the aggregates, then apply retention.

    Order matters and is the whole point: rollup first, verify, delete second. The default
    `history_years` of 1 reproduces the old 366-day depth, so upgrading is never a downgrade.
    """
    now = now if now is not None else time.time()
    out = rollup_all(con)
    if engines is None:
        engines = [r[0] for r in con.execute("SELECT DISTINCT engine FROM counter_buckets")]
    bucket_cap = horizon_ts(now, minute_days)   # same hour-aligned seam every reader uses
    hour_cap = hour_horizon(now, history_years, minute_days)   # day-aligned seam the readers share
    # only judge redundancy for rows we are actually about to drop
    older = con.execute("SELECT COUNT(*) FROM counter_buckets WHERE ts < ?",
                        (bucket_cap,)).fetchone()[0]
    out["buckets_older_than_horizon"] = older
    if older:
        ok, detail = buckets_are_redundant(con, engines, bucket_cap)
        out["gate"] = detail
        if not ok:
            out["pruned_buckets"] = 0
            out["blocked"] = "aggregates do not reproduce the minutes — refusing to prune"
            return out
    if dry:
        out["pruned_buckets"] = 0
        out["pruned_hours"] = 0
        return out
    cur = con.execute("DELETE FROM counter_buckets WHERE ts < ?", (bucket_cap,))
    out["pruned_buckets"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else older
    curh = con.execute("DELETE FROM counter_hour WHERE hour < ?", (hour_cap,))
    out["pruned_hours"] = curh.rowcount if curh and curh.rowcount > 0 else 0
    con.commit()
    # days must not contradict the hours they were derived from after the cut
    rollup_days(con)
    out["days"] = con.execute("SELECT COUNT(*) FROM counter_day").fetchone()[0]
    return out


# --------------------------------------------------------------------------- readers

def hour_totals(con, engine: str, since_ts: int | None, before_ts: int | None,
                real_only: bool) -> dict | None:
    """Aggregate rows over the hourly tier, mirroring counter_totals semantics."""
    # cost_base / cost_nocache are display extras recorded for measured minutes only, so they
    # have no synth_ twin — asking for one raises "no such column" (and must never silently
    # become 0, which would understate the nocache comparison in the headline).
    _SYNTHLESS = ("cost_base", "cost_nocache")

    def side(m):
        if real_only or m in _SYNTHLESS:
            return f"real_{m}"
        return f"real_{m} + COALESCE(synth_{m},0)"
    sel = ", ".join(f"COALESCE(SUM({side(m)}),0)" for m in _MEASURES)
    q = (f"SELECT {sel}, MIN(dec_min), MAX(dec_peak), COALESCE(SUM(spec_sum),0),"
         " COALESCE(SUM(spec_n),0), MIN(hour), MAX(hour + 59),"
         " COALESCE(SUM(priced_rows),0), COALESCE(SUM(unpriced_rows),0),"
         " COALESCE(SUM(unp_prompt),0), COALESCE(SUM(unp_cached),0), COALESCE(SUM(unp_gen),0)"
         " FROM counter_hour WHERE engine=?")
    params: list = [engine]
    if since_ts is not None:
        q += " AND hour >= ?"                      # counter_hour.hour is EPOCH SECONDS
        params.append(int(since_ts))
    if before_ts is not None:
        q += " AND hour < ?"                        # strictly below the minute horizon
        params.append(int(before_ts))
    r = con.execute(q, tuple(params)).fetchone()
    if not r or r[12] is None:                      # MIN(hour) is NULL exactly when no rows
        return None
    (prompt, cached, gen, reqs, iters, cost_hot, cost_base, cost_nocache,
     dmin, dpeak, ssum, sn, first, last, npr, nlg, lp, lc, lg) = r
    if not (prompt or cached or gen or reqs or iters or npr or nlg):
        return None
    return {"empty": False, "prompt": prompt, "cached": cached, "gen": gen, "reqs": reqs,
            "iters": iters, "dec_min": dmin, "dec_peak": dpeak,
            "spec_sum": ssum, "spec_n": sn,
            "spec": (ssum / sn) if sn else None, "first_ts": first, "last_ts": last,
            "cost_hot": cost_hot, "cost_base": cost_base, "cost_nocache": cost_nocache,
            "money_rows": npr, "money_legacy_rows": nlg,
            "legacy_prompt": lp, "legacy_cached": lc, "legacy_gen": lg,
            "source": "counter_hour"}


def hour_horizon(now: float, history_years: float, minute_days: float) -> int:
    """Below this, only the daily rung answers. Day-aligned for the same reason `horizon_ts`
    is hour-aligned: a boundary that splits a day lets a day be counted twice or never.

    Never above the minute horizon. If minute_bucket_days outlasts history_years, the hourly
    rung is kept down to the day the minute horizon falls in: otherwise the day leg and the
    minute leg both answer the years in between, and retention trims the hours of a day whose
    minutes are still there, so the next fold rebuilds that day from a fragment of it.
    """
    cap = int(now - float(history_years) * 365.25 * DAY)
    return (min(cap, horizon_ts(now, minute_days)) // DAY) * DAY


def day_totals(con, engine: str, since_ts: int | None, before_ts: int | None,
               real_only: bool) -> dict | None:
    """The forever rung, shaped exactly like hour_totals() so merge() can simply add it in.

    Without this leg, history_years would be a deletion policy rather than a retention policy:
    everything older than the hourly window would exist in counter_day and be read by nobody.
    """
    _SYNTHLESS = ("cost_base", "cost_nocache")

    def side(m):
        if real_only or m in _SYNTHLESS:
            return f"real_{m}"
        return f"real_{m} + COALESCE(synth_{m},0)"
    sel = ", ".join(f"COALESCE(SUM({side(m)}),0)" for m in _MEASURES)
    q = (f"SELECT {sel}, MIN(dec_min), MAX(dec_peak), COALESCE(SUM(spec_sum),0),"
         " COALESCE(SUM(spec_n),0), MIN(day * 86400), MAX(day * 86400 + 86399),"
         " COALESCE(SUM(priced_rows),0), COALESCE(SUM(unpriced_rows),0),"
         " COALESCE(SUM(unp_prompt),0), COALESCE(SUM(unp_cached),0), COALESCE(SUM(unp_gen),0)"
         " FROM counter_day WHERE engine=?")
    params: list = [engine]
    if since_ts is not None:
        q += " AND day * 86400 >= ?"
        params.append(int(since_ts))
    if before_ts is not None:
        q += " AND day * 86400 < ?"                    # counter_day.day is an epoch-day index
        params.append(int(before_ts))
    r = con.execute(q, tuple(params)).fetchone()
    if not r or r[12] is None:                          # MIN(day) is NULL exactly when no rows
        return None
    (prompt, cached, gen, reqs, iters, cost_hot, cost_base, cost_nocache,
     dmin, dpeak, ssum, sn, first, last, npr, nlg, lp, lc, lg) = r
    if not (prompt or cached or gen or reqs or iters or npr or nlg):
        return None
    return {"empty": False, "prompt": prompt, "cached": cached, "gen": gen, "reqs": reqs,
            "iters": iters, "dec_min": dmin, "dec_peak": dpeak,
            "spec_sum": ssum, "spec_n": sn,
            "spec": (ssum / sn) if sn else None, "first_ts": first, "last_ts": last,
            "cost_hot": cost_hot, "cost_base": cost_base, "cost_nocache": cost_nocache,
            "money_rows": npr, "money_legacy_rows": nlg,
            "legacy_prompt": lp, "legacy_cached": lc, "legacy_gen": lg,
            "source": "counter_day"}


def merge(a: dict | None, b: dict | None = None, c: dict | None = None) -> dict:
    """Combine the minute, hourly and daily legs into one total, counting each range once.

    The three legs always cover disjoint ranges (see `horizon_ts` / `hour_horizon`), so additive
    measures simply sum. Peaks are MAX and floors MIN; `spec` is re-derived from summed
    numerator/denominator rather than averaged, which is the mistake this function exists to
    avoid. Labels are positional so `source` names exactly which rungs answered.
    """
    labels = ("buckets", "counter_hour", "counter_day")
    parts = [(lbl, p) for lbl, p in zip(labels, (a, b, c)) if p and not p.get("empty")]
    if not parts:
        return {"empty": True}
    if len(parts) == 1:
        out = dict(parts[0][1])
        out["source"] = parts[0][0]
        return out
    out: dict[str, object] = {"empty": False}
    for k in ("prompt", "cached", "gen", "reqs", "iters", "cost_hot", "cost_base",
              "cost_nocache", "money_rows", "money_legacy_rows", "legacy_prompt",
              "legacy_cached", "legacy_gen", "spec_sum", "spec_n"):
        out[k] = sum(float(p.get(k) or 0) for _, p in parts)
    out["dec_min"] = min([v for _, p in parts for v in (p.get("dec_min"),) if v is not None],
                         default=None)
    out["dec_peak"] = max([v for _, p in parts for v in (p.get("dec_peak"),) if v is not None],
                          default=None)
    ssum = sum(float(p.get("spec_sum") or 0) for _, p in parts)
    sn = sum(float(p.get("spec_n") or 0) for _, p in parts)
    out["spec_sum"], out["spec_n"] = ssum, sn
    out["spec"] = (ssum / sn) if sn else None
    starts = [v for _, p in parts for v in (p.get("first_ts"),) if v]
    ends = [v for _, p in parts for v in (p.get("last_ts"),) if v]
    out["first_ts"] = min(starts) if starts else None
    out["last_ts"] = max(ends) if ends else None
    out["source"] = "+".join(lbl for lbl, _ in parts)
    return out


def tier_minute_rows(con, engine: str, before_ts: int) -> list[tuple]:
    """(ts, prompt, cached, gen, busy_minutes) for minutes already aged out of buckets.

    Minute granularity no longer exists below the horizon, so the measured busy-minute count
    travels with the row instead of being guessed: an hour that saw traffic in 40 minutes says
    40, and occupancy statistics stay measured rather than assumed.
    """
    q = ("SELECT hour, real_prompt + COALESCE(synth_prompt,0),"
         " real_cached + COALESCE(synth_cached,0),"
         " real_gen + COALESCE(synth_gen,0), busy_minutes FROM counter_hour"
         " WHERE engine=? AND hour < ? AND COALESCE(real_rows,0) > 0 ORDER BY hour")
    return [(t, p, c, g, bm) for t, p, c, g, bm in
            con.execute(q, (engine, int(before_ts))).fetchall()]


def tier_daily(con, engine: str, since_ts: int, before_ts: int | None = None) -> list[tuple]:
    """Day rows covering the aged-out range, shaped exactly like daily_bucket_totals().

    `before_ts` is the other half of the seam: the day query must stop where the minute query
    starts, or a day that straddles the horizon would be counted from both sources. The horizon
    is hour-aligned and a day row is the whole day, so day rows stop at the start of the day
    the horizon falls in, and the hours of that day below the horizon come from counter_hour
    (hour_horizon keeps them at least that far back).
    """
    cols = ("real_prompt", "real_cached", "real_gen", "real_cost_hot", "real_cost_base",
            "real_cost_nocache", "priced_rows", "unpriced_rows", "unp_prompt", "unp_cached",
            "unp_gen")
    q = (f"SELECT day, {', '.join(cols)}"
         " FROM counter_day WHERE engine=? AND day * 86400 >= ?")
    params: list = [engine, int(since_ts)]
    if before_ts is None:
        return con.execute(q, tuple(params)).fetchall()
    day0 = (int(before_ts) // DAY) * DAY
    q += " AND day * 86400 < ?"
    params.append(day0)
    rows = con.execute(q, tuple(params)).fetchall()
    sums = ", ".join(f"COALESCE(SUM({c}),0)" for c in cols)
    rows += con.execute(
        f"SELECT hour / {int(DAY)}, {sums} FROM counter_hour"
        f" WHERE engine=? AND hour >= ? AND hour < ? GROUP BY hour / {int(DAY)}",
        (engine, max(int(since_ts), day0), int(before_ts))).fetchall()
    return rows


def horizon_ts(now: float, minute_days: float) -> int:
    """Everything strictly below this belongs to the aggregate tiers, above it to minutes.

    One boundary shared by every reader is what prevents the classic tier bug: a window that
    counts the same traffic twice, or drops it in the seam between the two sources. It is
    snapped to an exact hour because hours are the aggregate unit: an un-snapped boundary
    splits an hour, so its early minutes are counted by the hour query while its late minutes
    are still counted by the minute query (double count), or belong to neither (gap). Observed
    as the redundancy gate refusing every prune.
    """
    return (int(now - float(minute_days) * DAY) // HOUR) * HOUR
