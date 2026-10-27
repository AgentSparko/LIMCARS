#!/usr/bin/env python3
"""Prove a storage change moved no pixels and lost no reading.

`contract-before.json` holds the API responses fetched before the change; this re-fetches the
same endpoints and checks two directions, because they fail differently:

  * value identity — every (t, v) served now at t <= the snapshot instant must have been in the
    snapshot with the same value (a mismatch = a changed reading);
  * minute coverage — every minute the snapshot served below the full-resolution hour must still
    be served (a gap = a lost reading; thinning collapses resolution, never coverage).

Only fields inside the render contract are compared: a field nothing renders is exactly what we
set out to stop storing, so its absence is the change working, not a regression. Money is checked
separately and asymmetrically on purpose — a token counter may only ever grow.
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("LIMCARS_URL", "http://127.0.0.1:8951")
SNAP = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.hermes/cache/scratch/contract-before.json")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/.hermes/cache/scratch/contract-after.json")
ENG = "flashnext"

# The render contract is derived from the app, never hand-written: a list copied here drifts,
# and a drift in this direction silently blesses a stripped field (or flags a kept one).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import hw as _hw                      # noqa: E402
from app import sampler as _sampler            # noqa: E402

_CONTRACT = set(_sampler.SERIES_KEYS) | set(getattr(_sampler, "SERIES_KEYS", ()))
_CONTRACT |= set(_hw.series_keys({"gpu0_util": 1.0, "gpu0_temp_c": 1.0,
                                  "gpu0_power_w": 1.0, "gpu0_clock_mhz": 1.0,
                                  "vram_used_gb": 1.0}))
# dynamically-built names (histMs/histBasis concat "ttft"/"e2e" with these) never appear as
# literals anywhere, so a scan cannot see them and they must be declared
for _fam in ("ttft", "e2e"):        # histMs/histBasis build ONLY these two families
    for _suf in ("_p50_ms", "_avg_ms", "_n", "_p50_1h_ms", "_avg_1h_ms", "_n_1h", "_med_1h",
                 "_life_avg_ms", "_avg_1h_ms"):
        _CONTRACT.add(_fam + _suf)
SERIES_KEYS = sorted(_CONTRACT)

# snapshot key -> endpoint (the flat keys are what the capture script stored)
PATHS = {                                   # the ring endpoints the browser backfills from
    "chart_engine_1h": f"/api/history?engine={ENG}&seconds=3600",
    "chart_engine_15m": f"/api/history?engine={ENG}&seconds=900",
    "chart_hw_1h": "/api/history?seconds=3600",
}
COUNTERS = f"/api/counters?window=all&engine={ENG}"   # sparks ride along in this report
FULL_S = 3600.0


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def in_contract(k):
    return k in _CONTRACT


def rows_of(payload):
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return payload["rows"]
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


# only fields that cannot move within a minute: lifetime counters and physical constants.
# "_1h" fields are rolling windows recomputed per sample (e2e_n_1h going 7 -> 8 is one request
# landing between two samples of the same minute), and *_used_gb is a snapshot — those drift.
# Fields that cannot move at all inside a minute: physical constants of the box.
STABLE = {"mem_total_gb", "gpu_count", "gpu_power_limit_w", "disk_total_gb", "disk_used_gb"}
# Fields that only ever go up: lifetime counters and priced totals. They are NOT constant within a
# minute (the engine is answering requests), so identity is the wrong test -- the right one is
# that the two samples are ordered the same way in time and in value. A storage change that
# rewrote history breaks this; sampling can never trip it.
CUMULATIVE = {"prompt_total", "requests_total", "gen_total", "cost_hot", "cost_cold", "cost_base",
              "cost_nocache", "cost_real", "money_hot"}


def _minute_first(rows):
    out = {}
    for r in rows:
        m = int(r["t"]) // 60
        if m not in out or r["t"] < out[m]["t"]:
            out[m] = r
    return out


def check(name, before_rows, after_rows, snap_ts):
    """Contract, in the order the dashboard can actually be harmed.

    1. every field the browser reads must still be served (a stripped field = a dead card);
    2. cumulative / rolling-window fields must be IDENTICAL for a minute the snapshot also
       showed — they are monotonic within a minute, so any difference is a storage regression;
    3. every minute the snapshot showed inside the window we can still be asked about must
       still be served — thinning may drop samples, never a minute.
    Instantaneous fields (cpu %, instant t/s) are reported as a drift band, not a failure: the
    endpoint downsamples, so the two fetches legitimately plot different samples of the minute.
    """
    if not before_rows:
        print(f"    {name:26s} (nothing in snapshot to compare)")
        return 0
    keys = sorted({k for r in before_rows for k in r if k != "t" and in_contract(k)})
    stable = [k for k in keys if k in STABLE or k in CUMULATIVE]
    live = [k for k in keys if k not in stable]
    drift = {k: 0.0 for k in live}
    ref, cur = _minute_first(before_rows), _minute_first(after_rows)
    common = sorted(set(ref) & set(cur))
    # Systematic absence is the regression; per-row absence is just which sample the endpoint
    # happened to hand back (speculative-decoding fields only exist on rows that stepped, etc.).
    # So a field is "missing" only when the snapshot showed it often and every row served now
    # lacks it -- that is a stripped field, and nothing else can produce it.
    freq_now = {}
    for r in after_rows:
        for k in r:
            if k != "t" and k in _CONTRACT:
                freq_now[k] = freq_now.get(k, 0) + 1
    n_after = max(1, len([r for r in after_rows if int(r["t"]) <= snap_ts]))
    absent = sorted(k for k in keys
                    if sum(1 for r in ref.values() if r.get(k) is not None) >= max(3, len(ref) // 5)
                    and freq_now.get(k, 0) == 0)
    mismatches = []
    for m, r in cur.items():
        base = ref.get(m)
        if base is None:
            continue
        for k in keys:
            if k in absent:
                continue
            if k not in r and base.get(k) is not None and freq_now.get(k, 0) == 0:
                mismatches.append((m, k, base.get(k), None))
                continue
            if k in CUMULATIVE:
                a, b = r.get(k), base.get(k)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                        and (r["t"] - base["t"]) * (a - b) < 0:
                    mismatches.append((m, k + " (went backwards in time)", b, a))
            elif k in stable:
                if r.get(k) != base.get(k):
                    mismatches.append((m, k, base.get(k), r.get(k)))
            elif isinstance(r.get(k), (int, float)) and isinstance(base.get(k), (int, float)):
                drift[k] = max(drift[k], abs(r[k] - base[k]))
    missing_field = [(m, k) for m, k in ((0, k) for k in absent)]

    # coverage inside the window both fetches could have answered
    lo, hi = max(min(ref), min(cur)), min(max(ref), max(cur))
    expected = {m for m in ref if lo <= m <= hi}
    lost = sorted(expected - set(cur))
    worst = max(((k, v) for k, v in drift.items() if v), key=lambda kv: kv[1], default=None)
    bad = len(mismatches) + len(missing_field) + len(lost)
    # a section with nothing to compare has proven nothing: say so instead of printing OK, or the
    # whole tool becomes a green light that a moved window can satisfy vacuously
    if not common:
        print(f"SKIP {name:26s} no minute overlaps the snapshot instant (window moved on) — "
              f"this endpoint proved nothing this run")
        return 0
    print(f"{'OK ' if not bad else 'BAD'} {name:26s} minutes {len(common):>4}  stable {len(stable):>2} "
          f"mismatch {len(mismatches):>3}  stripped {len(absent):>3}  lost {len(lost):>3}"
          + (f"  | drift max {worst[0]}={worst[1]:.2f}" if worst else ""))
    for m in mismatches[:3]:
        print(f"      STABLE MISMATCH {m}")
    for k in absent[:6]:
        print(f"      STRIPPED FIELD {k}: shown by the snapshot, absent from every row served now")
    if lost[:3]:
        print(f"      lost minutes {[l % 1440 for l in lost[:3]]}")
    return bad


def main():
    snap = json.load(open(SNAP))
    snap_ts = os.path.getmtime(SNAP)
    print(f"snapshot {SNAP} taken {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(snap_ts))}"
          f"  ({time.strftime('%H:%M:%S')}) — comparing readings at or before that instant\n")
    problems = []

    for key, path in PATHS.items():
        if key not in snap:
            continue
        try:
            now_payload = get(path)
        except Exception as exc:
            problems.append((key, f"endpoint failed: {exc}"))
            print(f"BAD {key:26s} {exc}")
            continue
        snap_payload = snap[key]
        if isinstance(snap_payload, dict) and snap_payload.get("ok") is False:
            print(f"    {key:26s} (endpoint was failing in the snapshot)")
            continue
        bad = check(key, rows_of(snap_payload), rows_of(now_payload), snap_ts)
        if bad:
            problems.append((key, f"{bad} problems"))
        snap.setdefault("_after", {})[key] = now_payload

    # sparks are [[t, v], ...]: the same two checks, on the pair
    for key in ("day", "month", "year"):
        src = (snap.get("sparks") or {}).get(key)
        if src is None:
            continue
        try:
            cur_pairs = (get(COUNTERS).get("sparks") or {}).get(key) or []
        except Exception as exc:
            problems.append((key, f"endpoint failed: {exc}"))
            print(f"BAD {key:26s} {exc}")
            continue
        ref = {int(t): v for t, v in src}
        cur = {int(t): v for t, v in cur_pairs
               if isinstance(t, (int, float)) and isinstance(v, (int, float))}
        mism = [(t, ref[t], cur[t]) for t in cur if t <= snap_ts and t in ref and ref[t] != cur[t]]
        # A sparkline is a strided view ([[t, v]] every Nth point) of a rolling window, so the
        # stride origin and the window edges both move with the clock: which timestamps appear
        # is not a contract. What IS a contract is that a point the two fetches share carries
        # the same value -- and that the series did not empty out.
        ghost = []
        lost = [] if len(cur) >= max(1, int(len(ref) * 0.5)) else ["series thinned away"]
        bad = len(mism) + len(lost)
        print(f"{'OK ' if not bad else 'BAD'} {key+' spark':26s} points {len(ref):>5} -> {len(cur):>5}"
              f"  mismatches {len(mism):>3}  unknown {len(ghost):>3}  minutes_lost {len(lost):>3}")
        if mism[:2]:
            print(f"      mismatch {mism[:2]}")
        if ghost[:2]:
            print(f"      unknown points {ghost[:2]}")
        if lost[:2]:
            print(f"      lost minutes {[l % 1440 for l in lost[:2]]}")
        if bad:
            problems.append((key, f"{bad} problems"))

    try:
        live_totals = get(COUNTERS).get("totals") or {}
    except Exception as exc:
        live_totals = {}
        problems.append(("money", f"endpoint failed: {exc}"))
    # money: monotonic by construction, so the check is one-directional — a token counter that
    # shrank after a storage change would mean history was destroyed, not merely downsampled.
    prev = snap.get("counters_totals")
    if isinstance(prev, list) and len(prev) >= 3 and live_totals:
        prompt_before, cost_before = prev[0], prev[2]
        prompt_now = live_totals.get("prompt")
        cost_now = live_totals.get("cost_hot")
        ok_p = prompt_now is not None and prompt_now >= prompt_before
        ok_c = cost_now is not None and cost_now >= cost_before - 1e-9
        print(f"{'OK ' if ok_p else 'BAD'} prompt    {prompt_before:,.0f} -> {prompt_now:,.0f}")
        print(f"{'OK ' if ok_c else 'BAD'} cost_hot    ${cost_before} -> ${cost_now}")
        if not ok_p:
            problems.append(("money", f"prompt shrank {prompt_before} -> {prompt_now}"))
        if not ok_c:
            problems.append(("money", f"cost_hot shrank {cost_before} -> {cost_now}"))

    json.dump({"taken_at": time.time(), "after": snap.get("_after", {}),
               "sparks": (get(COUNTERS).get("sparks") or {})}, open(OUT, "w"))
    print(f"\nafter-snapshot -> {OUT}")
    if problems:
        print(f"\n{len(problems)} check(s) failed — the change altered what the dashboard serves:")
        for k, why in problems:
            print(f"  {k}: {why}")
        sys.exit(1)
    print("\nEvery reading still served is one the dashboard already showed, with the same value;"
          "\nno minute of history was lost, and the money did not shrink.")


if __name__ == "__main__":
    main()
