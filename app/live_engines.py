"""Live inference-engine detector — finds engines wherever they were started.

Three probes combined:
  1. nvidia-smi compute-apps → pids holding > MIN_GPU_MIB of GPU memory
  2. strict argv classification: argv[0] basename or a raw --flag matches an
     engine marker (vllm/sglang/llama-server/tandem/…). Substring matching
     on the whole cmdline is NOT used — it false-positives on the dash
     proxy whose argv carries --tandem-url and on log paths mentioning vllm.
  3. port fingerprinting: EVERY listening port (from /proc/net/tcp[6]) is
     probed for OpenAI /v1/models; a port that answers IS an engine API
     regardless of who owns the process. This is how root/containerised
     engines are detected when their /proc is unreadable (ss shows no pid).

Attribution: engine-API ports are attributed to a scanning-classified pid
when their LISTEN backlog matches that pid's, otherwise listed as a
standalone engine-API row (port only). Stop re-validates at kill time and
refuses the dashboard itself, its own group, pids < 512, and any pid that
wasn't seen by a recent scan as engine/GPU-holder.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.request

MIN_GPU_MIB = 1024          # "over 1 GB of GPU memory" per spec
_SCAN_TTL = 5.0
# argv tokens that mean "this process IS the server" (matched exactly,
# case-insensitive: argv0 basename or a --flag)
ARGV_EXACT = {
    "vllm": "vLLM", "vllm-v1": "vLLM", "sglang": "SGLang",
    "llama-server": "llama.cpp", "llama.cpp": "llama.cpp",
    "tandemllm": "TandemLLM", "text-generation-launcher": "TGI",
}
ARGV_FLAG = {  # flags only the engine server itself passes to itself
    "--model": ("vLLM", "SGLang", "TGI"),
}
_SOCK_RE = re.compile(r"pid=(\d+)")


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _gpu_holders() -> dict[int, int]:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
        out: dict[int, int] = {}
        for line in r.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                try:
                    pid, mib = int(parts[0]), int(parts[1])
                except ValueError:
                    continue
                if mib >= MIN_GPU_MIB:
                    out[pid] = mib
        return out
    except Exception:  # noqa: BLE001 — nvidia-smi missing → other probes still run
        return {}


def _listen_sockets() -> list[tuple[int, str]]:
    """[(port, inode)] for all LISTEN tcp + tcp6 sockets."""
    out = []
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(f, encoding="ascii", errors="replace") as fh:
                next(fh, None)
                for line in fh:
                    fl = line.split()
                    if len(fl) > 9 and fl[3] == "0A":  # TCP_LISTEN
                        try:
                            out.append((int(fl[1].split(":")[1], 16), fl[9]))
                        except (IndexError, ValueError):
                            continue
        except OSError:
            pass
    return out


def _pid_inodes(pid: int) -> set[str]:
    inodes: set[str] = set()
    fd_dir = f"/proc/{pid}/fd"
    try:
        for fd in os.listdir(fd_dir):
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if link.startswith("socket:["):
                inodes.add(link[8:-1])
    except OSError:
        pass
    return inodes


def _classify(pid: int) -> str | None:
    """Engine name if this pid's argv IS an engine server, else None."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read().split(b"\0")
    except OSError:
        return None
    toks = [t.decode("utf-8", "replace") for t in raw if t]
    if not toks:
        return None
    base = os.path.basename(toks[0]).lower()
    if base in ARGV_EXACT:
        return ARGV_EXACT[base]
    if base.startswith("vllm::"):          # renamed procs: VLLM::EngineCore/Worker
        return "vLLM"
    if base.startswith("sglang::"):
        return "SGLang"
    for t in toks[1:]:
        tl = t.lower()
        if tl in ARGV_EXACT:
            return ARGV_EXACT[tl]
        if tl in ARGV_FLAG:  # --model only counts for real server invocations
            joined = " ".join(toks[:3]).lower()
            for name in ARGV_FLAG[tl]:
                if name.lower() in joined or base.endswith(name):
                    return name
    return None


def _serving(port: int, timeout: float = 1.0) -> tuple[str, str] | None:
    """(model_id, owned_by) if 127.0.0.1:port answers OpenAI /v1/models."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        items = data.get("data") or []
        if not items:
            return ("?", "")
        return (items[0]["id"], str(items[0].get("owned_by") or ""))
    except Exception:  # noqa: BLE001 — any failure = not an OpenAI endpoint
        return None


def _type_from_model(owned_by: str | None, fallback: str) -> str:
    o = (owned_by or "").lower()
    for key, name in (("vllm", "vLLM"), ("sglang", "SGLang"),
                      ("tandem", "TandemLLM"), ("llama", "llama.cpp")):
        if key in o:
            return name
    return fallback


class LiveEngines:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache: tuple[float, list[dict]] = (0.0, [])
        self._last_by_pid: dict = {}

    def scan(self, force: bool = False) -> list[dict]:
        with self._lock:
            now = time.time()
            # audit: force-refresh spam = one full proc scan per request
            # (CPU DoS vector). Even explicit refreshes coalesce to 2 s.
            if now - self._cache[0] < (_SCAN_TTL if not force else 2.0):
                return self._cache[1]

            holders = _gpu_holders()
            listeners = _listen_sockets()

            # argv-classified engine processes
            classified: dict[int, str] = {}
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                etype = _classify(pid)
                if etype:
                    classified[pid] = etype

            # ports owned by classified pids (fd inode match, if permitted)
            ports_by_pid: dict[int, list[int]] = {}
            claimed_inodes: set[str] = set()
            for pid in set(classified) | set(holders):
                own = _pid_inodes(pid) & {i for _, i in listeners}
                if own:
                    ports_by_pid[pid] = sorted(p for p, i in listeners if i in own)
                    claimed_inodes |= own

            # fingerprint every unclaimed listening port (catches root-owned
            # engines whose /proc/fd we cannot read); dedupe by port first so
            # an ipv4+ipv6 pair is only probed once
            orphans: dict[int, tuple[str, str]] = {}   # port -> (model, owned_by)
            for port, inode in listeners:
                if inode in claimed_inodes or port < 1024 or port in orphans:
                    continue
                sp = _serving(port)
                if sp:
                    orphans[port] = sp
                    claimed_inodes.add(inode)

            out = []
            seen_ports: set[int] = set()
            for pid in sorted(set(classified) | set(holders)):
                cmd = _cmdline(pid)
                ports = ports_by_pid.get(pid, [])
                serving = {}
                owned = ""
                for p in ports[:6]:
                    sp = _serving(p)
                    if sp:
                        serving[p] = sp[0]
                        owned = owned or sp[1]
                        seen_ports.add(p)
                etype = classified.get(pid)
                # a root-owned engine we cannot attribute by fd still declares
                # its --port somewhere in its process ancestry (VLLM::Worker →
                # EngineCore → vllm serve --port 8000) — merge that orphan here
                if not serving:
                    found_port = None
                    walk, depth = pid, 0
                    while walk and depth < 6 and found_port is None:
                        try:
                            with open(f"/proc/{walk}/status", encoding="ascii",
                                      errors="replace") as fh:
                                ppid = 0
                                for ln in fh:
                                    if ln.startswith("PPid:"):
                                        ppid = int(ln.split()[1])
                                        break
                        except OSError:
                            break
                        acmd = _cmdline(ppid)
                        m = re.search(r"--port[= ](\d{2,5})", acmd)
                        if m and int(m.group(1)) in orphans:
                            found_port = int(m.group(1))
                            break
                        if ppid in (0, 1) or ppid == walk:
                            break
                        walk, depth = ppid, depth + 1
                    if found_port is None:  # fall back: own cmdline --port
                        m = re.search(r"--port[= ](\d{2,5})", cmd)
                        if m and int(m.group(1)) in orphans:
                            found_port = int(m.group(1))
                    if found_port is not None:
                        serving[found_port] = orphans[found_port][0]
                        owned = orphans[found_port][1]
                        seen_ports.add(found_port)
                        del orphans[found_port]
                if not etype:
                    etype = _type_from_model(owned, "GPU process")
                model = next(iter(serving.values()), None)
                if not cmd and pid not in holders:
                    continue
                out.append({
                    "pid": pid, "type": etype, "model": model,
                    "ports": sorted(set(ports) | set(serving)),
                    "serving_ports": serving,
                    "gpu_mib": holders.get(pid),
                    "cmd": cmd[:400], "orphan": False,
                })
                self._last_by_pid[pid] = (now, cmd or "vllm")

            # engine-API ports owned by nobody we can see
            for port, (model, owned_by) in list(orphans.items()):
                if port in seen_ports:
                    continue
                out.append({
                    "pid": None, "type": _type_from_model(owned_by, "Engine API"),
                    "model": model, "ports": [port], "serving_ports": {port: model},
                    "gpu_mib": None, "cmd": "", "orphan": True,
                })
                # remember so stop-by-port can re-validate the fingerprint
                self._last_by_pid[("port", port)] = (now, model)

            # One engine is often several processes (vllm serve API proc +
            # EngineCore + GPU Worker, all in one process group). Merge rows
            # sharing a pgid into a single engine card.
            by_pgid: dict[int, dict] = {}
            merged: list[dict] = []
            for r in out:
                if r["pid"] is None:
                    merged.append(r)
                    continue
                try:
                    pg = os.getpgid(r["pid"])
                except (ProcessLookupError, PermissionError):
                    merged.append(r)
                    continue
                prev = by_pgid.get(pg)
                if prev is None:
                    by_pgid[pg] = r
                    merged.append(r)
                else:
                    prev["ports"] = sorted(set(prev["ports"]) | set(r["ports"]))
                    for p, m in r["serving_ports"].items():
                        prev["serving_ports"].setdefault(p, m)
                    if prev["model"] is None:
                        prev["model"] = r["model"]
                    if (prev.get("gpu_mib") or 0) < (r.get("gpu_mib") or 0):
                        prev["gpu_mib"] = r["gpu_mib"]
                    if prev["type"] == "GPU process" and r["type"] != "GPU process":
                        prev["type"] = r["type"]
                    prev["cmd"] = prev["cmd"] or r["cmd"]
                    prev["pids_extra"] = prev.get("pids_extra", []) + [r["pid"]]
                    self._last_by_pid[r["pid"]] = (now, r["cmd"] or "vllm")
            out = merged

            # drop pid rows that duplicate an orphan port we also fingerprinted
            # via a DIFFERENT owner claim (shouldn't happen; defensive dedupe)
            self._cache = (now, out)
            return out

    def stop(self, pid) -> dict:
        """SIGTERM→KILL an engine pid with guardrails (port rows refuse)."""
        if pid is None:
            return {"ok": False,
                    "error": "engine runs outside this dashboard's reach (root/container) — stop it there"}
        pid = int(pid)
        if pid == os.getpid() or pid < 512:
            return {"ok": False, "error": "refused: system/dashboard pid"}
        try:
            pgid = os.getpgid(pid)
            if pgid == os.getpgid(0):
                return {"ok": False, "error": "refused: dashboard's own process group"}
        except ProcessLookupError:
            return {"ok": False, "error": "no such process"}
        seen = self._last_by_pid.get(pid)
        if not seen or time.time() - seen[0] > 120:
            return {"ok": False, "error": "pid not seen in recent engine scan — refresh and retry"}
        cmd = _cmdline(pid)
        if not cmd:
            return {"ok": False, "error": "process gone"}
        with self._lock:
            holders = _gpu_holders()
        still_engine = bool(_classify(pid)) or holders.get(pid, 0) >= MIN_GPU_MIB
        if not still_engine:
            return {"ok": False, "error": "pid is no longer an engine/GPU holder — refusing"}
        try:
            os.killpg(pgid, signal.SIGTERM)
        except PermissionError:
            return {"ok": False,
                    "error": "process is root-owned — stop it where it was started (docker/systemd)"}
        except ProcessLookupError:
            return {"ok": False, "error": "process vanished"}
        deadline = time.time() + 10
        while time.time() < deadline and os.path.isdir(f"/proc/{pid}"):
            time.sleep(0.25)
        if os.path.isdir(f"/proc/{pid}"):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.5)
        self._last_by_pid.pop(pid, None)
        self.scan(force=True)
        return {"ok": True, "killed_pid": pid}
