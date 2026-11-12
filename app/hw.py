"""Hardware sampler — 1 Hz system + GB10 GPU telemetry, in-process rings.

Discrete-GPU boxes (memory.total reported) also get vram_used/total_gb and,
with 2+ cards, per-card gpuN_* keys; see _gpu_row.

Notes specific to DGX Spark (GB10, unified memory):
  * nvidia-smi memory.used/total read [N/A] on GB10 — we do NOT fake it;
    unified memory shows up in system memavail instead.
  * utilization.gpu is bursty for back-to-back queries; one query per
    second is the proven-safe cadence (old hw_services hammered it).
  * CPU/package temp comes from hwmon (acpitz) — sensors CLI not required.

The ring feeds BOTH the SSE `hw` topic and the sqlite downsample, so charts
keep history even with no browser attached (the old bug class).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

import psutil  # only non-stdlib import in the whole backend

from .rings import Ring
from .diskwatch import DiskWatch
from . import voice


_NV_FIELDS = ("index,utilization.gpu,temperature.gpu,power.draw,power.limit,"
              "clocks.current.sm,clocks.max.sm,memory.used,memory.total,name")


def _parse_nvidia(out: str) -> list[dict]:
    """One dict per GPU from `nvidia-smi --query-gpu=_NV_FIELDS` CSV.
    [N/A] reads as None (GB10 reports no memory.* — unified memory)."""
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",", 9)]
        if len(parts) < 10:
            continue

        def f(i):
            try:
                return float(parts[i])
            except ValueError:
                return None
        clock = f(5) if f(5) not in (None, 0) else f(6)  # current SM, else max SM
        mem_used, mem_total = f(7), f(8)
        gpus.append({
            "index": int(f(0)) if f(0) is not None else len(gpus), "name": parts[9],
            "util": f(1), "temp_c": f(2), "power_w": f(3), "power_limit_w": f(4),
            "clock_mhz": clock,
            "mem_used_gb": round(mem_used / 1024, 2) if mem_used is not None and mem_total else None,
            "mem_total_gb": round(mem_total / 1024, 2) if mem_total else None,
        })
    return gpus


def _gpu_row(gpus: list[dict]) -> dict:
    """Flatten per-GPU readings into the hw row. Legacy gpu_* keys keep their
    single-GPU meaning; with several cards they become box totals (power),
    worst case (temp) and means (util, clock). gpuN_* keys carry each card."""
    if not gpus:
        return {}

    def vals(k):
        return [g[k] for g in gpus if g[k] is not None]

    def mean(v):
        return round(sum(v) / len(v), 1) if v else None
    if len(gpus) == 1:
        g = gpus[0]
        row = {"gpu_util": g["util"], "gpu_temp_c": g["temp_c"], "gpu_power_w": g["power_w"],
               "gpu_clock_mhz": g["clock_mhz"]}
    else:
        p = vals("power_w")
        row = {"gpu_util": mean(vals("util")), "gpu_temp_c": max(vals("temp_c"), default=None),
               "gpu_power_w": round(sum(p), 1) if p else None,
               "gpu_clock_mhz": mean(vals("clock_mhz"))}
    row["gpu_count"] = len(gpus)
    lim = vals("power_limit_w")
    row["gpu_power_limit_w"] = round(sum(lim), 1) if len(lim) == len(gpus) else None
    # discrete VRAM only when every card reports it; unified (GB10) stays absent
    if all(g["mem_total_gb"] and g["mem_used_gb"] is not None for g in gpus):
        row["vram_used_gb"] = round(sum(vals("mem_used_gb")), 2)
        row["vram_total_gb"] = round(sum(vals("mem_total_gb")), 2)
    if len(gpus) > 1:
        for i, g in enumerate(gpus):
            for k in ("util", "temp_c", "power_w", "clock_mhz", "mem_used_gb", "mem_total_gb"):
                if g[k] is not None:
                    row[f"gpu{i}_{k}"] = g[k]
            if g["mem_total_gb"] and g["mem_used_gb"] is not None:
                row[f"gpu{i}_mem_pct"] = round(100 * g["mem_used_gb"] / g["mem_total_gb"], 1)
    return row


def _nvidia_query() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv
            ["nvidia-smi", "--query-gpu=" + _NV_FIELDS, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=0.9).stdout
    except Exception:  # noqa: BLE001
        return []
    return _parse_nvidia(out)


def _disk_free_bytes(path: str) -> int:
    """Bytes an unprivileged process can still write on `path`.

    statvfs f_bavail, NOT f_bfree: f_bfree includes the filesystem's
    root-reserved blocks (ext4 default reserves ~4-5% that ordinary users
    cannot write), so any free-space figure built on it — or on
    shutil.disk_usage().free, which is total-used = f_bfree — over-reports
    by exactly the reserve. A disk guard must count what the guard's own
    user can allocate. Falls back to shutil's figure if statvfs fails.
    """
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except (OSError, AttributeError):
        return shutil.disk_usage(path).free


def _ssd_disk() -> str | None:
    """Block device holding / (e.g. nvme0n1), or $LIMCARS_SSD_DEV."""
    dev = os.environ.get("LIMCARS_SSD_DEV")
    if dev:
        return dev
    try:
        src = next((ln.split()[0] for ln in open("/proc/mounts", encoding="utf-8")
                    if ln.split()[1:2] == ["/"] and ln.startswith("/dev/")), None)
    except OSError:
        return None
    if not src:
        return None
    name = os.path.basename(os.path.realpath(src))
    part = f"/sys/class/block/{name}/partition"
    if os.path.exists(part):
        name = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{name}")))
    return name


def _ssd_bytes(disk: str) -> tuple[int, int] | None:
    """(read, written) bytes since boot for one disk; /proc/diskstats sectors are 512 B."""
    try:
        for ln in open("/proc/diskstats", encoding="utf-8"):
            f = ln.split()
            if len(f) > 9 and f[2] == disk:
                return int(f[5]) * 512, int(f[9]) * 512
    except OSError:
        pass
    return None


def _ssd_temps(disk: str) -> dict:
    """NVMe composite temperature plus its warning (max) and critical limits, °C."""
    out: dict[str, float] = {}
    base = f"/sys/class/block/{disk}/device"
    try:
        hws = [h for h in os.listdir(base) if h.startswith("hwmon")]
    except OSError:
        return out
    for h in hws:
        for k, f in (("temp", "temp1_input"), ("max", "temp1_max"), ("crit", "temp1_crit")):
            try:
                out[k] = int(open(f"{base}/{h}/{f}", encoding="utf-8").read()) / 1000.0
            except (OSError, ValueError):
                pass
    return out


def _boot_id() -> str:
    try:
        return open("/proc/sys/kernel/random/boot_id", encoding="utf-8").read().strip()
    except OSError:
        return ""


def _hwmon_temp() -> dict:
    """Best-effort package temps from /sys/class/hwmon (no `sensors` dep)."""
    temps: dict[str, float] = {}
    try:
        for hw in sorted("/sys/class/hwmon") and os.listdir("/sys/class/hwmon"):
            d = f"/sys/class/hwmon/{hw}"
            try:
                name = open(d + "/name", encoding="utf-8", errors="replace").read().strip()
            except OSError:
                continue
            for label in os.listdir(d):
                if not label.endswith("_input") or not label.startswith(("temp", "cpu", "gpu", "soctherm")):
                    continue
                try:
                    val = int(open(f"{d}/{label}", encoding="utf-8").read()) / 1000.0
                except (OSError, ValueError):
                    continue
                key = f"{name}_{label[:-6]}"
                temps.setdefault(key, val)
    except OSError:
        pass
    return temps


_BASE_KEYS = ("cpu_pct", "cpu_temp_c", "load1", "load5", "mem_used_gb", "mem_total_gb",
              "mem_avail_gb", "swap_used_gb", "gpu_util", "gpu_temp_c",
              "gpu_power_w", "gpu_clock_mhz", "disk_r_mbs", "disk_w_mbs",
              "net_r_mbs", "net_w_mbs", "ssd_r_mbs", "ssd_w_mbs", "ssd_temp_c")


def series_keys(last: dict) -> tuple:
    """Charted keys: the fixed set plus whatever gpu*/vram* keys this box has."""
    # gpu_count is a card count, not a time series. The prefix match below sweeps
    # up any gpu*/vram* key this box reports, and gpu_count starts with "gpu", so
    # every hardware panel grew a flat line that reads like a stalled GPU.
    return _BASE_KEYS + tuple(k for k in last
                              if k.startswith(("gpu", "vram"))
                              and k not in _BASE_KEYS and k != "gpu_count")


class HwSampler(threading.Thread):
    daemon = True

    def __init__(self, cfg: dict, broker, store=None):
        super().__init__(name="sampler-hw", daemon=True)
        self.cfg, self.broker, self.store = cfg, broker, store
        self.hz = float(cfg.get("hw_hz", 1.0))
        self.ring = Ring(float(cfg.get("ring_seconds", 900)))
        self._stop_ev = threading.Event()
        self._last_net = None
        self._persist_acc: list[tuple[float, dict]] = []
        self.ssd_disk = _ssd_disk()
        # disk watchdog: alert states on the f_bavail reading + critical scan
        # (app/diskwatch.py). Thresholds are config keys, not env knobs.
        self.diskwatch = DiskWatch(cfg)
        self.ssd_limits: dict = {}
        self._ssd_prev = None          # (t, read, written) at the last tick
        self._ssd_day_prev = None      # (read, written) at the last per-day flush
        self._ssd_day_t = 0.0
        self.gpus: list[dict] = []   # static identity per card: index, name, vram
        # voice-lane monitor (app/voice.py): off unless voice_port is configured
        self.voice_port = int(cfg.get("voice_port") or 0)
        self.voice_latency_url = cfg.get("voice_latency_url") or ""
        self._voice_pid: int | None = None
        self._voice_base: float | None = None    # MemAvailable GiB at first sight
        self._voice_boot: bool | None = None     # None until first voice tick
        self._voice_lat: float | None = None
        self._voice_lat_age = 0
        psutil.cpu_percent(interval=None)  # prime
        psutil.net_io_counters()

    def stop(self) -> None:
        self._stop_ev.set()

    def snapshot(self, seconds: float | None = None) -> dict:
        rows = self.ring.all() if seconds is None else self.ring.window(seconds)
        keys = series_keys(self.ring.last() or {})
        return {"hz": self.hz, "latest": self.ring.last(), "gpus": self.gpus,
                "series": {k: [[t, r.get(k)] for t, r in rows] for k in keys}}

    def _ssd_tick(self, now: float, row: dict) -> None:
        if not self.ssd_disk:
            return
        b = _ssd_bytes(self.ssd_disk)
        if b is None:
            return
        if self._ssd_prev is not None:
            dt = now - self._ssd_prev[0]
            dr, dw = b[0] - self._ssd_prev[1], b[1] - self._ssd_prev[2]
            if dt > 0 and dr >= 0 and dw >= 0:
                row["ssd_r_mbs"] = round(dr / dt / 2**20, 2)
                row["ssd_w_mbs"] = round(dw / dt / 2**20, 2)
        self._ssd_prev = (now, b[0], b[1])
        t = _ssd_temps(self.ssd_disk)
        if "temp" in t:
            row["ssd_temp_c"] = round(t["temp"], 1)
        self.ssd_limits = {k: t[k] for k in ("max", "crit") if k in t}
        # per-day totals: flushed once a minute; the raw counters are saved with the boot
        # id, so a LIMCARS restart still books the bytes moved while it was down
        if self.store and now - self._ssd_day_t >= 60.0:
            self._ssd_day_t = now
            self.store.ssd_add(now, b[0], b[1], _boot_id())

    def ssd_report(self) -> dict:
        """Whole-drive view: last hour from the 1 s ring, per day from the store."""
        rows = self.ring.window(3600.0)
        r = w = 0.0
        prev_t = None
        for t, x in rows:
            if prev_t is not None and x.get("ssd_r_mbs") is not None:
                dt = min(t - prev_t, 5.0)
                r += x["ssd_r_mbs"] * dt
                w += (x.get("ssd_w_mbs") or 0.0) * dt
            prev_t = t
        span = (rows[-1][0] - rows[0][0]) if len(rows) > 1 else 0.0
        last = self.ring.last() or {}
        b = _ssd_bytes(self.ssd_disk) if self.ssd_disk else None
        return {"disk": self.ssd_disk, "temp_c": last.get("ssd_temp_c"),
                "temp_max_c": self.ssd_limits.get("max"), "temp_crit_c": self.ssd_limits.get("crit"),
                "hour": {"read_gb": round(r / 1024, 2), "written_gb": round(w / 1024, 2),
                         "span_s": round(span)},
                "since_boot": b and {"read_gb": round(b[0] / 2**30, 1), "written_gb": round(b[1] / 2**30, 1)},
                "days": self.store.ssd_days(30) if self.store else []}

    def _voice_tick(self, row: dict) -> None:
        """Voice-lane tile fields (app/voice.py). Zero work when the
        feature is off; when on, one /proc/net/tcp read per tick plus a
        cached-pid revalidation — the full pids scan only on cache miss.
        voice_delta_gb is the honest GB10 footprint: MemAvailable drop
        against the reading of the tick the process was first seen (GPU
        allocations live in the unified pool, invisible to RSS). No
        pre-spawn reading (LIMCARS booted with the voice stack already
        up) -> None, never an invented baseline."""
        if not self.voice_port:
            return
        pid = voice.find_voice_pid(self.voice_port, self._voice_pid)
        if pid is None:
            self._voice_pid = None
            self._voice_base = None
            self._voice_boot = False       # next appearance IS a watched spawn
            self._voice_lat = None
            self._voice_lat_age = 0
            row.update({"voice_alive": 0, "voice_pid": None, "voice_rss_gb": None,
                        "voice_delta_gb": None, "voice_latency_ms": None,
                        "voice_latency_stale": 0})
            return
        if pid != self._voice_pid:
            # baseline only for a spawn we WATCHED (an off tick came first);
            # a pid already running on our very first tick has no pre-spawn
            # reading on this dashboard's watch -> delta stays None
            watched = self._voice_boot is False
            self._voice_base = row.get("mem_avail_gb") if watched else None
            self._voice_pid = pid
            self._voice_lat = None          # previous process's latency is
            self._voice_lat_age = 0         # not this process's figure
        self._voice_boot = True
        base, avail = self._voice_base, row.get("mem_avail_gb")
        row.update({
            "voice_alive": 1,
            "voice_pid": pid,
            "voice_rss_gb": voice.rss_gb(pid),
            "voice_delta_gb": (round(base - avail, 2)
                               if base is not None and avail is not None else None),
        })
        lat = voice.read_latency(self.voice_latency_url)
        if lat is not None:
            self._voice_lat, self._voice_lat_age = lat, 0
        elif self._voice_lat is not None:
            self._voice_lat_age += 1
            if self._voice_lat_age > voice.LAT_HOLD_TICKS:
                self._voice_lat = None      # stale, not frozen
        row["voice_latency_ms"] = self._voice_lat
        # held-value marker: the figure is from an older tick while the
        # endpoint is quiet — the tile shows it prefixed "~" so a held
        # number never reads as a fresh measurement
        row["voice_latency_stale"] = int(self._voice_lat is not None
                                         and self._voice_lat_age > 0)

    def run(self) -> None:
        interval = 1.0 / max(0.25, self.hz)
        while not self._stop_ev.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                pass
            sleep = interval - (time.monotonic() - t0)
            if sleep > 0:
                self._stop_ev.wait(sleep)

    def _tick(self) -> None:
        now = time.time()
        vm = psutil.virtual_memory()
        du = shutil.disk_usage("/")
        # TRUE writable free: shutil.disk_usage's free is statvfs f_bfree,
        # which counts ext4's root-reserved blocks as writable — on this box
        # that over-reported free by ~36.8 GiB (measured 2026-11-09, the day
        # after the disk-full crash). f_bavail is what an unprivileged writer
        # can actually allocate; it is what df's Avail column shows.
        disk_free_gb = round(_disk_free_bytes("/") / 2**30, 2)
        try:
            load1, load5, _ = os.getloadavg()
        except (OSError, AttributeError):   # Windows has no load average: "–"
            load1 = load5 = None
        dio = psutil.disk_io_counters()
        nio = psutil.net_io_counters()
        row: dict[str, float | None] = {
            "cpu_pct": psutil.cpu_percent(interval=None),
            "load1": load1, "load5": load5,
            "mem_used_gb": round((vm.total - vm.available) / 2**30, 2),
            "mem_total_gb": round(vm.total / 2**30, 2),
            "mem_avail_gb": round(vm.available / 2**30, 2),
            "swap_used_gb": round(psutil.swap_memory().used / 2**30, 2),
            "disk_used_gb": round(du.used / 2**30, 2),
            "disk_total_gb": round(du.total / 2**30, 2),
            "disk_free_gb": disk_free_gb,
        }
        # watchdog keys ride the same row: SSE, ring and sqlite all carry them
        row.update(self.diskwatch.tick(disk_free_gb))
        # throughput rates from cumulative io counters
        if (self._last_net is not None and dio is not None
                and self._last_net[1] is not None and self._last_net[2] is not None):
            dt = now - self._last_net[0]
            if dt > 0:
                (p_dio, p_nio) = (self._last_net[1], self._last_net[2])
                # audit: NIC/disk re-init rewinds psutil counters to 0 →
                # negative deltas → impossible negative MB/s on the chart.
                # A counter reset reads as no-sample for this tick.
                dr = dio.read_bytes - p_dio.read_bytes
                dw = dio.write_bytes - p_dio.write_bytes
                nr = nio.bytes_recv - p_nio.bytes_recv
                nw = nio.bytes_sent - p_nio.bytes_sent
                if min(dr, dw, nr, nw) >= 0:
                    row["disk_r_mbs"] = round(dr / dt / 2**20, 2)
                    row["disk_w_mbs"] = round(dw / dt / 2**20, 2)
                    row["net_r_mbs"] = round(nr / dt / 2**20, 2)
                    row["net_w_mbs"] = round(nw / dt / 2**20, 2)
        self._last_net = (now, dio, nio)

        gpus = _nvidia_query()
        self.gpus = [{"index": g["index"], "name": g["name"], "vram_gb": g["mem_total_gb"]}
                     for g in gpus]
        row.update(_gpu_row(gpus))
        temps = _hwmon_temp()
        # normalized keys the Classic dash sidebar charts expect
        cpu_t = next((v for k, v in temps.items() if "acpitz" in k or "cpu" in k.lower()), None)
        if cpu_t is not None:
            row["cpu_temp_c"] = round(cpu_t, 1)
        for k, v in list(temps.items())[:6]:
            row[f"temp_{k}"] = round(v, 1)

        self._ssd_tick(now, row)
        self._voice_tick(row)

        self.ring.append(row, now)
        self.broker.publish("hw", json.dumps({"t": now, **row}))
        if self.store:
            self._persist_acc.append((now, row))
            if len(self._persist_acc) >= 10:
                rows, self._persist_acc = self._persist_acc, []
                self.store.save_hw_samples(rows)
