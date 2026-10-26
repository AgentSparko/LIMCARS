"""Agentless cluster-node hardware sampling over SSH.

Each entry in config `nodes` ({"id": "spark-2", "ssh": "user@host"}) gets one
thread that, once per second, pipes a tiny POSIX sh script to `ssh <target>
sh -s` and parses /proc, df, hwmon and nvidia-smi output into the same row
keys as the local HwSampler. Nothing is installed on the node: it needs sshd
and key-based login from this box (BatchMode — never prompts). One persistent
ControlMaster connection per node keeps the per-tick cost to one exec.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time

from . import config
from .hw import _NV_FIELDS, _gpu_row, _parse_nvidia, series_keys
from .rings import Ring

NODE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,48}")

SCRIPT = f"""echo @@stat; head -n 1 /proc/stat
echo @@mem; cat /proc/meminfo
echo @@load; cat /proc/loadavg
echo @@net; cat /proc/net/dev
echo @@df; df -Pk /
echo @@temp
for f in /sys/class/hwmon/hwmon*/temp*_input; do
  [ -r "$f" ] && echo "$(cat "${{f%/*}}/name" 2>/dev/null) $(basename "$f" _input) $(cat "$f")"
done
echo @@gpu
command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu={_NV_FIELDS} --format=csv,noheader,nounits 2>/dev/null
exit 0
"""


def _sections(out: str) -> dict[str, list[str]]:
    sec: dict[str, list[str]] = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = sec.setdefault(line[2:].strip(), [])
        elif cur is not None and line.strip():
            cur.append(line)
    return sec


class NodeSampler(threading.Thread):
    daemon = True

    def __init__(self, node: dict, cfg: dict, broker):
        self.id = node["id"]
        super().__init__(name=f"sampler-node-{self.id}", daemon=True)
        self.target = node["ssh"]
        self.extra = [str(a) for a in node.get("ssh_opts", [])]
        self.broker = broker
        self.topic = f"hw:{self.id}"
        self.hz = min(1.0, float(node.get("hz", cfg.get("hw_hz", 1.0))))
        self.ring = Ring(float(cfg.get("ring_seconds", 900)))
        self.alive = False
        self.error: str | None = None
        self.gpus: list[dict] = []
        self._prev: tuple | None = None   # (t, cpu_total, cpu_idle, rx, tx)
        self._stop = threading.Event()
        ctl = config.DATA / "ssh"
        ctl.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._ctl = str(ctl / "%C")

    def stop(self) -> None:
        self._stop.set()

    def argv(self) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                "-o", "ServerAliveInterval=5", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._ctl}", "-o", "ControlPersist=60",
                *self.extra, "--", self.target, "sh", "-s"]

    def snapshot(self, seconds: float | None = None) -> dict:
        rows = self.ring.all() if seconds is None else self.ring.window(seconds)
        last = self.ring.last() or {}
        return {"target": self.target, "alive": self.alive, "error": self.error,
                "hz": self.hz, "latest": last, "gpus": self.gpus,
                "series": {k: [[t, r.get(k)] for t, r in rows] for k in series_keys(last)}}

    def run(self) -> None:
        interval = 1.0 / self.hz
        while not self._stop.is_set():
            t0 = time.monotonic()
            self._tick()
            sleep = interval - (time.monotonic() - t0)
            if sleep > 0:
                self._stop.wait(sleep)

    def _tick(self) -> None:
        try:
            p = subprocess.run(self.argv(), input=SCRIPT, capture_output=True,  # noqa: S603 — fixed argv
                               text=True, timeout=8)
            if p.returncode != 0 or "@@stat" not in p.stdout:
                raise RuntimeError((p.stderr.strip().splitlines() or [f"ssh exit {p.returncode}"])[-1])
            row = self.parse(p.stdout, time.time())
        except Exception as exc:  # noqa: BLE001 — a dead node must never kill the thread
            if self.alive or self.error is None:
                self.broker.publish("events", json.dumps(
                    {"t": time.time(), "type": "node_down", "node": self.id, "error": str(exc)[:200]}))
            self.alive, self.error, self._prev = False, str(exc)[:200], None
            self._stop.wait(4.0)   # back off: an unreachable node is retried every ~5 s
            return
        self.alive, self.error = True, None
        now = row.pop("t")
        self.ring.append(row, now)
        self.broker.publish(self.topic, json.dumps({"t": now, **row}))

    def parse(self, out: str, now: float) -> dict:
        """Remote script output -> hw row (same keys as the local HwSampler)."""
        s = _sections(out)
        row: dict = {"t": now}
        cpu = [float(x) for x in (s.get("stat") or ["cpu"])[0].split()[1:9]]
        total, idle = sum(cpu), (cpu[3] + cpu[4] if len(cpu) > 4 else 0.0)
        mem = {}
        for line in s.get("mem", []):
            k, _, v = line.partition(":")
            try:
                mem[k] = float(v.split()[0]) / 2**20   # kB -> GiB
            except (IndexError, ValueError):
                pass
        if "MemTotal" in mem:
            avail = mem.get("MemAvailable", mem.get("MemFree", 0.0))
            row.update(mem_total_gb=round(mem["MemTotal"], 2), mem_avail_gb=round(avail, 2),
                       mem_used_gb=round(mem["MemTotal"] - avail, 2),
                       swap_used_gb=round(mem.get("SwapTotal", 0.0) - mem.get("SwapFree", 0.0), 2))
        load = (s.get("load") or [""])[0].split()
        if len(load) >= 2:
            row["load1"], row["load5"] = float(load[0]), float(load[1])
        rx = tx = 0.0
        for line in s.get("net", []):
            name, sep, rest = line.partition(":")
            f = rest.split()
            if sep and name.strip() != "lo" and len(f) >= 9:
                rx += float(f[0]); tx += float(f[8])
        df = (s.get("df") or [])
        if len(df) >= 2 and len(df[1].split()) >= 3:
            f = df[1].split()
            row["disk_total_gb"] = round(float(f[1]) / 2**20, 2)
            row["disk_used_gb"] = round(float(f[2]) / 2**20, 2)
        for line in s.get("temp", []):
            f = line.split()
            if len(f) == 3 and ("acpitz" in f[0] or "cpu" in f"{f[0]}_{f[1]}".lower()):
                row["cpu_temp_c"] = round(float(f[2]) / 1000.0, 1)
                break
        if self._prev:
            dt = now - self._prev[0]
            dtot, didle = total - self._prev[1], idle - self._prev[2]
            drx, dtx = rx - self._prev[3], tx - self._prev[4]
            if dtot > 0 and didle >= 0:
                row["cpu_pct"] = round(100.0 * (1 - didle / dtot), 1)
            if dt > 0 and drx >= 0 and dtx >= 0:   # counter reset = no sample, never negative
                row["net_r_mbs"] = round(drx / dt / 2**20, 2)
                row["net_w_mbs"] = round(dtx / dt / 2**20, 2)
        self._prev = (now, total, idle, rx, tx)
        gpus = _parse_nvidia("\n".join(s.get("gpu", [])))
        self.gpus = [{"index": g["index"], "name": g["name"], "vram_gb": g["mem_total_gb"]} for g in gpus]
        row.update(_gpu_row(gpus))
        return row


def start_nodes(cfg: dict, broker) -> list[NodeSampler]:
    nodes = []
    for n in cfg.get("nodes") or []:
        # a target starting with '-' would be parsed as an ssh option (-oProxyCommand=…)
        if not (isinstance(n, dict) and isinstance(n.get("ssh"), str)
                and re.fullmatch(r"[^\s-][^\s]*", n["ssh"])
                and NODE_ID_RE.fullmatch(str(n.get("id", "")))):
            print(f"[limcars-next] ignoring node entry {n!r}: needs id [A-Za-z0-9_-] and ssh")
            continue
        s = NodeSampler(n, cfg, broker)
        s.start()
        nodes.append(s)
    return nodes
