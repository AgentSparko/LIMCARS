"""Hardware sampler — 1 Hz system + GB10 GPU telemetry, in-process rings.

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

import os
import shutil
import subprocess
import threading
import time

import psutil  # only non-stdlib import in the whole backend

from .rings import Ring


def _nvidia_query() -> dict:
    if not shutil.which("nvidia-smi"):
        return {}
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,power.draw,"
             "clocks.current.sm,clocks.max.sm",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=0.9).stdout.strip()
    except Exception:  # noqa: BLE001
        return {}
    parts = [p.strip() for p in out.split(",")]
    if len(parts) < 5:
        return {}

    def f(i):
        try:
            return float(parts[i])
        except ValueError:
            return None
    clock = f(3) if f(3) not in (None, 0) else f(4)  # current SM, else max SM
    return {"gpu_util": f(0), "gpu_temp_c": f(1), "gpu_power_w": f(2),
            "gpu_clock_mhz": clock}


def _hwmon_temp() -> dict:
    """Best-effort package temps from /sys/class/hwmon (no `sensors` dep)."""
    temps: dict[str, float] = {}
    try:
        for hw in sorted("/sys/class/hwmon") and os.listdir("/sys/class/hwmon"):
            d = f"/sys/class/hwmon/{hw}"
            try:
                name = open(d + "/name").read().strip()
            except OSError:
                continue
            for label in os.listdir(d):
                if not label.endswith("_input") or not label.startswith(("temp", "cpu", "gpu", "soctherm")):
                    continue
                try:
                    val = int(open(f"{d}/{label}").read()) / 1000.0
                except (OSError, ValueError):
                    continue
                key = f"{name}_{label[:-6]}"
                temps.setdefault(key, val)
    except OSError:
        pass
    return temps


class HwSampler(threading.Thread):
    daemon = True

    def __init__(self, cfg: dict, broker, store=None):
        super().__init__(name="sampler-hw", daemon=True)
        self.cfg, self.broker, self.store = cfg, broker, store
        self.hz = float(cfg.get("hw_hz", 1.0))
        self.ring = Ring(float(cfg.get("ring_seconds", 900)))
        self._stop = threading.Event()
        self._last_net = None
        self._persist_acc: list[tuple[float, dict]] = []
        psutil.cpu_percent(interval=None)  # prime
        psutil.net_io_counters()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self, seconds: float | None = None) -> dict:
        rows = self.ring.all() if seconds is None else self.ring.window(seconds)
        keys = ("cpu_pct", "cpu_temp_c", "load1", "load5", "mem_used_gb", "mem_total_gb",
                "mem_avail_gb", "swap_used_gb", "gpu_util", "gpu_temp_c",
                "gpu_power_w", "gpu_clock_mhz", "disk_r_mbs", "disk_w_mbs",
                "net_r_mbs", "net_w_mbs")
        return {"hz": self.hz, "latest": self.ring.last(),
                "series": {k: [[t, r.get(k)] for t, r in rows] for k in keys}}

    def run(self) -> None:
        interval = 1.0 / max(0.25, self.hz)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                pass
            sleep = interval - (time.monotonic() - t0)
            if sleep > 0:
                self._stop.wait(sleep)

    def _tick(self) -> None:
        now = time.time()
        vm = psutil.virtual_memory()
        du = shutil.disk_usage("/")
        try:
            load1, load5, _ = os.getloadavg()
        except OSError:
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
        }
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

        row.update(_nvidia_query())
        temps = _hwmon_temp()
        # normalized keys the Classic dash sidebar charts expect
        cpu_t = next((v for k, v in temps.items() if "acpitz" in k or "cpu" in k.lower()), None)
        if cpu_t is not None:
            row["cpu_temp_c"] = round(cpu_t, 1)
        for k, v in list(temps.items())[:6]:
            row[f"temp_{k}"] = round(v, 1)

        self.ring.append(row, now)
        import json
        self.broker.publish("hw", json.dumps({"t": now, **row}))
        if self.store:
            self._persist_acc.append((now, row))
            if len(self._persist_acc) >= 10:
                rows, self._persist_acc = self._persist_acc, []
                self.store.save_hw_samples(rows)
