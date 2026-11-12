"""Voice-lane memory gates: the guard lines from the 2026-11-10 build-phase
resource verdict, as a small reusable module the voice bot's launcher calls.

Two guards, one measurement (MemAvailable from /proc/meminfo — the SYSTEM
number, not process RSS: on GB10 the model's GPU side lives in the unified
pool and RSS under-reports the real footprint ~4x, measured 2026-11-10:
RSS 1.0 GiB vs system delta 4.6-4.8 GiB):

1. LOAD GATE  — refuse a model load unless MemAvailable >= 10 GiB *at that
   moment*. The gate exists because the floor swings several GiB with engine
   load (measured 17.4 GiB idle vs 13.5 GiB at 3 requests in flight on
   :8020); the worst-case post-load floor must stay above the 5 GiB
   box hard-stop.

2. RUNTIME WATCHDOG — MemAvailable < 6 GiB sustained for 30 s fires a
   callback once (the launcher's unload / refuse-new-round-trips hook).
   Sustained, not instantaneous: a 20 s dip must not unload a healthy
   model. Hysteresis: after firing, the watchdog stays tripped until
   MemAvailable recovers to the recover line (default 7 GiB), so
   oscillation around the low line can never re-fire the callback.

Fail-closed: an unreadable /proc/meminfo refuses the LOAD GATE (no number,
no load) but never counts as a watchdog sample (no number, no trip).

CLI for the launcher (exit-code style):
    python3 -m app.voice_gates check-load   # 0 allow, 1 refuse
    python3 -m app.voice_gates watch        # runs until trip, then exit 3

Thresholds follow the house config layering (defaults < data/config.json
< LIMCARS_* env); CLI flags override everything for one invocation.
"""
from __future__ import annotations

import argparse
import sys
import time

# Module defaults = the verdict numbers; config keys (app/config.py
# DEFAULTS) and explicit call arguments override them.
GATE_LOAD_GIB = 10.0        # LOAD GATE: refuse below this MemAvailable
WATCH_LOW_GIB = 6.0         # WATCHDOG: low line
WATCH_SECS = 30.0           # WATCHDOG: how long it must stay low
WATCH_RECOVER_GIB = 7.0     # WATCHDOG: rearm only after rising back to here

MEMINFO = "/proc/meminfo"


def read_memavail_gib(path: str = MEMINFO) -> float | None:
    """MemAvailable in GiB, or None if the file is missing/unparsable."""
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 2**20   # kB -> GiB
    except (OSError, ValueError, IndexError):
        pass
    return None


def check_load(min_gib: float = GATE_LOAD_GIB, meminfo: str = MEMINFO) -> dict:
    """LOAD GATE verdict: {"ok", "mem_avail_gib", "reason"}. Fail-closed —
    no measurement means no load."""
    avail = read_memavail_gib(meminfo)
    if avail is None:
        return {"ok": False, "mem_avail_gib": None,
                "reason": f"refuse: MemAvailable unreadable ({meminfo}) — "
                          f"fail closed, no load without a measurement"}
    ok = avail >= min_gib
    rel, word = (">=", "allow") if ok else ("<", "refuse")
    return {"ok": ok, "mem_avail_gib": round(avail, 2),
            "reason": f"{word}: MemAvailable {avail:.2f} GiB {rel} "
                      f"{min_gib:g} GiB load gate"}


class Watchdog:
    """Feed it samples: `sample(mem_gib, t=None)` returns True on the tick
    that fires (once per trip). Fires when MemAvailable stays below
    `low_gib` for `secs`; stays tripped — silently — until a sample
    reaches `recover_gib` rearms it. `callback(reason)` runs on the fire.
    A None sample (unreadable meminfo) neither accumulates nor resets the
    sustained timer: no number, no trip."""

    def __init__(self, low_gib: float = WATCH_LOW_GIB, secs: float = WATCH_SECS,
                 recover_gib: float = WATCH_RECOVER_GIB, callback=None):
        if recover_gib <= low_gib:
            # hysteresis is meaningless (or a flap machine) without a
            # recover line ABOVE the low line — refuse the config loudly
            raise ValueError(
                f"recover_gib {recover_gib} must be above low_gib {low_gib}")
        self.low, self.secs, self.recover = low_gib, secs, recover_gib
        self.callback = callback
        self.tripped = False
        self.last_reason: str | None = None
        self.below_since: float | None = None

    def sample(self, mem_gib: float | None, t: float | None = None) -> bool:
        if t is None:
            t = time.monotonic()
        if self.tripped:
            if mem_gib is not None and mem_gib >= self.recover:
                self.tripped = False          # hysteresis: rearm on recovery
                self.below_since = None
            return False
        if mem_gib is None:
            return False
        if mem_gib < self.low:
            if self.below_since is None:
                self.below_since = t
            if t - self.below_since >= self.secs:
                self.tripped = True
                reason = (f"trip: MemAvailable {mem_gib:.2f} GiB below "
                          f"{self.low:g} GiB for {t - self.below_since:.0f} s "
                          f"(>= {self.secs:g} s) — unload / refuse round-trips")
                self.last_reason = reason
                if self.callback:
                    try:
                        self.callback(reason)
                    except Exception:  # noqa: BLE001 — a launcher hook that
                        pass           # throws must not kill the watchdog
                return True
            return False
        self.below_since = None               # back above the line: timer resets
        return False


def _thresholds(cfg: dict | None = None) -> dict:
    """Gate thresholds with the house layering: module defaults < config."""
    from . import config
    src = cfg if cfg is not None else config.load()
    return {"load": float(src.get("voice_gate_load_gib", GATE_LOAD_GIB)),
            "low": float(src.get("voice_gate_watch_low_gib", WATCH_LOW_GIB)),
            "secs": float(src.get("voice_gate_watch_secs", WATCH_SECS)),
            "recover": float(src.get("voice_gate_watch_recover_gib",
                                     WATCH_RECOVER_GIB))}


def _watch_loop(wd: "Watchdog", meminfo: str, interval: float,
                heartbeat: float = 60.0, max_ticks: int | None = None) -> int:
    """The CLI watch loop, factored out so pins can bound it. Prints a
    heartbeat line every `heartbeat` seconds while silent — a watchdog
    nobody sees alive is indistinguishable from a hung one. Returns 3 on
    trip, 0 only when max_ticks is reached (the real loop runs until trip)."""
    last_hb = time.monotonic()
    ticks = 0
    while True:
        avail = read_memavail_gib(meminfo)
        if wd.sample(avail):
            # flush=True: a launcher consuming this via a pipe gets block-
            # buffered stdout — without an explicit flush the trip line
            # sits in the buffer until exit and the heartbeat is pointless.
            print(getattr(wd, "last_reason", "trip"), flush=True)
            return 3
        now = time.monotonic()
        if heartbeat > 0 and now - last_hb >= heartbeat:
            last_hb = now
            print(f"ok: MemAvailable {avail if avail is not None else '?'} GiB "
                  f"(watchdog armed: trip < {wd.low:g} GiB sustained "
                  f"{wd.secs:g} s, rearm >= {wd.recover:g} GiB)", flush=True)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return 0
        time.sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m app.voice_gates")
    ap.add_argument("cmd", choices=["check-load", "watch"])
    ap.add_argument("--min", type=float, help="load gate floor GiB (default from config)")
    ap.add_argument("--low", type=float, help="watchdog low line GiB")
    ap.add_argument("--secs", type=float, help="watchdog sustained seconds")
    ap.add_argument("--recover", type=float, help="watchdog rearm line GiB")
    ap.add_argument("--interval", type=float, default=1.0, help="watch sample seconds")
    ap.add_argument("--heartbeat", type=float, default=60.0,
                    help="watch: print an alive line every N seconds (0 = off)")
    ap.add_argument("--meminfo", default=MEMINFO,
                    help="meminfo path (127.0.0.1-style escape hatch for tests)")
    a = ap.parse_args(argv)
    th = _thresholds()
    if a.cmd == "check-load":
        v = check_load(a.min if a.min is not None else th["load"], a.meminfo)
        print(v["reason"])
        return 0 if v["ok"] else 1
    try:
        wd = Watchdog(a.low if a.low is not None else th["low"],
                      a.secs if a.secs is not None else th["secs"],
                      a.recover if a.recover is not None else th["recover"])
    except ValueError as e:
        # bad thresholds (from flags OR config) are a usage error, not a
        # crash: one clean line on stderr, exit 1 — no traceback for the
        # launcher's log. The constructor keeps raising for library users.
        print(f"voice_gates: {e}", file=sys.stderr)
        return 1
    return _watch_loop(wd, a.meminfo, a.interval, a.heartbeat)


if __name__ == "__main__":
    sys.exit(main())