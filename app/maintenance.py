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
        """Row counts and money, restricted to rows at or before `cap`.

        The cap is the point. The server is writing rings twice a second and a bucket a
        minute while this runs, so comparing raw counts before/after a VACUUM would always
        "drift". Measuring both sides against one frozen timestamp makes it a real check.
        """
        con = getattr(self.store, "_con", None)
        out: dict = {}
        if con is None:
            return out
        lock = getattr(self.store, "_lock", None)
        try:
            if lock is not None:
                lock.acquire()
            for table, col in (("engine_samples", "t"), ("hw_samples", "t"),
                               ("counter_buckets", "ts")):
                try:
                    out[table] = con.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {col}<=?", (cap,)).fetchone()[0]
                except Exception:  # noqa: BLE001 — a missing table is not a drift signal
                    out[table] = None
            try:
                row = con.execute(
                    "SELECT COALESCE(SUM(prompt),0), COALESCE(SUM(cached),0),"
                    " COALESCE(SUM(gen),0), COALESCE(SUM(reqs),0),"
                    " COALESCE(SUM(cost_hot),0), COUNT(cost_hot) FROM counter_buckets"
                    " WHERE ts<=?", (cap,)).fetchone()
                for k, v in zip(("prompt", "cached", "gen", "reqs", "cost_hot", "priced"), row):
                    out[k] = round(float(v), 6) if isinstance(v, float) else v
            except Exception:  # noqa: BLE001
                pass
        finally:
            if lock is not None and lock.locked():
                lock.release()
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
        rep = self.store.compact(reclaim_ratio=self.reclaim_ratio)
        rep = dict(rep or {})
        post = self._invariants(cap)
        drift = {k: (pre[k], post[k]) for k in pre if pre[k] != post[k]}
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
            self.run_once()
        except Exception as exc:                       # never take the dashboard down for tidy
            self.status["note"] = f"boot pass failed: {exc}"
        while not self._stop.wait(self.interval):
            try:
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
