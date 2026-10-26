"""Portable process helpers. Linux keeps reading /proc directly (cheap, and
the behaviour the rest of the code was written against); everywhere else
(Windows, macOS) the same questions are answered through psutil."""
from __future__ import annotations

import os
import time

import psutil

IS_WIN = os.name == "nt"
LINUX_PROC = os.path.isdir("/proc/self")
HAS_PGRP = hasattr(os, "killpg")


def pids() -> list[int]:
    if LINUX_PROC:
        return [int(e) for e in os.listdir("/proc") if e.isdigit()]
    return psutil.pids()


def argv(pid: int) -> list[str]:
    if LINUX_PROC:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read().split(b"\0")
        except OSError:
            return []
        return [t.decode("utf-8", "replace") for t in raw if t]
    try:
        return [t for t in psutil.Process(pid).cmdline() if t]
    except (psutil.Error, OSError):
        return []


def alive(pid) -> bool:
    if not pid:
        return False
    if LINUX_PROC:
        return os.path.isdir(f"/proc/{int(pid)}")
    return psutil.pid_exists(int(pid))


def ppid(pid: int) -> int | None:
    """Parent pid; 0 when unknown, None when the process can't be read."""
    if LINUX_PROC:
        try:
            with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
                for ln in fh:
                    if ln.startswith("PPid:"):
                        return int(ln.split()[1])
            return 0
        except OSError:
            return None
    try:
        return psutil.Process(pid).ppid()
    except (psutil.Error, OSError):
        return None


def ancestors(pid: int) -> set[int]:
    try:
        return {p.pid for p in psutil.Process(pid).parents()}
    except psutil.Error:
        return set()


def kill_tree(pid: int, grace: float = 10.0) -> None:
    """No process groups (Windows): terminate the pid and every descendant,
    give them `grace` seconds, then kill what is left."""
    try:
        root = psutil.Process(pid)
        tree = root.children(recursive=True) + [root]
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as exc:
        raise PermissionError(str(exc)) from exc
    for p in tree:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            raise PermissionError(str(exc)) from exc
    left = _wait_gone(tree, grace)
    for p in left:
        try:
            p.kill()
        except psutil.Error:
            pass
    _wait_gone(left, 2)


def _wait_gone(tree: list, timeout: float) -> list:
    """Wait until every process exited; an unreaped zombie counts as gone."""
    deadline = time.monotonic() + timeout
    while True:
        left = []
        for p in tree:
            try:
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                    left.append(p)
            except psutil.Error:
                pass
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.1)
