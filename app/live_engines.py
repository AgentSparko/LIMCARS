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
import ntpath
import os
import re
import signal
import subprocess
import threading
import time
import urllib.request

import psutil

from . import procs

MIN_GPU_MIB = 1024          # "over 1 GB of GPU memory" per spec
# A containerised engine's inner process belongs to root, so the dashboard cannot
# signal it; `docker stop` is the primitive that can. Grace is generous enough for
# an engine to release the GPU cleanly, the timeout is a hard ceiling on the CLI.
DOCKER_STOP_GRACE_S = 15
DOCKER_STOP_TIMEOUT_S = 90
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
_net_owner: dict[str, int] = {}   # synthetic socket token -> pid (no /proc)


def _cmdline(pid: int) -> str:
    if not procs.LINUX_PROC:
        return " ".join(procs.argv(pid)).strip()
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
    """[(port, inode)] for all LISTEN tcp + tcp6 sockets. Without /proc the
    "inode" is a synthetic "pid:port" token owned by the listening pid."""
    out = []
    if not procs.LINUX_PROC:
        _net_owner.clear()
        try:
            conns = psutil.net_connections(kind="tcp")
        except (psutil.Error, OSError):
            return out
        for c in conns:
            if c.status == psutil.CONN_LISTEN and c.laddr:
                tok = f"{c.pid or 0}:{c.laddr.port}"
                _net_owner[tok] = c.pid or 0
                out.append((c.laddr.port, tok))
        return out
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
    if not procs.LINUX_PROC:
        return {t for t, p in _net_owner.items() if p == pid}
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


def _ancestor_chain(pid: int) -> list[int]:
    """Parent pids, nearest first."""
    try:
        return [p.pid for p in psutil.Process(pid).parents()]
    except psutil.Error:
        return []


def _win_root(pid: int, is_engine, servers: set[int]) -> int:
    """Owning launcher of a worker: the topmost engine ancestor, stepping over
    unclassified helpers, but never past a process that is itself a verified
    OpenAI API server (an independent engine, not a worker)."""
    top = pid
    for a in _ancestor_chain(pid):
        if top in servers:
            break
        if is_engine(a):
            top = a
        elif a in servers:   # an unclassified API server is a boundary too
            break
    return top


def _classify(pid: int) -> str | None:
    """Engine name if this pid's argv IS an engine server, else None."""
    toks = procs.argv(pid)
    if not toks:
        return None
    base = (ntpath if procs.IS_WIN else os.path).basename(toks[0]).lower()
    if procs.IS_WIN and base.endswith(".exe"):   # llama-server.exe, vllm.exe …
        base = base[:-4]
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
        self._win_servers: set[int] = set()

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
            for pid in procs.pids():
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
                        ppid = procs.ppid(walk)
                        if ppid is None:
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
            # Windows has no process groups: a non-listening worker joins the
            # engine row it descends from; anything serving its own port stays
            # a separate card. Launcher rows sort first so they own the card.
            win_top: dict[int, int] = {}
            if not procs.HAS_PGRP:
                row_pids = {r["pid"] for r in out if r["pid"] is not None}
                orphan_ports = {r["ports"][0] for r in out if r["pid"] is None}
                self._win_servers = {r["pid"] for r in out
                                     if r["pid"] is not None and r["serving_ports"]}
                self._win_servers |= {p for t, p in _net_owner.items()
                                      if p and int(t.rsplit(":", 1)[1]) in orphan_ports}
                for p in row_pids:
                    win_top[p] = _win_root(p, row_pids.__contains__, self._win_servers)
                out.sort(key=lambda r: r["pid"] is not None and win_top[r["pid"]] != r["pid"])
            by_pgid: dict[int, dict] = {}
            merged: list[dict] = []
            for r in out:
                if r["pid"] is None:
                    merged.append(r)
                    continue
                if not procs.HAS_PGRP:
                    pg = win_top[r["pid"]]
                else:
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

    def _stop_container(self, pid: int) -> dict | None:
        """Stop a containerised engine whose process we are not allowed to signal.

        `docker run` without a user namespace puts root's process behind the pid we
        scan, so killpg raises PermissionError for a dashboard running as a normal
        user — even one whose operator is in the `docker` group, which is the case
        that matters. Docker has the right primitive and `docker stop` lets the
        engine let go of the GPU instead of being SIGKILLed mid-request.

        The container identity comes ONLY from /proc/<pid>/cgroup for the pid the
        operator clicked; the request carries a pid and nothing else, so this stays
        an engine-stop button and not a "stop any container" endpoint. Returns None
        when the pid is not a container or docker cannot be reached, so the caller
        still gets the honest 'stop it where it was started' answer.
        """
        try:
            from . import docker_recipe
            cid = docker_recipe.container_id(pid)
        except Exception:  # noqa: BLE001 — no docker, no perms, weird cgroup: old answer
            return None
        if not cid:
            return None
        name = ""
        try:                      # cosmetic label only; failure must not block the stop
            name = subprocess.run(["docker", "inspect", "-f", "{{.Name}}", cid],
                                  capture_output=True, text=True,
                                  timeout=DOCKER_STOP_TIMEOUT_S).stdout.strip().lstrip("/")
        except Exception:  # noqa: BLE001
            pass
        try:
            r = subprocess.run(["docker", "stop", "--time", str(DOCKER_STOP_GRACE_S), cid],
                               capture_output=True, text=True, timeout=DOCKER_STOP_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            return None          # docker missing / not permitted for this service user
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()[:200]
            return {"ok": False, "error": f"docker stop failed: {detail or 'unknown error'}"}
        self._last_by_pid.pop(pid, None)
        self.scan(force=True)
        return {"ok": True, "killed_pid": pid, "stopped_container": name or cid[:12],
                "via": "docker stop"}

    def stop(self, pid) -> dict:
        """SIGTERM→KILL an engine pid with guardrails (port rows refuse)."""
        if pid is None:
            return {"ok": False,
                    "error": "engine runs outside this dashboard's reach (root/container) — stop it there"}
        pid = int(pid)
        if pid == os.getpid() or pid < (8 if procs.IS_WIN else 512):
            return {"ok": False, "error": "refused: system/dashboard pid"}
        pgid = None
        if procs.HAS_PGRP:
            try:
                pgid = os.getpgid(pid)
                if pgid == os.getpgid(0):
                    return {"ok": False, "error": "refused: dashboard's own process group"}
            except ProcessLookupError:
                return {"ok": False, "error": "no such process"}
        else:
            if not procs.alive(pid):
                return {"ok": False, "error": "no such process"}
            if pid in procs.ancestors(os.getpid()):
                return {"ok": False, "error": "refused: dashboard's own process tree"}
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
        if pgid is None:
            # a worker: stop the card it belongs to *right now* — a fresh scan
            # re-derives engine rows and API-server boundaries, never one of ours
            mine = procs.ancestors(os.getpid())
            card = next((r for r in self.scan(force=True)
                         if r["pid"] == pid or pid in (r.get("pids_extra") or ())), None)
            if card and card["pid"] != pid and card["pid"] not in mine:
                with self._lock:
                    holders = _gpu_holders()
                root = card["pid"]
                if bool(_classify(root)) or holders.get(root, 0) >= MIN_GPU_MIB:
                    pid = root
            try:
                procs.kill_tree(pid)
            except PermissionError:
                stopped = self._stop_container(pid)
                if stopped is not None:
                    return stopped
                return {"ok": False,
                        "error": "process belongs to another user/admin — stop it where it was started"}
            self._last_by_pid.pop(pid, None)
            self.scan(force=True)
            return {"ok": True, "killed_pid": pid}
        try:
            os.killpg(pgid, signal.SIGTERM)
        except PermissionError:
            # a container the CLI never supervised: ask docker, which can
            stopped = self._stop_container(pid)
            if stopped is not None:
                return stopped
            return {"ok": False,
                    "error": "process is root-owned — stop it where it was started (systemd?)"}
        except ProcessLookupError:
            return {"ok": False, "error": "process vanished"}
        deadline = time.time() + 10
        while time.time() < deadline and procs.alive(pid):
            time.sleep(0.25)
        if procs.alive(pid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.5)
        self._last_by_pid.pop(pid, None)
        self.scan(force=True)
        return {"ok": True, "killed_pid": pid}
