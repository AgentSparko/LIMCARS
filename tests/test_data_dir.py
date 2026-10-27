"""A test that mutates the operator's live state is not a test.

Born from a real incident during development: the security suite boots the real app with
cwd=<repo> and only overrode LIMCARS_DB, so config.json and auth.json resolved to the same
files the running dashboard on :8951 uses. One test's POST /api/auth/set therefore created a
scrypt password in the LIVE data dir — and the operator's dashboard started reporting
`{"enabled": true}` for a password nobody had chosen.

The fix is a data-dir override (LIMCARS_DATA) honoured by every state path. These tests pin
both halves: the knob works, and a booted instance keeps its credentials out of the repo.
"""
import http.client
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402


def test_data_dir_honours_the_knob_and_defaults_to_the_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("LIMCARS_DATA", raising=False)
    assert config.data_dir() == ROOT / "data"
    monkeypatch.setenv("LIMCARS_DATA", str(tmp_path))
    assert config.data_dir() == tmp_path


def test_booted_instance_keeps_its_state_out_of_the_repo(tmp_path):
    """POST a password the way the incident did, then prove the repo never saw it."""
    repo_auth = ROOT / "data" / "auth.json"
    repo_cfg = ROOT / "data" / "config.json"
    before = {p: (p.exists(), p.stat().st_mtime_ns if p.exists() else None)
              for p in (repo_auth, repo_cfg)}

    env = dict(os.environ, LIMCARS_PORT=str(8995 + os.getpid() % 40),
               LIMCARS_HOST="127.0.0.1", LIMCARS_DATA=str(tmp_path),
               LIMCARS_DB=str(tmp_path / "h.db"))
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = int(env["LIMCARS_PORT"])
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/api/health")
                if c.getresponse().status == 200:
                    c.close()
                    break
                c.close()
            except OSError:
                time.sleep(0.3)
        else:
            pytest.fail("instance did not boot")

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/auth/set",
                  headers={"content-type": "application/json"},
                  body=json.dumps({"new": "incident-guard-pw"}))
        body = json.loads(c.getresponse().read() or b"{}")
        c.close()
        assert body.get("ok") is True, body

        # the state landed where we pointed it
        assert (tmp_path / "auth.json").exists(), "auth.json did not follow LIMCARS_DATA"
        assert json.loads((tmp_path / "auth.json").read_text(encoding="utf-8")).get("hash"), \
            "password was not stored in the isolated dir"
        # ...and nowhere else
        for p, (existed, ns) in before.items():
            now = (p.exists(), p.stat().st_mtime_ns if p.exists() else None)
            assert now == (existed, ns), f"{p.name} was written by a test run"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_pricing_file_follows_the_same_knob(tmp_path, monkeypatch):
    """pricing.json is read through config's data dir too, so an isolated install
    cannot accidentally pick up (or overwrite) the operator's price table."""
    monkeypatch.setenv("LIMCARS_DATA", str(tmp_path))
    from importlib import reload
    from app import pricing
    reload(pricing)
    try:
        assert pricing.DATA == tmp_path
        assert pricing.FILE == tmp_path / "pricing.json"
        assert pricing.load() == {}        # nothing there -> no price, never a guess
    finally:
        monkeypatch.delenv("LIMCARS_DATA", raising=False)
        reload(pricing)
