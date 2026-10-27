#!/usr/bin/env python3
"""Rehearse a user's upgrade before it happens.

Takes an existing database — the kind a user who installed weeks ago has: 48 h of raw
second-resolution rings, minute buckets, no aggregate ladder, no stats table — copies it, boots the
NEW code against the copy, and asserts the one thing that must never move: every number the
dashboard shows.

The baseline is produced by the OLD code (a separate checkout of what users run, e.g. a worktree of
origin/main) in a subprocess, so it is what users actually saw rather than what the new code
remembers. Comparison is a deep diff of the full payloads the HTTP handlers return — not a few
hand-picked fields, which is the kind of check that passes vacuously when a shape is guessed wrong.

    python3 scripts/limcars_upgrade_rehearsal.py --db <user.db> --old-code <checkout-of-main>

The input database is copied first; nothing of the user's is modified. Non-zero exit on any change.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)   # the checkout this script lives in

# Fields that legitimately differ between two reads seconds apart on a live box. Everything else
# is a promise: if it moves across the upgrade, the upgrade changed what the user sees.
VOLATILE = re.compile(
    r"(^|_)(at|ts|now|age|uptime|generated|updated|refreshed|observed|elapsed|duration"
    r"|alive|connected|reason|window|since|until|step|n_after|samples|pid|port)$"
    r"|^(window|reason|alive|extrapolated|empty)$"
    # sparkline points are [[t, v], ...] on a grid anchored at `now`, so the timestamps of the
    # points legitimately shift between two reads; the VALUES are what must agree, and they are
    # compared per-minute by the coverage checks below instead.
    r"|\[\d+\]\[0\]$", re.I)

DUMPER = r"""
import json, sys
code, db, out = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, code)
from app.store import Store
from app import counters

PRICING = {"input": 0.15, "output": 0.47, "cache_write": 0.2, "cache_read": 0.016}
store = Store(db)                     # the real constructor: migrations, harvest, schema
engines = [r[0] for r in store._con.execute(
    "SELECT DISTINCT engine FROM counter_buckets ORDER BY engine")]
for r in store._con.execute("SELECT DISTINCT engine FROM engine_samples ORDER BY engine"):
    if r[0] not in engines:
        engines.append(r[0])
d = {"engines": engines, "money": {}, "cache_life": {}, "sparks": {}, "counts": {}}
for e in engines:
    d["money"][e] = counters.report(store, e, "all", e, override=PRICING)
    rows = store.history("engine_samples", e, 7 * 86400)
    d["cache_life"][e] = [[r["t"], r.get("cache_hit_pct")] for r in rows
                          if r.get("cache_hit_pct") is not None]
    d["sparks"][e] = {k: store.series_tail(e, k, span, stride) for k, span, stride in
                      (("savings_month", 6 * 3600, 60), ("savings_year", 24 * 3600, 120))}
d["daily"] = counters.daily_savings(store, [{"id": e, "model": e} for e in engines], PRICING, 30)
d["lost"] = counters.lost_potential(store, [{"id": e, "model": e, "alive": True}
                                            for e in engines], PRICING)
for t in ("engine_samples", "hw_samples", "counter_buckets", "events", "counter_raw",
          "counter_hour", "counter_day", "spark_stats"):
    try:
        d["counts"][t] = store._con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    except Exception:
        d["counts"][t] = None
store._con.close()
json.dump(d, open(out, "w", encoding="utf-8"))
"""


def _run_dump(code: str, db: str, out: str) -> dict:
    script = os.path.join(code, ".rehearsal_dumper.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(DUMPER)
    env = dict(os.environ, LIMCARS_DATA=tempfile.mkdtemp(prefix="limcars-rehearsal-"))
    try:
        p = subprocess.run([sys.executable, script, code, db, out], check=True, env=env,
                           cwd=code, capture_output=True, text=True, timeout=3600)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"dump failed with code at {code}:\n{exc.stderr[-4000:]}")
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass
    return json.load(open(out, encoding="utf-8"))


def boot_new(db: str) -> str:
    """Everything a `git pull` + restart does: migrations, the sparkline harvest, the ladder fold,
    the thin, the tidy pass — in the order Maintenance.run() does them, timed, because the honest
    answer to "what happens to my dashboard when I update" includes how long it is unavailable."""
    code = f'''
import json, sys, time
sys.path.insert(0, {REPO!r})
from app.store import Store
from app import maintenance

t = {{}}
def mark(name, fn):
    t0 = time.time(); r = fn(); t[name] = round(time.time() - t0, 2); return r

store = mark("store_init", lambda: Store({db!r}))       # ALTERs, ladder DDL, sparkline harvest
tidy = maintenance.Maintenance({{}}, store)
mark("fold", tidy._rollup)                                # minutes -> hour/day before any drop
mark("tidy", lambda: tidy.run_once(force=True))           # first pass (nothing freed yet)
thinned = mark("thin", lambda: (store.thin_rings() or {{}}).get("thinned", 0))
if thinned > 5000:                                        # exactly what Maintenance.run() does:
    mark("tidy2", lambda: tidy.run_once(force=True))      # a big thin earns the VACUUM
rows = store._con.execute("SELECT COUNT(*) FROM engine_samples").fetchone()[0]
store._con.close()
print(json.dumps({{"seconds": t, "engine_rows": rows}}))
'''
    env = dict(os.environ, LIMCARS_DATA=tempfile.mkdtemp(prefix="limcars-rehearsal-"))
    p = subprocess.run([sys.executable, "-c", code], env=env, cwd=REPO,
                       capture_output=True, text=True, timeout=3600)
    if p.returncode != 0:
        raise SystemExit(f"boot of the new code failed on the copied database:\n{p.stderr[-4000:]}")
    return p.stdout.strip()


def flatten(obj, prefix=""):
    """Path -> leaf value, so a shape I did not predict still gets compared instead of skipped."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def compare(old: dict, new: dict, size_before: int = 0,
            size_after: int = 0) -> tuple[list[str], list[str]]:
    fails: list[str] = []
    ok: list[str] = []

    if sorted(old["engines"]) != sorted(new["engines"]):
        fails.append(f"engine list changed: {old['engines']} -> {new['engines']}")

    # every payload the dashboard renders, compared leaf by leaf
    for section in ("money", "daily", "lost"):
        a, b = flatten(old.get(section)), flatten(new.get(section))
        only_a = sorted(set(a) - set(b))          # a field the dashboard used to get and no longer
        only_b = sorted(set(b) - set(a))          # a new field: an addition, not a regression
        if only_a:
            fails.append(f"{section}: {len(only_a)} fields DISAPPEARED, e.g. {only_a[:8]}")
        if only_b:
            ok.append(f"{section}: {len(only_b)} fields added (new features), none removed")
        drift = 0
        for k in sorted(set(a) & set(b)):
            va, vb = a[k], b[k]
            leaf = k.split(".")[-1].split("[")[0]
            if VOLATILE.search(leaf) or VOLATILE.search(k):
                continue        # whole path is checked too: point timestamps end in [i][0]
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)) \
                    and not isinstance(va, bool) and not isinstance(vb, bool):
                if abs(float(va) - float(vb)) > max(1e-6, abs(float(va)) * 1e-9):
                    fails.append(f"{section}.{k}: {va} -> {vb}")
            elif va != vb:
                fails.append(f"{section}.{k}: {va!r} -> {vb!r}")
            else:
                drift += 1
        ok.append(f"{section}: {drift} values identical")

    # the sparklines: coverage of the timeline is the promise, resolution may change
    for e in old["engines"]:
        for label, o_list, n_list in (
                ("cache-life", old["cache_life"].get(e) or [], new["cache_life"].get(e) or []),
                *[(f"spark {k}", old["sparks"].get(e, {}).get(k) or [],
                   new["sparks"].get(e, {}).get(k) or []) for k in ("savings_month", "savings_year")]):
            om = sorted({int(t) // 60 for t, _ in o_list})
            nm = sorted({int(t) // 60 for t, _ in n_list})
            if not om:
                ok.append(f"{label}[{e}]: nothing in baseline")
                continue
            if not nm:
                fails.append(f"{label}[{e}]: series went empty")
                continue
            # The endpoint downsamples to ~800 points, so which minutes appear is the sampler's
            # choice, not a promise. What IS a promise: the same span is reachable and there are
            # no holes a chart would show as a flat gap.
            span_old, span_new = om[-1] - om[0], nm[-1] - nm[0]
            if span_new < span_old * 0.9:
                fails.append(f"{label}[{e}]: span collapsed {span_old} min -> {span_new} min")
            gaps = [(b - a) for a, b in zip(nm, nm[1:])] or [0]
            worst = max(gaps)
            if worst > max(5, 4 * (sorted(gaps)[len(gaps) // 2] or 1)):
                fails.append(f"{label}[{e}]: hole of {worst} min in the series "
                             f"(median gap {sorted(gaps)[len(gaps)//2]})")
            else:
                ok.append(f"{label}[{e}]: {len(nm)} min, span {span_new} min, worst gap {worst} min")
            ov = {int(t): v for t, v in o_list}
            nv = {int(t): v for t, v in n_list}
            changed = [t for t in ov if t in nv and ov[t] != nv[t]]
            if changed:
                t0 = changed[0]
                fails.append(f"{label}[{e}]: {len(changed)} shared points changed value, "
                             f"e.g. t={t0} {ov[t0]} -> {nv[t0]}")

    # the storage facts the change is supposed to produce
    c_old, c_new = old["counts"], new["counts"]
    for t in ("counter_buckets", "events", "counter_raw"):
        if c_old.get(t) is not None and c_new.get(t) is not None and c_new[t] < c_old[t]:
            fails.append(f"{t} lost rows during the upgrade: {c_old[t]} -> {c_new[t]}")
        else:
            ok.append(f"{t} intact ({c_new.get(t)})")
    if not c_new.get("spark_stats"):
        fails.append("spark_stats empty: the lifetime sparklines have nothing to draw from")
    else:
        ok.append(f"spark_stats harvested ({c_new['spark_stats']} rows)")
    for t in ("counter_hour", "counter_day"):
        if not c_new.get(t):
            fails.append(f"{t} empty after the fold: history depth is at risk")
        else:
            ok.append(f"{t} built ({c_new[t]} rows)")
    if size_after > size_before * 0.5:
        fails.append(f"file did not shrink after the upgrade: {size_before/1e6:.0f} MB -> "
                     f"{size_after/1e6:.0f} MB (a big thin must earn its VACUUM)")
    else:
        ok.append(f"file {size_before/1e6:.0f} MB -> {size_after/1e6:.0f} MB")
    if (c_new.get("engine_samples") or 0) >= (c_old.get("engine_samples") or 0):
        fails.append(f"engine_samples did not shrink: {c_old.get('engine_samples')} -> "
                     f"{c_new.get('engine_samples')} (ring horizon not applied?)")
    else:
        ok.append(f"engine_samples {c_old['engine_samples']} -> {c_new['engine_samples']}")
    return fails, ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="existing user database (copied, never modified)")
    ap.add_argument("--old-code", required=True, help="checkout the user runs today")
    ap.add_argument("--work", default=None)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    src = os.path.abspath(a.db)
    if not os.path.exists(src):
        raise SystemExit(f"no such database: {src}")
    work = a.work or tempfile.mkdtemp(prefix="limcars-upgrade-")
    os.makedirs(work, exist_ok=True)
    db = os.path.join(work, "user.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(src + suffix):
            shutil.copy2(src + suffix, db + suffix)
    print(f"rehearsing upgrade of {src} ({os.path.getsize(db) / 1e6:.1f} MB) in {work}")

    t0 = time.time()
    old = _run_dump(a.old_code, db, os.path.join(work, "old.json"))
    print(f"  baseline (old code): { {k: v for k, v in old['counts'].items() if v} }")
    size_before = os.path.getsize(db)
    boot = boot_new(db)
    print(f"  boot (new code):     {boot}")
    new = _run_dump(REPO, db, os.path.join(work, "new.json"))
    print(f"  after  (new code):   { {k: v for k, v in new['counts'].items() if v} }")
    size_after = os.path.getsize(db)

    fails, ok = compare(old, new, size_before=size_before, size_after=size_after)
    print(f"\nresults after {time.time() - t0:.0f}s — file {size_after / 1e6:.1f} MB:")
    for line in ok:
        print(f"  ok    {line}")
    for line in fails:
        print(f"  FAIL  {line}")
    print(f"\n{'FAILED: ' + str(len(fails)) + ' assertion(s) — see above' if fails else 'PASS — the upgrade moved no visible number'}")
    if not a.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"  kept: {work}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
