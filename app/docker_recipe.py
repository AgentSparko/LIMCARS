"""Reconstruct the FULL launchable `docker run` for the container owning a pid.

The dashboard's "launch recipe" box used to show the raw /proc cmdline of the
serving process INSIDE the container (`vllm serve ...`). Saving that as a
recipe produced something that cannot start the model: the docker wrapper
(image, -e, -v, --gpus, ulimits, cgroup pins, restart policy) was invisible.

This module asks docker for the truth — `docker inspect` on the container
that owns the pid — and re-emits an equivalent `docker run`. Rules:

* everything comes from docker inspect; nothing is guessed,
* env vars identical to the image's own ENV are dropped (docker re-applies
  them anyway) so the command stays compact and still reproduces exactly,
* docker defaults are omitted (e.g. default shm size),
* returns None when the pid is not a docker container (native engines) —
  the caller keeps the plain process cmdline.

The result is a foreground `docker run` (no -d): the recipe manager stops
things by killpg, which only reaches a container the CLI still supervises.
"""
from __future__ import annotations

import json
import shlex
import subprocess

_HEX = set("0123456789abcdef")


def container_id(pid: int) -> str | None:
    """64-hex container id owning pid, from /proc/<pid>/cgroup (v1 + v2)."""
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as f:
            for line in f:
                path = line.split(":", 2)[-1].strip()
                for tok in reversed(path.split("/")):
                    cid = None
                    if tok.startswith("docker-") and tok.endswith(".scope"):
                        cid = tok[len("docker-"):-len(".scope")]
                    elif len(tok) == 64:
                        cid = tok
                    if cid and len(cid) == 64 and set(cid) <= _HEX:
                        return cid
    except OSError:
        return None
    return None


def _docker_json(*args: str):
    try:
        r = subprocess.run(["docker", *args], capture_output=True,
                           text=True, timeout=10)
        if r.returncode:
            return None
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001 — absence of docker just means "no recipe"
        return None


def build_for_pid(pid: int) -> str | None:
    """Complete `docker run ...` reproducing the container owning pid."""
    cid = container_id(pid)
    if not cid:
        return None
    return build_from_inspect(_docker_json("inspect", cid),
                              image_lookup=lambda ref: _docker_json(
                                  "inspect", "--type", "image", ref))


def build_from_inspect(j, image_lookup=None) -> str | None:
    """Pure part: take `docker inspect <cid>` JSON -> docker run string.

    image_lookup(ref) -> `docker inspect --type image <ref>` JSON or None
    (injectable for tests)."""
    if not isinstance(j, list) or not j:
        return None
    c = j[0]
    cfg = c.get("Config") or {}
    hc = c.get("HostConfig") or {}
    image = cfg.get("Image") or c.get("Image") or ""
    if not image:
        return None
    image_env: set[str] = set()
    image_entry = None
    if image_lookup:
        im = image_lookup(image)
        if isinstance(im, list) and im:
            ic = (im[0].get("Config") or {})
            image_env = set(ic.get("Env") or [])
            image_entry = ic.get("Entrypoint")

    q = shlex.quote
    parts = ["docker", "run"]  # foreground: dashboard Stop (killpg) must reach it
    name = (c.get("Name") or "").lstrip("/")
    if name:
        parts += ["--name", q(name)]
    rp = hc.get("RestartPolicy") or {}
    if rp.get("Name") and rp["Name"] != "no":
        v = rp["Name"]
        if rp["Name"] == "on-failure" and rp.get("MaximumRetryCount"):
            v += f":{rp['MaximumRetryCount']}"
        parts += ["--restart", q(v)]
    if hc.get("IpcMode") and hc["IpcMode"] not in ("private", "default", ""):
        parts += ["--ipc", q(hc["IpcMode"])]
    if hc.get("NetworkMode"):
        parts += ["--network", q(hc["NetworkMode"])]
    if hc.get("PidMode"):
        parts += ["--pid", q(hc["PidMode"])]
    if hc.get("Privileged"):
        parts.append("--privileged")
    if hc.get("ReadonlyRootfs"):
        parts.append("--read-only")
    if hc.get("Init"):
        parts.append("--init")
    for s in hc.get("SecurityOpt") or []:
        parts += ["--security-opt", q(s)]
    for cap in hc.get("CapAdd") or []:
        parts += ["--cap-add", q(cap)]
    for cap in hc.get("CapDrop") or []:
        parts += ["--cap-drop", q(cap)]
    for d in hc.get("Devices") or []:
        parts += ["--device", q(f"{d.get('PathOnHost')}:{d.get('PathInContainer')}"
                                f":{d.get('CgroupPermissions', 'rwm')}")]
    for dr in hc.get("DeviceRequests") or []:
        caps = {c for grp in (dr.get("Capabilities") or []) for c in grp}
        if "gpu" in caps or dr.get("Count") == -1:
            if dr.get("Count") == -1:
                parts += ["--gpus", "all"]
            elif dr.get("DeviceIDs"):
                parts += ["--gpus", q(",".join(dr["DeviceIDs"]))]
            elif dr.get("Count"):
                parts += ["--gpus", str(dr["Count"])]
    for u in hc.get("Ulimits") or []:
        parts += ["--ulimit", q(f"{u.get('Name')}={u.get('Soft')}:{u.get('Hard')}")]
    if hc.get("Memory"):
        parts += ["--memory", str(hc["Memory"])]
    if hc.get("MemorySwap") and hc.get("MemorySwap") != -1:
        parts += ["--memory-swap", str(hc["MemorySwap"])]
    if hc.get("MemoryReservation"):
        parts += ["--memory-reservation", str(hc["MemoryReservation"])]
    if hc.get("ShmSize") and hc["ShmSize"] != 67108864:
        parts += ["--shm-size", str(hc["ShmSize"])]
    if hc.get("CpusetCpus"):
        parts += ["--cpuset-cpus", q(hc["CpusetCpus"])]
    if hc.get("CpusetMems"):
        parts += ["--cpuset-mems", q(hc["CpusetMems"])]
    if hc.get("CpuQuota"):
        parts += ["--cpu-quota", str(hc["CpuQuota"])]
    if hc.get("CpuPeriod"):
        parts += ["--cpu-period", str(hc["CpuPeriod"])]
    if hc.get("CpuShares") and hc["CpuShares"] != 1024:
        parts += ["--cpu-shares", str(hc["CpuShares"])]
    for b in hc.get("Binds") or cfg.get("Binds") or []:
        parts += ["-v", q(b)]
    if hc.get("NetworkMode") != "host":
        for port, binds in (hc.get("PortBindings") or {}).items():
            for b in binds or []:
                hp, hip = b.get("HostPort"), b.get("HostIp")
                parts += ["-p", q(f"{hip}:{hp}:{port}" if hip else f"{hp}:{port}")]
    if hc.get("PublishAllPorts"):
        parts.append("-P")
    if hc.get("OomScoreAdj"):
        parts += ["--oom-score-adj", str(hc["OomScoreAdj"])]
    if hc.get("OOMKillDisable"):
        parts.append("--oom-kill-disable")
    if cfg.get("User"):
        parts += ["-u", q(cfg["User"])]
    if cfg.get("WorkingDir"):
        parts += ["-w", q(cfg["WorkingDir"])]
    if cfg.get("Tty") and cfg.get("OpenStdin"):
        parts.append("-it")
    lc = hc.get("LogConfig") or {}
    if lc.get("Type") and lc["Type"] != "json-file":
        parts += ["--log-driver", q(lc["Type"])]
    for k, v in (lc.get("Config") or {}).items():
        parts += ["--log-opt", q(f"{k}={v}")]
    for e in cfg.get("Env") or []:          # drop vars the image already sets
        if e not in image_env:
            parts += ["-e", q(e)]
    entry, cmd = cfg.get("Entrypoint"), cfg.get("Cmd")
    if entry is not None and list(entry) != list(image_entry or []):
        parts += ["--entrypoint", q(entry[0])]
        cmd = list(entry[1:]) + list(cmd or [])
    parts.append(q(image))
    parts += [q(x) for x in (cmd or [])]
    return " ".join(parts)
