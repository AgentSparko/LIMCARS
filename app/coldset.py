"""SSD cold-expert set + memory reserve view (/api/coldset).

Reads what a serving recipe leaves on disk, no engine API needed:
- <route_heat_dir>/*.status.json: written by the engine each time it saves its permanent
  per-expert route record (routes, boots, record size, cold-route share of recent steps)
- <route_heat_dir>/auto_cold-last.json: the last boot-time cold-set decision
- the reserve cgroup (systemd user slice that caps non-engine tenants such as Hermes):
  memory now / peak / max. The cgroup's own peak resets when the slice is recreated, so the
  highest peak seen is kept in <data>/reserve_peak.json.
Everything is optional; a box without them gets {"available": false}.
"""
from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path


def _heat_dir(cfg: dict) -> Path:
    v = os.environ.get("LIMCARS_ROUTE_HEAT_DIR") or cfg.get("route_heat_dir")
    return Path(v).expanduser() if v else Path.home() / ".cache/dsv41-ds41f/route-heat"


def _reserve_cg(cfg: dict) -> Path:
    v = os.environ.get("LIMCARS_RESERVE_CGROUP") or cfg.get("reserve_cgroup")
    if v:
        return Path(v)
    uid = os.getuid()
    return Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/reserve.slice")


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_int(p: Path) -> int | None:
    try:
        s = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return None if s == "max" else int(s)


def reserve(cfg: dict, data_dir: Path, now: float | None = None) -> dict | None:
    cg = _reserve_cg(cfg)
    cur = _read_int(cg / "memory.current")
    if cur is None:
        return None
    now = time.time() if now is None else now
    peak = _read_int(cg / "memory.peak")
    keep = data_dir / "reserve_peak.json"
    rec = _read_json(keep) or {}
    best = max(x for x in (rec.get("peak_bytes") or 0, peak or 0, cur))
    if best > (rec.get("peak_bytes") or 0):
        rec = {"peak_bytes": best, "peak_time": now, "since": rec.get("since", now)}
        try:
            tmp = keep.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec), encoding="utf-8")
            os.replace(tmp, keep)
        except OSError:
            pass
    tenants = sorted(p.name for p in cg.iterdir() if p.is_dir()) if cg.is_dir() else []
    return {"cgroup": cg.name, "current_bytes": cur, "peak_bytes": peak,
            "max_bytes": _read_int(cg / "memory.max"), "high_bytes": _read_int(cg / "memory.high"),
            "peak_ever_bytes": rec.get("peak_bytes"), "peak_ever_time": rec.get("peak_time"),
            "watched_since": rec.get("since"), "tenants": tenants}


def report(cfg: dict, data_dir: Path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    d = _heat_dir(cfg)
    records = []
    for f in sorted(glob.glob(str(d / "*.status.json"))):
        s = _read_json(Path(f))
        if not s:
            continue
        s["name"] = Path(f).name[: -len(".status.json")]
        s["age_s"] = round(now - s.get("time", now))
        records.append(s)
    last = _read_json(d / "auto_cold-last.json")
    if last:
        last["age_s"] = round(now - last.get("time", now))
    res = reserve(cfg, data_dir, now)
    return {"available": bool(records or last or res), "dir": str(d),
            "records": records, "auto_cold": last, "reserve": res}
