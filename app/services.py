"""Whitelisted service start/stop for the admin card on the hub page.

Actions come ONLY from config `services` entries (fixed argv strings);
arbitrary commands can never reach here. systemd-run isn't used; entries
may be `systemctl ...`, `docker ...` — whatever the config says.
"""
from __future__ import annotations

import shlex
import subprocess
import threading
import time


class Services:
    def __init__(self, cfg: dict):
        self.entries = {s["id"]: s for s in cfg.get("services", [])}
        self._lock = threading.Lock()
        self._busy: dict[str, str] = {}

    def status_all(self) -> list[dict]:
        out = []
        for sid, s in self.entries.items():
            state, detail = self._probe(s)
            out.append({"id": sid, "label": s.get("label", sid),
                        "state": state, "detail": detail,
                        "busy": self._busy.get(sid)})
        return out

    def _probe(self, s: dict) -> tuple[str, str]:
        cmd = s.get("status")
        if not cmd:
            return "unknown", ""
        try:
            r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=5)
            detail = (r.stdout or r.stderr).strip().splitlines()
            return ("running" if r.returncode == 0 and detail and detail[0] not in ("", "0")
                    else "stopped"), (detail[0] if detail else "")
        except Exception as exc:  # noqa: BLE001
            return "unknown", str(exc)[:120]

    def action(self, sid: str, act: str) -> dict:
        s = self.entries.get(sid)
        if not s:
            return {"ok": False, "error": f"unknown service {sid}"}
        cmd = s.get(act)
        if not cmd:
            return {"ok": False, "error": f"service {sid} has no {act} action"}
        with self._lock:
            if sid in self._busy:
                return {"ok": False, "error": f"{sid} busy ({self._busy[sid]})"}
            self._busy[sid] = act
        try:
            r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=120)  # noqa: S603
            return {"ok": r.returncode == 0, "exit": r.returncode,
                    "stdout": r.stdout[-2000:], "stderr": r.stderr[-2000:],
                    "at": time.time()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:300]}
        finally:
            with self._lock:
                self._busy.pop(sid, None)
