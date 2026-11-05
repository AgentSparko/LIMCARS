"""Recipe Start: stale `--name` containers are cleared, instant failures are reported."""
import os
import stat
import sys
import time

import pytest

from app import recipes as R

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell recipes")

FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_DOCKER_LOG"
case "$1" in
  inspect) [ -n "$FAKE_STATE" ] || { echo "Error: No such object: $4" >&2; exit 1; }
           echo "$FAKE_STATE" ;;
  rm) exit 0 ;;
  run) [ "$FAKE_RUN" = fail ] && { echo 'docker: Error response from daemon: Conflict.' >&2; exit 125; }
       exec sleep 30 ;;
esac
"""


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "docker"
    exe.write_text(FAKE_DOCKER, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(calls))
    monkeypatch.delenv("FAKE_STATE", raising=False)
    monkeypatch.delenv("FAKE_RUN", raising=False)
    return lambda: calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def _start(tmp_path, cmd):
    rs = R.Recipes(str(tmp_path / "data"))
    rid = rs.add("r", cmd)["recipe"]["id"]
    return rs, rid, rs.start(rid)


def test_docker_run_names_parses_name_forms():
    assert R.docker_run_names("docker run --name a img") == [(["docker"], "a")]
    assert R.docker_run_names("sudo -n docker run -d --name=b img x") == \
        [(["sudo", "-n", "docker"], "b")]
    assert R.docker_run_names("docker rm -f c >/dev/null 2>&1; docker run --name c i") == \
        [(["docker"], "c")]
    assert R.docker_run_names("vllm serve m --port 8000") == []
    assert R.docker_run_names("docker run img --name inner-arg-only-after") == \
        [(["docker"], "inner-arg-only-after")]


def test_a_stopped_container_with_the_name_is_removed_first(tmp_path, monkeypatch, fake_docker):
    monkeypatch.setenv("FAKE_STATE", "exited")
    rs, rid, res = _start(tmp_path, "docker run --name eng img")
    try:
        assert res["ok"], res
        calls = fake_docker()
        assert calls.index("rm eng") < calls.index("run --name eng img"), calls
        with open(res["log"], encoding="utf-8") as fh:
            assert "removed the stopped container 'eng'" in fh.read()
    finally:
        rs.stop(rid)


def test_a_live_container_with_the_name_is_left_alone(tmp_path, monkeypatch, fake_docker):
    monkeypatch.setenv("FAKE_STATE", "running")
    rs, _rid, res = _start(tmp_path, "docker run --name eng img")
    assert not res["ok"] and "already running" in res["error"], res
    assert not any(c.startswith(("rm", "run")) for c in fake_docker())
    assert rs.list()[0]["last_error"] == res["error"]


def test_no_container_yet_just_runs(tmp_path, fake_docker):
    rs, rid, res = _start(tmp_path, "docker run --name eng img")
    try:
        assert res["ok"], res
        assert not any(c.startswith("rm") for c in fake_docker())
    finally:
        rs.stop(rid)


def test_a_command_that_dies_at_once_is_a_failed_start(tmp_path, monkeypatch, fake_docker):
    monkeypatch.setenv("FAKE_RUN", "fail")
    rs, _rid, res = _start(tmp_path, "docker run --name eng img")
    assert not res["ok"], res
    assert "code 125" in res["error"] and "Conflict" in res["error"], res
    row = rs.list()[0]
    assert not row["running"] and row["pid"] is None and "Conflict" in row["last_error"]


def test_a_long_running_command_starts_and_stops(tmp_path):
    rs, rid, res = _start(tmp_path, "sleep 30")
    assert res["ok"] and rs.list()[0]["running"], res
    assert rs.list()[0]["last_error"] is None
    assert rs.stop(rid)["ok"]
    time.sleep(0.3)
    assert not rs.list()[0]["running"]
