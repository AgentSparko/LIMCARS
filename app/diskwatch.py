"""Disk watchdog — threshold alerting on the TRUE free number + critical scan.

Born from the 2026-11-09 disk-full crash: / hit 0 bytes at 22:32 and the box
was unbootable for ~7 hours. The tile now tells the truth (f_bavail, commit
45ca18d) — this module makes the dashboard WATCH it: warn below 10 GiB,
critical below 5 GiB, and at critical a read-only scan names the largest
reclaimable-LOOKING paths under a root so whoever is at the screen sees where
the space could come from. Nothing here ever deletes, moves or truncates
anything — sizes only, and the UI labels the result "looks deletable", not
"is deletable".

Thresholds come from config (disk_warn_gib / disk_crit_gib); the scan runs
only while critical, at most once per scan_ttl, in a background thread with a
hard time budget — a watchdog that itself hogs the disk or CPU is a worse
outage than the one it watches.
"""
from __future__ import annotations

import os
import re
import threading
import time

WARN_GIB = 10.0
CRIT_GIB = 5.0

# Directory names that read as reclaimable to a human at a glance: caches,
# trash, temp, downloads, logs, and node_modules (reinstallable). Matched on
# the BASENAME at any depth under the root; sizes are measured exactly (a
# full du of the candidate), never guessed.
_DELETABLE_RE = re.compile(
    r"^(cache|caches|\.cache|tmp|temp|trash|\.trash|trash-\d+|downloads"
    r"|node_modules|__pycache__|logs?|backups?|\.npm|\.pnpm|\.yarn|\.uv|\.cargo"
    r"|\.pip|\.nuget|\.docker|snap)$", re.I)
# Well-known homes even when the basename alone would not match the regex
# above at the scanned depth (dotdirs are skipped by the name walk).
_KNOWN = (".cache", ".local/share/Trash", ".npm", ".uv", ".cargo", ".docker",
          "Downloads", ".hermes/cache", ".dsh/cache")


def disk_alert(free_gib, warn: float = WARN_GIB, crit: float = CRIT_GIB):
    """'ok' | 'warn' | 'critical' | None (None = no reading; never guess)."""
    if free_gib is None:
        return None
    if free_gib < crit:
        return "critical"
    if free_gib < warn:
        return "warn"
    return "ok"


def _du(path: str, deadline: float) -> tuple[int, bool]:
    """Bytes under `path` (following no symlinks), stopping at the deadline.
    Returns (bytes, complete). Permission errors read as 0, not as a crash."""
    total = 0
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            return total, False
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, True


def largest_deletable(root: str = os.path.expanduser("~"), top: int = 3,
                      budget_s: float = 8.0, max_depth: int = 4) -> dict:
    """Top `top` reclaimable-LOOKING directories under `root`, by exact size.

    Two passes, both read-only and budgeted: (1) the well-known homes, (2) a
    depth-limited name walk for cache/trash/tmp/logs/node_modules-shaped dirs.
    Returns {"items": [{"path", "gib"}], "partial": bool, "scanned_s": float}.
    """
    t0 = time.monotonic()
    deadline = t0 + budget_s
    seen: set[str] = set()
    cands: list[str] = []

    def add(p: str):
        p = os.path.normpath(p)
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            cands.append(p)

    for k in _KNOWN:
        add(os.path.join(root, k))
    # breadth-ish name walk: shallow first, so a big ~/.cache beats a deep
    # node_modules inside a project the owner is mid-edit on
    frontier = [root]
    for _ in range(max_depth):
        if time.monotonic() > deadline:
            break
        nxt: list[str] = []
        for d in frontier:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if time.monotonic() > deadline:
                            break
                        try:
                            if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if _DELETABLE_RE.search(e.name):
                            add(e.path)
                        else:
                            nxt.append(e.path)
            except OSError:
                continue
        frontier = nxt
    items, partial = [], False
    for c in cands:
        if time.monotonic() > deadline:
            partial = True
            break
        size, complete = _du(c, deadline)
        if not complete:
            partial = True
        if size > 0:
            items.append({"path": c, "gib": round(size / 2**30, 2)})
    items.sort(key=lambda x: -x["gib"])
    return {"items": items[:top], "partial": partial,
            "scanned_s": round(time.monotonic() - t0, 1)}


class DiskWatch:
    """State holder used by the hw sampler: alert per tick, scan only while
    critical, at most once per scan_ttl, off-thread."""

    def __init__(self, cfg: dict, root: str = os.path.expanduser("~"), scan_ttl: float = 600.0,
                 budget_s: float = 8.0):
        self.warn = float(cfg.get("disk_warn_gib", WARN_GIB))
        self.crit = float(cfg.get("disk_crit_gib", CRIT_GIB))
        self.root, self.scan_ttl, self.budget_s = root, scan_ttl, budget_s
        self.state: str | None = None
        self.scan: dict | None = None
        self._scan_at = 0.0
        self._busy = False
        self._lock = threading.Lock()

    def tick(self, free_gib) -> dict:
        """Fold one free-space reading into the watch; returns the row keys."""
        self.state = disk_alert(free_gib, self.warn, self.crit)
        row = {"disk_alert": self.state,
               "disk_warn_gib": self.warn, "disk_crit_gib": self.crit}
        if self.state == "critical":
            now = time.time()
            with self._lock:
                stale = (not self._busy) and (now - self._scan_at >= self.scan_ttl)
                if stale:
                    self._busy = True
                    self._scan_at = now
            if stale:
                threading.Thread(target=self._refresh, daemon=True,
                                 name="diskwatch-scan").start()
            if self.scan:
                row["disk_alert_top"] = self.scan
        return row

    def _refresh(self):
        try:
            self.scan = largest_deletable(self.root, budget_s=self.budget_s)
        finally:
            with self._lock:
                self._busy = False