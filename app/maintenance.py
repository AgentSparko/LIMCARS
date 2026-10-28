"""The dashboard reclaims its own database space.

Why a thread and not only a cron/timer: users update by pulling and restarting. A systemd
timer is a second thing to install, and the installs that never installed it are precisely the
ones still carrying an 800 MB file whose rows were pruned weeks ago — SQLite leaves deleted
pages on the freelist, so the file never shrinks by itself. Measured on a real install: 322 MB
of the 818 MB file (39%) was dead pages.

So the app does it itself: once shortly after boot (that is the moment right after an update,
when the old high-water mark is still sitting there) and then daily. The behaviour mirrors the
guard rails of scripts/limcars_tidy.py, because the two must not disagree:

  * only act when the freelist is worth taking (>= reclaim_ratio of the file),
  * refuse when free disk cannot hold a second copy (VACUUM rewrites the file),
  * at most one VACUUM per interval, so a crash-loop cannot thrash the disk,
  * never touch data: this path only reclaims pages, it deletes no rows. Pruning stays where
    it already lives (Store._prune on write, set_persist_window on change).

Everything it decided is kept in .status so /api/storage can show the operator what retention
bought, instead of making them run a script to find out.
"""
from __future__ import annotations

import os
import logging
import shutil
import threading
import time


log = logging.getLogger("limcars.maintenance")


class Maintenance(threading.Thread):
    def __init__(self, cfg: dict, store):
        super().__init__(name="limcars-maintenance", daemon=True)
        self.store = store
        self.enabled = bool(cfg.get("auto_compact", True))
        self.interval = max(3600.0, float(cfg.get("compact_interval_s", 86400)))
        self.reclaim_ratio = float(cfg.get("compact_reclaim_ratio", 0.10))
        self.start_delay = max(5.0, float(cfg.get("compact_delay_s", 45)))
        self.disk_headroom = float(cfg.get("compact_disk_headroom", 1.2))
        # a young database has nothing worth reclaiming; do not rewrite it for sport
        self.min_bytes = float(cfg.get("compact_min_bytes", 20 << 20))
        # how often to fold the rings down to one row per minute past the full-resolution hour
        self.thin_interval = max(60.0, float(cfg.get("thin_interval_s", 300)))
        # how long the pre/post compaction invariants will wait for the store lock before
        # declaring the check unavailable (which then blocks the compaction, never allows it)
        self.probe_timeout = max(0.5, float(cfg.get("invariant_timeout_s", 20)))
        self._stop = threading.Event()
        self.status: dict = {"enabled": self.enabled, "runs": 0, "last_run": None,
                             "last_vacuum": None, "freed_bytes_total": 0,
                             "last_report": None, "note": "not yet run"}

    # --- one pass -------------------------------------------------------------

    def _db_bytes(self) -> int:
        p = getattr(self.store, "path", None)
        if not p:
            return 0
        return sum(os.path.getsize(x) for x in (p, p + "-wal", p + "-shm")
                   if os.path.exists(x))

    def _invariants(self, cap: int) -> dict:
        """Ring rows per minute and the money of every bucket, at or before `cap`.

        The cap is the point. The server is writing rings twice a second and a bucket a
        minute while this runs, so comparing raw counts before/after a VACUUM would always
        "drift". Measuring both sides against one frozen timestamp makes it a real check.
        Kept per slice rather than as totals because the cap alone does not freeze them: see
        _lost() for what the server still changes under it.
        """
        con = getattr(self.store, "_con", None)
        out: dict = {}
        if con is None:
            return out
        lock = getattr(self.store, "_lock", None)
        held = False
        try:
            if lock is not None:
                # bounded, and ownership tracked: this runs on either side of a VACUUM, and a
                # blocked acquire here would wedge the tidy thread forever (the same failure the
                # standalone tidy script had against an open Cursor)
                held = lock.acquire(timeout=self.probe_timeout)
                if not held:
                    self.status["note"] = "invariants skipped: store lock busy"
                    return {}
            for table in ("engine_samples", "hw_samples"):
                try:
                    out[table] = dict(con.execute(
                        f"SELECT CAST(t AS INTEGER)/60*60, COUNT(*) FROM {table}"
                        " WHERE t<=? GROUP BY 1", (cap,)).fetchall())
                except Exception:  # noqa: BLE001 — a missing table is not a drift signal
                    out[table] = None
            try:
                out["counter_buckets"] = {(e, ts): rest for e, ts, *rest in con.execute(
                    "SELECT engine, ts, prompt, cached, gen, reqs, cost_hot FROM counter_buckets"
                    " WHERE ts<=?", (cap,))}
            except Exception:  # noqa: BLE001
                pass
        finally:
            if held:
                lock.release()
        # a read that produced nothing is NOT a clean check — returning {} here used to make the
        # VACUUM guard pass vacuously, which is the one thing it exists to prevent
        return out if "counter_buckets" in out and out.get("engine_samples") is not None else {}

    def _lost(self, pre: dict, post: dict) -> dict:
        """What the rewrite lost: a ring minute holding fewer rows, a bucket gone or changed.

        One way only, because the server keeps writing under the cap: a sampler batch lands
        rows stamped before it, and a minute's bucket is written a minute later under that
        minute's ts. A VACUUM can lose rows but never add one, so growth is a writer.
        Retention deletes from the old end meanwhile (Store._prune on every ring write, the
        minute tier on the first bucket write of a new hour), so slices below its floor, read
        now that the rewrite is done, are not compared. Everything above it still is.
        """
        from . import tiers
        now = time.time()
        ring = now - self.store.persist_s                       # Store._prune's cutoff
        floors = {"engine_samples": ring, "hw_samples": ring,
                  "counter_buckets": tiers.horizon_ts(now, self.store.minute_days)}
        out: dict = {}
        for table, floor in floors.items():
            a, b = pre.get(table), post.get(table)
            if a is None or b is None:
                if (a is None) != (b is None):
                    out[table] = "unreadable on one side"
                continue
            if table == "counter_buckets":
                bad = {f"{e}@{ts}": (v, b.get((e, ts))) for (e, ts), v in a.items()
                       if ts >= floor and b.get((e, ts)) != v}
            else:
                bad = {m: (n, b.get(m, 0)) for m, n in a.items()
                       if m >= floor and b.get(m, 0) < n}
            if bad:
                out[table] = dict(sorted(bad.items())[:5])      # a sample is enough to act on
        return out

    def _rollup(self) -> None:
        """Fold aged minutes into the hourly/daily ladder without deleting anything.

        The store folds on bucket writes, but a machine that goes quiet for weeks still needs
        its aggregates current — that is what makes the next prune verifiable, and what keeps
        long-history reads complete while the rings stay a day deep.
        """
        from . import tiers
        try:
            with self.store._lock:
                tiers.rollup_all(self.store._con)
        except Exception as exc:                       # noqa: BLE001 - never fatal
            log.warning("ladder rollup failed: %r", exc)

    def run_once(self, force: bool = False) -> dict:
        """Compact if (and only if) there is something worth reclaiming.

        Returns the store's compact() report, or a dict explaining why it declined — a silent
        no-op is how "it never compacted" becomes a mystery three months later.
        """
        self.status["last_run"] = time.time()
        self.status["runs"] += 1
        size = self._db_bytes()
        if not self.enabled and not force:
            self.status["note"] = "auto_compact is off"
            return {"vacuumed": False, "skipped": "disabled"}
        if size < self.min_bytes:
            self.status["note"] = f"database too small to bother ({size} B)"
            return {"vacuumed": False, "skipped": "small"}
        free_disk = shutil.disk_usage(os.path.dirname(
            getattr(self.store, "path", ".") or ".")).free
        if free_disk < size * self.disk_headroom:
            self.status["note"] = (f"skipped: needs ~{int(size * self.disk_headroom)} B free, "
                                   f"{free_disk} B available")
            return {"vacuumed": False, "skipped": "disk"}
        # VACUUM must not be able to lose anything, and "must not" is not the same as
        # "did not": snapshot the live row counts and money before rewriting, compare
        # after, and stand down for the rest of the session if anything moved. Same
        # discipline as scripts/limcars_tidy.py, which refuses and restores.
        cap = int(time.time())          # one frozen timestamp for both sides of the rewrite
        pre = self._invariants(cap)
        if not pre:
            # fail closed: without a "before" picture there is no way to prove the rewrite lost
            # nothing, so the rewrite does not happen. It will be tried again next pass.
            self.status["note"] = "compaction skipped: could not read the invariant baseline"
            return {"vacuumed": False, "skipped": "invariants-unavailable"}
        rep = self.store.compact(reclaim_ratio=self.reclaim_ratio)
        rep = dict(rep or {})
        post = self._invariants(cap)
        if not post:
            self.status["warning"] = "post-compaction invariants could not be read"
            self.status["note"] = "compaction not verified; automatic compaction disabled"
            self.enabled = False
            rep["invariant_unverified"] = True
            return rep
        drift = self._lost(pre, post)
        if drift:
            self.enabled = False                      # do not do it again while confused
            self.status["warning"] = f"rows changed across compaction: {drift}"
            self.status["note"] = "compaction halted: invariant check failed"
            rep["invariant_drift"] = drift
            return rep
        self.status.pop("warning", None)
        rep["bytes_before"] = rep.get("bytes_before") or size
        if not rep.get("vacuumed"):
            self.status["note"] = (f"freelist too small "
                                   f"({rep.get('free_pages', 0)} of {rep.get('pages', 0)} pages)")
            self.status["last_report"] = rep
            return rep
        freed = max(0, int(rep.get("bytes_before", 0)) - int(rep.get("bytes_after", 0)))
        self.status["last_vacuum"] = time.time()
        self.status["freed_bytes_total"] += freed
        self.status["last_report"] = rep
        self.status["note"] = f"reclaimed {freed} bytes"
        return rep

    # --- lifecycle ------------------------------------------------------------

    def run(self) -> None:
        # the boot pass is the one that matters after an update: the previous version left a
        # high-water mark behind, and the new retention rules have just made it reclaimable
        if self._stop.wait(self.start_delay):
            return
        try:
            self._rollup()                                  # fold first, at boot too
            self.run_once()
            thinned = (self.store.thin_rings() or {}).get("thinned", 0)
            self.status["last_thin"] = {"thinned": thinned}
            # The boot order is compact-then-thin, so the pages this thin frees are invisible to
            # the pass above (measured: 244k rows deleted, freelist still reported 16 pages, and
            # the file stayed 555 MB while 530 MB of it was dead). A thin this large rewrites
            # most of the file, so it earns its own compaction immediately — compact() still
            # refuses on its own guards.
            if thinned > 5000:
                self.status["note"] = f"post-thin compaction after {thinned} rows"
                self.run_once()
        except Exception as exc:                       # never take the dashboard down for tidy
            self.status["note"] = f"boot pass failed: {exc}"
        # Two cadences on one thread: thinning is cheap and wants to be frequent (otherwise the
        # full-resolution window grows a day deep again), compaction is a full file rewrite and
        # must stay on its own interval.
        while not self._stop.wait(min(self.interval, self.thin_interval)):
            try:
                thinned = (self.store.thin_rings() or {}).get("thinned", 0)
                # A thin can free hundreds of megabytes in one statement, and SQLite hands
                # those pages back to nobody until a VACUUM — so a big thin earns a compaction
                # pass immediately instead of waiting an hour for the schedule. compact() still
                # refuses on its own guards (reclaim ratio, disk headroom, minimum size).
                due = (time.time() - (self.status.get("last_run") or 0)) >= self.interval
                if due or thinned > 5000:
                    # fold first, then reclaim: the ladder is what makes the rings disposable
                    self._rollup()
                    self.run_once()
            except Exception as exc:
                self.status["note"] = f"scheduled pass failed: {exc}"

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        """What /api/storage shows. Numbers measured, decisions explained."""
        out = dict(self.status)
        out.update({"db_bytes": self._db_bytes(), "interval_s": self.interval,
                    "reclaim_ratio": self.reclaim_ratio})
        return out
