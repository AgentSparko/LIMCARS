"""Stopping a containerised engine.

A `docker run` engine puts root's process behind the pid the dashboard scanned, so the
kill that works for a native engine raises PermissionError. The operator is usually in
the `docker` group, so docker itself can stop it — and `docker stop` is also the *better*
primitive: the engine releases the GPU instead of being SIGKILLed mid-request.

The container handle must come from /proc/<pid>/cgroup for the pid that was clicked,
never from the request body, or an engine-stop button becomes "stop any container".
"""
import os
import subprocess
import time

import pytest

from app import docker_recipe, live_engines, procs

# the killpg / os.getpgid half of stop() is POSIX-only; the worker branch is not
_POSIX = hasattr(os, "getpgid")
posix_only = pytest.mark.skipif(not _POSIX, reason="POSIX process groups (os.getpgid)")

CID = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _stub(monkeypatch, pid=4242, classify="vLLM"):
    """A LiveEngines that believes `pid` is a live engine, without scanning the box."""
    le = live_engines.LiveEngines()
    monkeypatch.setattr(live_engines.procs, "HAS_PGRP", True)
    if _POSIX:      # the worker test runs on Windows too, where os.getpgid doesn't exist
        monkeypatch.setattr(live_engines.os, "getpgid",
                            lambda p: 99999 if p == 0 else pid)   # never our own group
    monkeypatch.setattr(live_engines.procs, "alive", lambda p: True)
    monkeypatch.setattr(live_engines, "_cmdline", lambda p: "/usr/bin/vllm serve /models/x")
    monkeypatch.setattr(live_engines, "_classify", lambda p: classify)
    monkeypatch.setattr(live_engines, "_gpu_holders", lambda: {})
    le._last_by_pid[pid] = (time.time(), classify)
    monkeypatch.setattr(le, "scan", lambda force=False: [])
    return le, pid


def _deny_killpg(monkeypatch):
    def boom(pgid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(live_engines.os, "killpg", boom)


def _fake_docker(monkeypatch, rc=0, err=""):
    calls = []

    class R:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[1] == "inspect":
            return R(rc, "/limcars-engine-probe\n")
        return R(rc, "", err)

    monkeypatch.setattr(live_engines.subprocess, "run", run)
    return calls


@posix_only
def test_a_root_owned_container_is_stopped_through_docker(monkeypatch):
    le, pid = _stub(monkeypatch)
    _deny_killpg(monkeypatch)
    monkeypatch.setattr(docker_recipe, "container_id", lambda p: CID)
    calls = _fake_docker(monkeypatch)
    out = le.stop(pid)
    assert out["ok"] is True, out
    assert out["stopped_container"] == "limcars-engine-probe", out
    assert out["via"] == "docker stop"
    stop_calls = [c for c in calls if "stop" in c]
    assert stop_calls[0][:2] == ["docker", "stop"], stop_calls
    assert stop_calls[0][-1] == CID, "must address the container id derived from the pid"


@posix_only
def test_docker_failure_is_reported_verbatim_instead_of_claiming_success(monkeypatch):
    le, pid = _stub(monkeypatch)
    _deny_killpg(monkeypatch)
    monkeypatch.setattr(docker_recipe, "container_id", lambda p: CID)
    _fake_docker(monkeypatch, rc=1, err="Cannot connect to the Docker daemon")
    out = le.stop(pid)
    assert out["ok"] is False and "Cannot connect to the Docker daemon" in out["error"], out


@posix_only
def test_no_docker_binary_falls_back_to_the_honest_message(monkeypatch):
    le, pid = _stub(monkeypatch)
    _deny_killpg(monkeypatch)
    monkeypatch.setattr(docker_recipe, "container_id", lambda p: CID)

    def missing(cmd, **kw):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(live_engines.subprocess, "run", missing)
    out = le.stop(pid)
    assert out["ok"] is False and "root-owned" in out["error"], out


@posix_only
def test_a_native_root_process_still_says_stop_it_where_it_was_started(monkeypatch):
    """systemd-owned, not containerised: docker cannot help and must not be tried."""
    le, pid = _stub(monkeypatch)
    _deny_killpg(monkeypatch)
    monkeypatch.setattr(docker_recipe, "container_id", lambda p: None)
    calls = _fake_docker(monkeypatch)
    out = le.stop(pid)
    assert out["ok"] is False and "systemd" in out["error"], out
    assert not [c for c in calls if c[1] == "stop"], "no docker call for a non-container pid"


@posix_only
def test_the_container_handle_never_comes_from_the_request(monkeypatch):
    """Only the pid crosses the wire; container_id() is the sole source of the handle."""
    seen = {}
    le, pid = _stub(monkeypatch)
    _deny_killpg(monkeypatch)
    monkeypatch.setattr(docker_recipe, "container_id", lambda p: seen.setdefault("pid", p) and CID)
    calls = _fake_docker(monkeypatch)
    out = le.stop(pid)
    assert out["ok"] is True
    assert seen["pid"] == pid, "the handle must be derived from the clicked pid"
    for c in calls:
        assert str(pid) not in c[2:], f"pid must not be usable as a docker argument: {c}"


def test_the_worker_branch_also_reaches_the_container(monkeypatch):
    """A worker pid has no own process group; its PermissionError must route too."""
    le, pid = _stub(monkeypatch)
    monkeypatch.setattr(live_engines.procs, "HAS_PGRP", False)
    monkeypatch.setattr(live_engines.procs, "ancestors", lambda p: [])
    monkeypatch.setattr(live_engines.procs, "kill_tree",
                        lambda p: (_ for _ in ()).throw(PermissionError(1, "nope")))
    monkeypatch.setattr(docker_recipe, "container_id", lambda p: CID)
    _fake_docker(monkeypatch)
    out = le.stop(pid)
    assert out["ok"] is True and out["via"] == "docker stop", out
