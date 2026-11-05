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

Start also clears the two ways a launch used to die silently: a stopped
container still holding the `--name` of the recipe's `docker run` (docker
refuses the name; Stop leaves such a container behind every time) is removed
first, a command that exits within START_GRACE_S is reported as a failed
start with the tail of its log instead of a silent "started", and a recipe
that ends later without Stop shows how it ended (exit code, log tail).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid

from . import procs

_SHELL_OPS = {";", "&&", "||", "|", "&", "(", ")"}
_LIVE_STATES = {"running", "restarting", "paused"}


def docker_run_names(cmd: str) -> list[tuple[list[str], str]]:
    """(docker argv prefix, container name) for each `docker run --name X` in a shell line.

    The prefix keeps a leading `sudo [-n]` so the cleanup runs docker the same way."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return []
    out, seg = [], []
    for t in toks + [";"]:
        if t not in _SHELL_OPS:
            seg.append(t)
            continue
        for i in range(len(seg) - 1):
            if os.path.basename(seg[i]) == "docker" and seg[i + 1] == "run":
                prefix = ["sudo", "-n", "docker"] if "sudo" in seg[:i] else ["docker"]
                rest = seg[i + 2:]
                for j, a in enumerate(rest):
                    if a == "--name" and j + 1 < len(rest):
                        out.append((prefix, rest[j + 1]))
                        break
                    if a.startswith("--name="):
                        out.append((prefix, a.split("=", 1)[1]))
                        break
                break
        seg = []
    return out


def _docker(argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _log_tail(path: str, start: int, lines: int = 6) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(start)
            text = f.read()[-8192:].decode("utf-8", "replace")
    except OSError:
        return ""
    keep = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = [ln for ln in keep if not re.match(r"Run '.*--help' for more information", ln)]
    return " | ".join(keep[-lines:])[-600:]


def _dur(s: float) -> str:
    s = int(max(0, s))
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s" if s < 3600 else f"{s // 3600}h {s % 3600 // 60:02d}m"


class Recipes:
    MAX_CMD = 8000
    MAX_NAME = 80
    START_GRACE_S = 2.0

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "recipes.json")
        self.log_dir = os.path.join(data_dir, "recipe-logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self._lock = threading.Lock()
        self.items: list[dict] = self._load()
        self._procs: dict[str, subprocess.Popen] = {}   # started by this dashboard run

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
        return procs.alive(pid)

    def _reap(self, r: dict) -> None:
        """A recipe whose process is gone without Stop: record how it ended (lock held)."""
        if not r.get("pid") or self._alive(r["pid"]):
            return
        proc = self._procs.pop(r["id"], None)
        rc = proc.poll() if proc else None   # unknown when started before a dashboard restart
        log_path = os.path.join(self.log_dir, f"{r['id']}.log")
        started = r.get("started_at") or time.time()
        try:                                  # last log write ~ when it ended; noticed later
            ended = min(time.time(), max(os.path.getmtime(log_path), started))
        except OSError:
            ended = time.time()
        took = _dur(ended - started)
        tail = _log_tail(log_path, int(r.get("log_mark") or 0))
        if rc == 0:
            r["last_note"] = f"finished on its own after {took} (exit 0)"
        else:
            how = ("" if rc is None else f" killed by signal {-rc}" if rc < 0
                   else f" with code {rc}")
            r["last_error"] = f"ended on its own after {took}{how}" + (f": {tail}" if tail else "")
        r["pid"] = None
        self._save()

    def list(self) -> list[dict]:
        out = []
        with self._lock:
            for r in self.items:
                self._reap(r)
        for r in self.items:
            out.append({
                "id": r["id"], "name": r["name"], "cmd": r["cmd"],
                "pid": r.get("pid"), "started_at": r.get("started_at"),
                "created_at": r.get("created_at"),
                "running": self._alive(r.get("pid")),
                "log": os.path.join(self.log_dir, f"{r['id']}.log"),
                "last_error": r.get("last_error"),
                "last_note": r.get("last_note"),
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
                mark = log.tell()
                if not procs.IS_WIN:
                    err = self._clear_stale_containers(r["cmd"], log)
                    if err:
                        log.write(f"[limcars] not started: {err}\n".encode())
                        log.close()
                        r["last_error"] = "start failed: " + err
                        r["last_note"] = None
                        self._save()
                        return {"ok": False, "error": err}
                if procs.IS_WIN:
                    # Windows recipes are PowerShell lines; own process group
                    # so the dashboard's console Ctrl+C doesn't reach them
                    proc = subprocess.Popen(  # noqa: S603
                        ["powershell", "-NoProfile", "-Command", r["cmd"]],
                        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                        cwd=os.path.expanduser("~"))
                else:
                    # shell=True is deliberate: recipes ARE user-authored shell lines
                    proc = subprocess.Popen(  # noqa: S602
                        r["cmd"], shell=True,  # noqa: S602
                        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                        start_new_session=True, cwd=os.path.expanduser("~"))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)[:300]}
            r["pid"] = proc.pid
            r["started_at"] = time.time()
            r["log_mark"] = mark
            r["last_error"] = r["last_note"] = None
            try:
                rc = proc.wait(timeout=self.START_GRACE_S)
            except subprocess.TimeoutExpired:
                rc = None
            log.close()  # the child holds its own copy of the fd
            if rc is not None and rc != 0:
                err = f"exited with code {rc} right after start: {_log_tail(log_path, mark)}"
                r["pid"] = None
                r["last_error"] = "start failed: " + err
                self._save()
                return {"ok": False, "error": err, "log": log_path}
            self._procs[r["id"]] = proc
            self._save()
            return {"ok": True, "pid": proc.pid, "log": log_path}

    @staticmethod
    def _clear_stale_containers(cmd: str, log) -> str | None:
        """Remove stopped containers holding a name this command's `docker run` needs.

        A live container with that name is left alone and reported: it is serving
        something, and docker would refuse the name anyway."""
        for prefix, name in docker_run_names(cmd):
            got = _docker(prefix + ["inspect", "-f", "{{.State.Status}}", name])
            if got is None or got.returncode != 0:
                continue                      # no such container (or no docker): nothing to clear
            state = got.stdout.strip()
            if state in _LIVE_STATES:
                return (f"container '{name}' is already {state}; stop it first "
                        f"(docker stop {name})")
            rm = _docker(prefix + ["rm", name])
            if rm is None or rm.returncode != 0:
                why = (rm.stderr.strip() if rm else "docker not runnable")[:200]
                return f"could not remove the stopped container '{name}': {why}"
            log.write(f"[limcars] removed the stopped container '{name}' ({state}) "
                      f"left by an earlier run\n".encode())
            log.flush()
        return None

    def stop(self, rid: str) -> dict:
        with self._lock:
            r = self._find(rid)
            if not r:
                return {"ok": False, "error": "no such recipe"}
            pid = int(r.get("pid") or 0)
            if not self._alive(pid):
                self._reap(r)
                r["pid"] = None
                self._save()
                return {"ok": True, "note": "was not running"}
            try:
                if not procs.HAS_PGRP:
                    procs.kill_tree(pid)
                    raise ProcessLookupError
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
                return {"ok": False, "error": f"stop: {exc}"}
            proc = self._procs.pop(r["id"], None)
            if proc:
                proc.poll()
            r["pid"] = None
            r["last_stop_at"] = time.time()
            self._save()
            return {"ok": True}
