#!/usr/bin/env python3
"""Backfill counter_buckets from the OLD LIMCARS app.db dash_events history.

The new LIMCARS store starts empty at rebuild; the old classic dashboard kept
every prefill/decode event in `dash_events` (ts in ms). This imports the real
usage of the selected model into this store's minute buckets so the
lost-potential bubble / daily chart have history from day zero.

Honesty rules baked in:
  * only events whose engine served the requested model are imported
    (join dash_events.engine_id -> engines.served_model, case-insensitive);
  * minutes that ALREADY exist in this store are skipped (live sampling
    wins; the same minute counted twice would double-charge);
  * buckets are written with synthetic=0 (they are real measured traffic),
    so the store's 366-day retention applies exactly as for live buckets.

External prerequisite: the old LIMCARS checkout with data/app.db.
Override with --source (or LIMCARS_LEGACY_DB); nothing here touches the
source database (opened read-only).

Usage:
  python3 scripts/import_legacy_history.py --dry-run
  python3 scripts/import_legacy_history.py --model "Qwen/Qwen3.8-Flash-Next"
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.store import Store  # noqa: E402

DEFAULT_SOURCE = os.path.expanduser("~/projects/limcars/data/app.db")
DEFAULT_TARGET = os.path.expanduser("~/projects/limcars-next/data/limcars.db")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill counter_buckets from old LIMCARS dash_events")
    ap.add_argument("--source", default=os.environ.get("LIMCARS_LEGACY_DB",
                      DEFAULT_SOURCE), help="old LIMCARS app.db path")
    ap.add_argument("--target", default=os.environ.get("LIMCARS_DB",
                      DEFAULT_TARGET), help="new LIMCARS limcars.db path")
    ap.add_argument("--engine", default="flashnext",
                    help="engine id to attribute the history to")
    ap.add_argument("--model", default="Qwen/Qwen3.8-Flash-Next",
                    help="served_model filter (case-insensitive)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.source):
        print(f"ERROR: legacy db not found: {a.source}\n"
              f"fetch the old LIMCARS checkout or pass --source", file=sys.stderr)
        return 2
    src = sqlite3.connect(f"file:{a.source}?mode=ro", uri=True)

    import re
    # AGENTS.md rule 2a: quant/hardware variant suffixes must not hide an era
    VARIANTS = ("hybrid48", "nvidia", "flashinfer", "exl3", "sage",
                "fp8", "fp4", "fp16", "bf16", "awq", "gptq", "int8", "int4")

    def norm(s):                       # 'Qwen/Qwen3.8-Flash-Next' ==
        t = re.sub(r"[^a-z0-9]", "", (s or "").lower())
        while t.startswith("qwen"):    # org prefix AND 'Qwen3.8' fused name
            t = t[4:]
        changed = True
        while changed:                 # strip trailing variant tags
            changed = False
            for v in VARIANTS:
                if t.endswith(v) and len(t) > len(v):
                    t = t[:-len(v)]
                    changed = True
        return t                       # model under a quant/variant name
    want = norm(a.model)
    alleng = src.execute("select id, served_model from engines").fetchall()
    ids = [i for i, m in alleng if norm(m) == want]
    # audit: silent era-drop was the importer's worst failure — print every
    # source engine with its verdict so a missed variant is visible, not quiet
    for i, m in alleng:
        print(f"  engine {i:>3}  {str(m)!r:<55} {'MATCH' if i in ids else '-'}")
    if not ids:
        print(f"ERROR: no engine in {a.source} ever served '{a.model}'",
              file=sys.stderr)
        return 2
    ph = ",".join("?" * len(ids))
    print(f"legacy engines serving '{a.model}': {ids}")

    # AGENTS.md: the target engine id must be one config.json actually serves,
    # else a typo writes an invisible parallel history
    cfg_p = os.path.join(os.path.dirname(os.path.abspath(a.target)), "config.json")
    if os.path.exists(cfg_p):
        import json
        valid = [e.get("id") for e in json.load(open(cfg_p)).get("engines", [])]
        if valid and a.engine not in valid:
            print(f"ERROR: --engine '{a.engine}' is not served by this dashboard "
                  f"(config.json ids: {valid}). Fix --engine or add the engine.",
                  file=sys.stderr)
            return 2

    # aggregate dash_events -> minute buckets (mirror store counter semantics:
    # prompt = total input incl. cached; prefill rows carry the input, decode
    # rows carry generated tokens)
    buckets: dict[int, dict] = defaultdict(
        lambda: {"prompt": 0.0, "cached": 0.0, "gen": 0.0,
                 "reqs": 0.0, "iters": 0.0, "dec_min": None, "dec_peak": None,
                 "spec_sum": None, "spec_n": 0})
    n = 0
    for ts, kind, new_tok, cached_tok, gen_tok in src.execute(
            f"select ts, kind, new_tok, cached_tok, gen_tok from dash_events"
            f" where engine_id in ({ph}) and kind in ('prefill','decode')", ids):
        n += 1
        b = buckets[int(ts // 60000) * 60]      # ms -> minute-aligned seconds
        if kind == "prefill":
            b["prompt"] += (new_tok or 0) + (cached_tok or 0)
            b["cached"] += cached_tok or 0
            b["reqs"] += 1
        else:
            b["gen"] += gen_tok or 0
            b["iters"] += 1
    print(f"source events: {n}  ->  minute buckets: {len(buckets)}")

    # audit: --dry-run used to CREATE the target db (Store init runs schema);
    # read-only probe instead, and only the write path upgrades to Store()
    try:
        con = sqlite3.connect(f"file:{a.target}?mode=ro", uri=True, timeout=30)
        existing = {r[0] for r in con.execute(
            "select ts from counter_buckets where engine=?", (a.engine,))}
        con.close()
    except sqlite3.OperationalError:      # fresh/empty target: nothing exists
        existing = set()
    todo = {ts: b for ts, b in buckets.items() if ts not in existing
            and b["prompt"] + b["gen"] > 0}
    print(f"minutes already recorded live: {len(buckets) - len(todo)}"
          f"  to import: {len(todo)}")
    if a.dry_run:
        print("dry-run: nothing written")
        return 0
    # audit: save_counter_bucket is INSERT OR REPLACE — with the live 2 Hz
    # sampler running, the 'skip existing minutes' snapshot was TOCTOU-racy
    # and could silently REPLACE a real live minute with legacy aggregates.
    tgt = Store(a.target)                 # ensures schema exists
    con = sqlite3.connect(a.target, timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")    # refuse to interleave with a writer
        wrote = 0
        for ts, b in sorted(todo.items()):
            cur = con.execute(
                "INSERT INTO counter_buckets(engine,ts,prompt,cached,gen,reqs,"
                "iters,dec_min,dec_peak,spec_sum,spec_n,synthetic)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,0)"
                " ON CONFLICT(engine,ts) DO NOTHING",  # never clobber a live row
                (a.engine, ts, b["prompt"], b["cached"], b["gen"], b["reqs"],
                 b["iters"], b["dec_min"], b["dec_peak"], b["spec_sum"],
                 b["spec_n"]))
            wrote += cur.rowcount
        con.commit()
    finally:
        con.close()
    days = len({ts // 86400 for ts in todo})
    print(f"imported {wrote} buckets across {days} days into '{a.engine}'"
          f" ({len(todo) - wrote} skipped: already recorded live)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
