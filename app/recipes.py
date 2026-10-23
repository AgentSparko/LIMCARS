"""Recipe server — user-saved engine launch commands with Start/Stop.

Recipes live in data/recipes.json. Each recipe = name (emoji in the name is
encouraged as a visual anchor), a shell command (the full launch recipe,
e.g. `vllm serve ... --port 8000`), and the pid of the last start.

Security posture: commands are defined BY THE LOCAL USER through an
authenticated dashboard on their own box — same trust level as the shell.
The old LIMCARS recipe launcher worked the same way. Endpoints sit behind
the existing /api/ auth gate; nothing here runs anything that isn't stored
in recipes.json by the user.

Processes are started detached in their OWN session (start_new_session),
stdout/stderr appended to data/recipe-logs/<id>.log, and stopped by
killpg SIGTERM → grace → SIGKILL so launch scripts with children die fully.
Running state is re-derived from /proc, so a dashboard restart does not
lose track of started recipes.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid


class Recipes:
    MAX_CMD = 8000
    MAX_NAME = 80

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "recipes.json")
        self.log_dir = os.path.join(data_dir, "recipe-logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self._lock = threading.Lock()
        self.items: list[dict] = self._load()

    # ---------------- persistence ----------------

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _find(self, rid: str) -> dict | None:
        return next((r for r in self.items if r["id"] == rid), None)

    # ---------------- status ----------------

    @staticmethod
    def _alive(pid) -> bool:
        return bool(pid) and os.path.isdir(f"/proc/{int(pid)}")

    def list(self) -> list[dict]:
        out = []
        for r in self.items:
            out.append({
                "id": r["id"], "name": r["name"], "cmd": r["cmd"],
                "pid": r.get("pid"), "started_at": r.get("started_at"),
                "created_at": r.get("created_at"),
                "running": self._alive(r.get("pid")),
                "log": os.path.join(self.log_dir, f"{r['id']}.log"),
            })
        return out

    # ---------------- CRUD ----------------

    def add(self, name, cmd) -> dict:
        name = (name or "").strip()[: self.MAX_NAME]
        cmd = (cmd or "").strip()
        if not name or not cmd:
            return {"ok": False, "error": "name and cmd are both required"}
        if len(cmd) > self.MAX_CMD:
            return {"ok": False, "error": f"cmd too long (max {self.MAX_CMD})"}
        with self._lock:
            if any(r["name"].lower() == name.lower() for r in self.items):
                return {"ok": False, "error": f"a recipe named '{name}' already exists"}
            r = {"id": uuid.uuid4().hex[:8], "name": name, "cmd": cmd,
                 "pid": None, "started_at": None, "created_at": time.time()}
            self.items.append(r)
            self._save()
            return {"ok": True, "recipe": r}

    def update(self, rid: str, name=None, cmd=None) -> dict:
        with self._lock:
            r = self._find(rid)
            if not r:
                return {"ok": False, "error": "no such recipe"}
            if cmd is not None and cmd.strip() != r["cmd"] and self._alive(r.get("pid")):
                return {"ok": False, "error": "stop the recipe before editing its command"}
            if name is not None:
                name = name.strip()[: self.MAX_NAME]
                if not name:
                    return {"ok": False, "error": "name cannot be empty"}
                if any(x["name"].lower() == name.lower() and x["id"] != rid
                       for x in self.items):
                    return {"ok": False, "error": f"a recipe named '{name}' already exists"}
                r["name"] = name
            if cmd is not None:
                cmd = cmd.strip()
                if not cmd:
                    return {"ok": False, "error": "cmd cannot be empty"}
                if len(cmd) > self.MAX_CMD:
                    return {"ok": False, "error": f"cmd too long (max {self.MAX_CMD})"}
                r["cmd"] = cmd
            self._save()
            return {"ok": True, "recipe": r}

    def delete(self, rid: str) -> dict:
        with self._lock:
            r = self._find(rid)
            if not r:
                return {"ok": False, "error": "no such recipe"}
            if self._alive(r.get("pid")):
                return {"ok": False, "error": "running — stop it first"}
            self.items = [x for x in self.items if x["id"] != rid]
            self._save()
            try:
                os.remove(os.path.join(self.log_dir, f"{rid}.log"))
            except OSError:
                pass
            return {"ok": True}

    # ---------------- start / stop ----------------

    def start(self, rid: str) -> dict:
        with self._lock:
            r = self._find(rid)
            if not r:
                return {"ok": False, "error": "no such recipe"}
            if self._alive(r.get("pid")):
                return {"ok": False, "error": f"already running (pid {r['pid']})"}
            log_path = os.path.join(self.log_dir, f"{r['id']}.log")
            try:
                log = open(log_path, "ab")  # noqa: SIM115 — stays open for the proc
                log.write(f"\n===== start {time.strftime('%F %T')} :: {r['cmd']}\n".encode())
                log.flush()
                # shell=True is deliberate: recipes ARE user-authored shell lines
                proc = subprocess.Popen(  # noqa: S602
                    r["cmd"], shell=True,  # noqa: S602
                    stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                    start_new_session=True, cwd=os.path.expanduser("~"))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)[:300]}
            r["pid"] = proc.pid
            r["started_at"] = time.time()
            self._save()
            return {"ok": True, "pid": proc.pid, "log": log_path}

    def stop(self, rid: str) -> dict:
        with self._lock:
            r = self._find(rid)
            if not r:
                return {"ok": False, "error": "no such recipe"}
            pid = int(r.get("pid") or 0)
            if not self._alive(pid):
                r["pid"] = None
                self._save()
                return {"ok": True, "note": "was not running"}
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                deadline = time.time() + 10
                while time.time() < deadline and self._alive(pid):
                    time.sleep(0.25)
                if self._alive(pid):
                    os.killpg(pgid, signal.SIGKILL)
                    time.sleep(0.5)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                return {"ok": False, "error": f"killpg: {exc}"}
            r["pid"] = None
            r["last_stop_at"] = time.time()
            self._save()
            return {"ok": True}
