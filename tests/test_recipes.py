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
    assert rs.list()[0]["last_error"] == "start failed: " + res["error"]


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


def _wait_ended(rs, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = rs.list()[0]
        if not row["running"]:
            return row
        time.sleep(0.2)
    raise AssertionError("recipe did not end")


def test_a_later_crash_shows_how_it_ended(tmp_path):
    rs, _rid, res = _start(tmp_path, "sleep 2.5; echo 'CUDA out of memory' >&2; exit 7")
    assert res["ok"], res
    row = _wait_ended(rs)
    assert row["pid"] is None
    assert row["last_error"].startswith("ended on its own after "), row
    assert "with code 7" in row["last_error"] and "CUDA out of memory" in row["last_error"]
    assert "===== start" not in row["last_error"]


def test_a_clean_finish_is_a_note_not_an_error(tmp_path):
    rs, _rid, res = _start(tmp_path, "sleep 2.5; echo done")
    assert res["ok"], res
    row = _wait_ended(rs)
    assert row["last_error"] is None
    assert row["last_note"].startswith("finished on its own after ") and "exit 0" in row["last_note"]


def test_an_end_seen_after_a_dashboard_restart_has_no_code(tmp_path):
    rs, rid, res = _start(tmp_path, "sleep 2.5; echo bye >&2; exit 3")
    assert res["ok"], res
    rs2 = R.Recipes(str(tmp_path / "data"))      # the new run did not spawn it
    time.sleep(3.0)
    rs._procs.pop(rid).wait()                     # the old run's child, reaped by its parent
    row = rs2.list()[0]
    assert not row["running"]
    assert row["last_error"] is None                # unknown exit is not shown as a failure
    assert "exit code unknown" in row["last_note"] and "bye" in row["last_note"]
    assert row["last_end"]["kind"] == "unknown" and row["last_end"]["code"] is None


def test_stop_is_not_reported_as_ending_on_its_own(tmp_path):
    rs, rid, res = _start(tmp_path, "sleep 30")
    assert res["ok"], res
    assert rs.stop(rid)["ok"]
    row = rs.list()[0]
    assert row["last_error"] is None and row["last_note"] is None


def test_a_new_start_clears_the_previous_end(tmp_path):
    rs, rid, res = _start(tmp_path, "sleep 2.5; exit 4")
    assert res["ok"], res
    assert _wait_ended(rs)["last_error"]
    assert rs.update(rid, None, "sleep 30")["ok"]
    try:
        assert rs.start(rid)["ok"]
        row = rs.list()[0]
        assert row["running"] and row["last_error"] is None and row["last_note"] is None
    finally:
        rs.stop(rid)


def test_a_crash_records_a_structured_end(tmp_path):
    rs, _rid, res = _start(tmp_path, "sleep 2.5; echo 'loading'; echo 'CUDA out of memory' >&2; "
                                     "echo 'UserWarning: leaked semaphore' >&2; exit 7")
    assert res["ok"], res
    end = _wait_ended(rs)["last_end"]
    assert end["kind"] == "crash" and end["code"] == 7 and end["took"] >= 2
    assert end["line"] == "CUDA out of memory"     # the explaining line, not the warning noise
    assert "loading" in end["tail"] and end["at"]


def test_key_line_prefers_errors_and_skips_warning_noise():
    assert R.key_line(["a", "docker: Error response from daemon: Conflict.", "b"]).startswith("docker:")
    assert R.key_line(["INFO: Application shutdown complete.",
                       "UserWarning: resource_tracker: There appear to be 1 leaked"]) == \
        "INFO: Application shutdown complete."
    assert R.key_line([]) == ""


def test_old_rows_get_a_compact_end_from_their_message(tmp_path):
    rs = R.Recipes(str(tmp_path / "data"))
    rid = rs.add("old", "true")["recipe"]["id"]
    rs.items[0]["last_error"] = ("ended on its own after 140h 00m: --max-time 3 x || { echo "
                                 "'engine did not come up'; exit 1; } | ===== start 2026-10-30 "
                                 "07:28:08 :: cmd | engine live on 127.0.0.1:8000")
    end = rs.list()[0]["last_end"]
    assert end["kind"] == "unknown" and end["line"] == "engine live on 127.0.0.1:8000"
    rs.items[0]["last_error"] = "start failed: exited with code 125 right after start: docker: Error x"
    end = rs.list()[0]["last_end"]
    assert end["kind"] == "start_failed" and end["code"] == 125 and rid
