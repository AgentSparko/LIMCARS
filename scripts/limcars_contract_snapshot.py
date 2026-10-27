#!/usr/bin/env python3
"""Capture the dashboard's visible contract — the companion to limcars_contract_diff.py.

Fetches the endpoints the browser actually calls and stores them as the baseline a storage
change is judged against. Capture BEFORE the change, then run the diff script after: it asserts
no rendered field disappeared, cumulative values are unchanged for any minute both fetches can
answer, no minute of history was dropped, and the money did not shrink.

Usage:  python3 scripts/limcars_contract_snapshot.py [out.json]
Env:    LIMCARS_URL (default http://127.0.0.1:8951)
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("LIMCARS_URL", "http://127.0.0.1:8951")
ENG = os.environ.get("LIMCARS_ENGINE", "flashnext")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/.hermes/cache/scratch/contract-before.json")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    counters = get(f"/api/counters?window=all&engine={ENG}")
    snap = {
        "taken": time.time(),
        "base": BASE, "engine": ENG,
        "chart_engine_1h": get(f"/api/history?engine={ENG}&seconds=3600"),
        "chart_engine_15m": get(f"/api/history?engine={ENG}&seconds=900"),
        "chart_hw_1h": get("/api/history?seconds=3600"),
        "counters_totals": [counters["totals"]["prompt"], counters["totals"]["prompt"],
                            counters["totals"]["cost_hot"], counters["totals"]["cost_hot"]],
        "savings_series": {k: counters.get(k) for k in
                           ("savings_day", "savings_month", "savings_year")},
        "savings_bubble": counters.get("project") or [],
        "sparks": {k: (counters.get("sparks") or {}).get(k) or []
                   for k in ("day", "month", "year")},
    }
    json.dump(snap, open(OUT, "w", encoding="utf-8"))
    print(f"captured -> {OUT}")
    for k, v in snap["sparks"].items():
        print(f"  {k:5s} spark {len(v):>4} points")
    for k in ("chart_engine_1h", "chart_engine_15m", "chart_hw_1h"):
        print(f"  {k:16s} {len(snap[k].get('rows') or []):>4} rows")
    print(f"  money: prompt {snap['counters_totals'][0]:,.0f}  cost_hot ${snap['counters_totals'][2]}")


if __name__ == "__main__":
    main()
