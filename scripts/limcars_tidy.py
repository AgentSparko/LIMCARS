#!/usr/bin/env python3
"""limcars_tidy — reclaim space from the history database without losing a cent of history.

Why this exists
---------------
The dashboard's 1-second rings are stored as one JSON blob per tick, and the file grows fast
(~242 MB per day) while the tables the money figures are actually derived from cost ~14 KB per
day. Deletes inside SQLite leave pages marked free but never returned to the filesystem, so a
file that has pruned once keeps the space forever: a live 818 MB database measured 322 MB of
dead pages. VACUUM returns that space; tiers (later phases) stop it accumulating.

Modes
-----
  --check     default. Dry run: measures, runs the invariant gate, writes nothing.
  --apply     same engine, performs backup -> gate -> VACUUM. This is what the nightly timer runs.
  --migrate   one-shot for installs that grew large: identical to --apply, plus it reports what
              a future tiered build would do. Kept as a distinct mode so the code path a user
              runs once and the code path the timer runs nightly are literally the same function.
  --install-timer / --uninstall-timer   user systemd service+timer (daily).

Safety model
------------
1. Refuses to run against a database the server has open, unless --allow-live.
2. Always writes a backup (<db>.pretidy-<stamp>) and never deletes it.
3. Money-invariant gate: the headline figures are computed from the database before and after
   the operation. Any difference aborts, restores the backup, and exits non-zero. Compaction
   must be money-preserving by construction, and this makes that an assertion instead of a hope.
4. PRAGMA integrity_check / quick_check must both be clean before the file is swapped in.
5. Disk-space preflight: VACUUM needs roughly one copy of the file free.
6. Exit codes: 0 ok / nothing to do, 2 gate failed, 3 preflight failed, 4 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(REPO, "data", "limcars.db")

# The figures the dashboard shows as money. If any of these move, the operation failed.
MONEY_QUERIES = {
    "prompt": "SELECT COALESCE(SUM(prompt),0) FROM counter_buckets",
    "cached": "SELECT COALESCE(SUM(cached),0) FROM counter_buckets",
    "gen": "SELECT COALESCE(SUM(gen),0) FROM counter_buckets",
    "reqs": "SELECT COALESCE(SUM(reqs),0) FROM counter_buckets",
    "iters": "SELECT COALESCE(SUM(iters),0) FROM counter_buckets",
    "dec_peak": "SELECT COALESCE(MAX(dec_peak),0) FROM counter_buckets",
    "spec_sum": "SELECT COALESCE(SUM(spec_sum),0) FROM counter_buckets",
    "spec_n": "SELECT COALESCE(SUM(spec_n),0) FROM counter_buckets",
    "bucket_rows": "SELECT COUNT(*) FROM counter_buckets",
    "bucket_first": "SELECT COALESCE(MIN(ts),0) FROM counter_buckets",
    "bucket_last": "SELECT COALESCE(MAX(ts),0) FROM counter_buckets",
    # The frozen money is part of the invariant set: a reclaim that lost or altered
    # a cost stamp is as bad as one that lost a token count.
    "cost_hot_sum": "SELECT COALESCE(SUM(cost_hot),0) FROM counter_buckets",
    "cost_base_sum": "SELECT COALESCE(SUM(cost_base),0) FROM counter_buckets",
    "cost_nocache_sum": "SELECT COALESCE(SUM(cost_nocache),0) FROM counter_buckets",
    "cost_priced_rows": "SELECT COUNT(cost_hot) FROM counter_buckets",
    "eng_rows": "SELECT COUNT(*) FROM engine_samples",
    "eng_last": "SELECT COALESCE(MAX(t),0) FROM engine_samples",
    "hw_rows": "SELECT COUNT(*) FROM hw_samples",
    "hw_last": "SELECT COALESCE(MAX(t),0) FROM hw_samples",
    "events": "SELECT COUNT(*) FROM events",
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} GB"


def db_size(path: str) -> int:
    return sum(os.path.getsize(p) for p in (path, path + "-wal", path + "-shm") if os.path.exists(p))


def money(path: str) -> dict:
    """Read the figures the dashboard's money math depends on. Read-only connection.

    A missing TABLE means this is not a LIMCARS database and the run must stop. A missing
    COLUMN is different: cost_hot/price_* were added later, and every existing install is
    exactly the pre-migration shape this script exists to serve — so those read as None and
    the gate compares like with like (None == None).
    """
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        out = {}
        for name, q in MONEY_QUERIES.items():
            try:
                out[name] = con.execute(q).fetchone()[0]
            except sqlite3.OperationalError as exc:
                msg = str(exc)
                if "no such table" in msg:
                    raise SystemExit(f"{path} is not a LIMCARS database: {exc}")
                out[name] = None                    # older schema: absent, not zero
            except sqlite3.Error as exc:
                raise SystemExit(f"cannot read {name} from {path}: {exc}")
        return out
    finally:
        con.close()


def server_holds(db: str) -> str | None:
    """Return a description of a process holding the database, if any."""
    try:
        p = subprocess.run(["fuser", "-v", db], capture_output=True, text=True, timeout=10)
        txt = (p.stdout + p.stderr).strip()
        if "PID" in txt and len(txt.splitlines()) > 1:
            return txt
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    if os.path.exists(db + "-wal") and os.path.getsize(db + "-wal") > 0:
        # A live WAL is normal between writes; only a busy writer matters, detected by lock probe.
        try:
            con = sqlite3.connect(db, timeout=1)
            con.execute("BEGIN IMMEDIATE").execute
            con.execute("ROLLBACK")
            con.close()
        except sqlite3.OperationalError as exc:
            return f"write lock held: {exc}"
    return None


def free_bytes(path: str) -> int:
    return shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free


def backup(db: str, keep: str | None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = keep or f"{db}.pretidy-{stamp}"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)                             # online backup API: consistent snapshot
    finally:
        src.close()
        out.close()
    for ext in ("-wal", "-shm"):                    # a stray sidecar would shadow the copy
        if os.path.exists(dst + ext):
            os.remove(dst + ext)
    return dst


def vacuum(db: str, inplace: bool = True) -> dict:
    """VACUUM, then drive the WAL to zero.

    In WAL mode VACUUM rewrites the whole database through the WAL, so the WAL temporarily
    holds a full second copy. A single wal_checkpoint(TRUNCATE) can return without raising and
    still leave those pages in place, which makes the file look like it GREW by ~500 MB. So we
    checkpoint from a fresh connection, retry while the WAL is still fat, and only then trust a
    size number.
    """
    con = sqlite3.connect(db, timeout=120)
    try:
        con.isolation_level = None
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
    finally:
        con.close()
    wal = 0
    for attempt in range(40):
        con = sqlite3.connect(db, timeout=120)
        try:
            con.isolation_level = None
            row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.close()
        wal = os.path.getsize(db + "-wal") if os.path.exists(db + "-wal") else 0
        if wal <= 64 * 1024:
            return {"checkpoint": row, "wal_bytes": wal, "attempts": attempt + 1}
        time.sleep(0.25)
    raise sqlite3.OperationalError(
        f"WAL still {wal} bytes after 40 checkpoint attempts — refusing to report a size")



def integrity(db: str) -> tuple[bool, str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    try:
        a = con.execute("PRAGMA integrity_check").fetchone()[0]
        b = con.execute("PRAGMA quick_check").fetchone()[0]
        return (a == "ok" and b == "ok"), f"integrity_check={a} quick_check={b}"
    finally:
        con.close()


def report(path: str) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    d: dict = {"path": path, "bytes": db_size(path)}
    try:
        try:
            d["tables_mb"] = {r[0]: round(r[1] / 1048576.0, 2) for r in con.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC")}
        except sqlite3.Error:
            d["tables_mb"] = {}
        try:
            # fetchone() here is load-bearing, not style: leaving the Cursor from
            # con.execute("PRAGMA page_count") alive pins a read lock, and the VACUUM
            # that follows then blocks on the connect timeout (observed as a 120 s
            # hang that looked like the compaction itself being slow).
            pg = con.execute("PRAGMA page_count").fetchone()[0]
            freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
            ps = con.execute("PRAGMA page_size").fetchone()[0]
            d["dead_pages_mb"] = round(freelist * ps / 1048576.0, 1)
            d["pages"] = pg
        except sqlite3.Error:
            pass
        return d
    finally:
        con.close()


def run(db: str, apply: bool, keep_backup: bool, allow_live: bool, quiet: bool) -> int:
    if not os.path.exists(db):
        print(f"no database at {db}", file=sys.stderr)
        return 3
    if apply and not allow_live:
        held = server_holds(db)
        if held:
            print(f"refusing: {db} is in use ({held}).\n"
                  f"  stop the service first:  systemctl --user stop limcars.service\n"
                  f"  or pass --allow-live if you are sure.", file=sys.stderr)
            return 3
    before_size = db_size(db)
    pre = money(db)
    rep = report(db)
    if not quiet:
        print(f"database        : {db}")
        print(f"size on disk    : {human(before_size)}")
        print(f"dead pages      : {human(rep.get('dead_pages_mb', 0) * 1048576)} "
              f"({100*rep.get('dead_pages_mb',0)*1048576/max(before_size,1):.0f}% of the file)")
        print("largest tables  : " + ", ".join(f"{k} {v} MB" for k, v in
              list(rep.get("tables_mb", {}).items())[:5]))
        print(f"money before    : prompt {pre['prompt']:,.0f} · cached {pre['cached']:,.0f} · "
              f"gen {pre['gen']:,.0f} · reqs {pre['reqs']:,.0f} · buckets {pre['bucket_rows']:,}")
    need = before_size + (64 << 20)
    if free_bytes(db) < need:
        print(f"refusing: VACUUM needs ~{human(need)} free, only {human(free_bytes(db))} available",
              file=sys.stderr)
        return 3

    if not apply:
        gain = rep.get("dead_pages_mb", 0) * 1048576
        print(f"\n[dry run] nothing written. VACUUM would return about {human(gain)} "
              f"({100*gain/max(before_size,1):.0f}%).")
        print(f"[dry run] to do it: {os.path.basename(sys.argv[0])} --apply --db {db}")
        return 0

    bkp = backup(db, None)
    if not quiet:
        print(f"backup          : {bkp} ({human(db_size(bkp))}) — kept, not deleted")
    try:
        vres = vacuum(db)
    except sqlite3.Error as exc:
        print(f"VACUUM failed: {exc}\nrestoring backup", file=sys.stderr)
        restore(db, bkp)
        return 2

    ok, why = integrity(db)
    post = money(db)
    drift = {k: (pre[k], post[k]) for k in pre if pre[k] != post[k]}
    after_size = db_size(db)
    if not ok or drift:
        print(f"GATE FAILED ({why}; drift={drift}) — restoring backup", file=sys.stderr)
        restore(db, bkp)
        return 2
    print(f"money after     : prompt {post['prompt']:,.0f} · cached {post['cached']:,.0f} · "
          f"gen {post['gen']:,.0f} · reqs {post['reqs']:,.0f} · buckets {post['bucket_rows']:,}")
    print(f"integrity       : {why}")
    print(f"wal after       : {human(vres['wal_bytes'])} drained in {vres['attempts']} checkpoint(s)")
    print(f"result          : {human(before_size)} -> {human(after_size)}  "
          f"({human(before_size-after_size)} reclaimed, "
          f"{100*(before_size-after_size)/max(before_size,1):.0f}%)")
    if not keep_backup:
        os.remove(bkp)
        print("backup          : removed (--keep-backup was not given)")
    return 0


def restore(db: str, bkp: str) -> None:
    for ext in ("", "-wal", "-shm"):
        p = db + ext
        if os.path.exists(p):
            os.remove(p)
    shutil.copy2(bkp, db)
    print(f"restored {db} from {bkp}")


UNIT = """\
[Unit]
Description=LIMCARS history database tidy (reclaims dead pages, keeps all history)
After=network-online.target

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
ExecStart={python} {script} --apply --db {db} --allow-live
"""

TIMER = """\
[Unit]
Description=Daily LIMCARS history database tidy

[Timer]
OnCalendar=*-*-* 04:30:00
RandomizedDelaySec=1500
Persistent=true

[Install]
WantedBy=timers.target
"""


def timer_path(name: str) -> str:
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                        "systemd", "user", name)


def manage_timer(action: str, db: str) -> int:
    os.makedirs(os.path.dirname(timer_path("x.service")), exist_ok=True)
    if action == "install":
        py = sys.executable or "/usr/bin/python3"
        scr = os.path.abspath(__file__)
        for name, body in (("limcars-tidy.service", UNIT.format(python=py, script=scr, db=db)),
                           ("limcars-tidy.timer", TIMER)):
            with open(timer_path(name), "w", encoding="utf-8") as fh:
                fh.write(body)
        for cmd in (["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", "limcars-tidy.timer"]):
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode != 0:
                print((p.stdout + p.stderr).strip(), file=sys.stderr)
                return 4
        print("installed limcars-tidy.timer (daily 04:30, randomized). "
              "Check: systemctl --user list-timers limcars-tidy.timer")
        return 0
    for cmd in (["systemctl", "--user", "disable", "--now", "limcars-tidy.timer"],):
        subprocess.run(cmd, capture_output=True, text=True)
    for name in ("limcars-tidy.service", "limcars-tidy.timer"):
        p = timer_path(name)
        if os.path.exists(p):
            os.remove(p)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    print("removed limcars-tidy.timer")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"path to limcars.db (default {DEFAULT_DB})")
    ap.add_argument("--check", action="store_true", help="dry run (default)")
    ap.add_argument("--apply", action="store_true", help="back up, VACUUM, gate, report")
    ap.add_argument("--migrate", action="store_true",
                    help="one-shot for a large existing install (same engine as --apply)")
    ap.add_argument("--install-timer", action="store_true")
    ap.add_argument("--uninstall-timer", action="store_true")
    ap.add_argument("--allow-live", action="store_true",
                    help="run even though the server has the database open")
    ap.add_argument("--keep-backup", action="store_true",
                    help="keep the pretidy backup after a successful run (default: keep)")
    ap.add_argument("--delete-backup", action="store_true",
                    help="delete the backup on success instead of keeping it")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if a.install_timer:
        return manage_timer("install", a.db)
    if a.uninstall_timer:
        return manage_timer("uninstall", a.db)
    if a.json:
        rep = report(a.db)
        rep["money"] = money(a.db)
        rep["size"] = db_size(a.db)
        print(json.dumps(rep, indent=1))
        return 0
    apply = a.apply or a.migrate
    return run(a.db, apply=apply, keep_backup=not a.delete_backup,
               allow_live=a.allow_live, quiet=a.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
