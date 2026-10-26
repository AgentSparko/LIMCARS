"""Windows/macOS code paths (psutil instead of /proc, no process groups),
exercised on any OS by forcing the non-/proc branch, plus a real boot."""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil
import pytest

from app import live_engines, procs

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def no_proc(monkeypatch):
    monkeypatch.setattr(procs, "LINUX_PROC", False)


def test_psutil_argv_alive_ppid(no_proc):
    me = os.getpid()
    assert procs.argv(me) == [t for t in psutil.Process(me).cmdline() if t]
    assert procs.alive(me) and not procs.alive(0)
    assert procs.ppid(me) == os.getppid()
    assert me in procs.pids()


def test_kill_tree_takes_children_down():
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); time.sleep(600)"
    p = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.time() + 10
        while not psutil.Process(p.pid).children() and time.time() < deadline:
            time.sleep(0.05)
        kids = [c.pid for c in psutil.Process(p.pid).children(recursive=True)]
        assert kids
        procs.kill_tree(p.pid, grace=5)
        p.wait(5)
        assert not any(psutil.pid_exists(k) and psutil.Process(k).status() != psutil.STATUS_ZOMBIE
                       for k in kids)
    finally:
        if p.poll() is None:
            p.kill()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return urllib.request.urlopen(url, timeout=2)
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"{url} never answered")


FAKE_ENGINE = """
import http.server, json, sys
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps({"data": [{"id": "m", "owned_by": "vllm"}]}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(b)
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def test_engine_scan_without_proc_finds_listening_engine(no_proc):
    port = _free_port()
    p = subprocess.Popen([sys.executable, "-c", FAKE_ENGINE, str(port)])
    try:
        _wait_http(f"http://127.0.0.1:{port}/v1/models")
        rows = live_engines.LiveEngines().scan(force=True)
        hit = [r for r in rows if port in r["ports"]]
        assert hit and hit[0]["type"] == "vLLM" and hit[0]["model"] == "m"
    finally:
        p.kill()
        p.wait(5)


def test_dashboard_boots_and_serves_modules(tmp_path):
    for d in ("app", "web"):
        shutil.copytree(ROOT / d, tmp_path / d, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text(
        json.dumps({"engines": [], "services": [], "host": "127.0.0.1"}), encoding="utf-8")
    port = _free_port()
    env = {**os.environ, "LIMCARS_PORT": str(port), "LIMCARS_HOST": "127.0.0.1"}
    p = subprocess.Popen([sys.executable, "-m", "app"], cwd=tmp_path, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        html = _wait_http(base + "/").read().decode("utf-8")
        assert "<html" in html.lower()
        for src in re.findall(r'src="([^"]+\.js)(?:\?[^"]*)?"', html)[:3]:
            r = urllib.request.urlopen(base + "/" + src.lstrip("/"), timeout=5)
            assert "javascript" in r.headers["Content-Type"]
        snap = json.load(urllib.request.urlopen(base + "/api/snapshot?seconds=60", timeout=5))
        assert "engines" in snap
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/..%2fapp%2fconfig.py", timeout=5)
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def test_windows_exe_names_classify(monkeypatch):
    monkeypatch.setattr(procs, "IS_WIN", True)
    monkeypatch.setattr(procs, "argv", lambda pid: [r"C:\llama\llama-server.exe", "--port", "8080"])
    assert live_engines._classify(1234) == "llama.cpp"


def test_windows_worker_rows_merge_under_launcher_and_stop_kills_it(monkeypatch, no_proc):
    monkeypatch.setattr(procs, "HAS_PGRP", False)
    code = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
            "'import time; time.sleep(600)', 'vllm']); time.sleep(600)")
    p = subprocess.Popen([sys.executable, "-c", code, "vllm"])
    try:
        deadline = time.time() + 10
        while not psutil.Process(p.pid).children() and time.time() < deadline:
            time.sleep(0.05)
        worker = psutil.Process(p.pid).children()[0].pid
        le = live_engines.LiveEngines()
        rows = [r for r in le.scan(force=True) if r["pid"] in (p.pid, worker)]
        assert len(rows) == 1 and rows[0]["pid"] == p.pid and worker in rows[0]["pids_extra"]
        assert le.stop(worker)["ok"]
        p.wait(10)
        assert not psutil.pid_exists(worker) or psutil.Process(worker).status() == psutil.STATUS_ZOMBIE
    finally:
        if p.poll() is None:
            p.kill()



SERVER_PY = """
import http.server, json, subprocess, sys
if len(sys.argv) > 3:  # launch an independent server, then serve our own port
    subprocess.Popen([sys.executable, __file__, sys.argv[3], "vllm"])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps({"data": [{"id": "m" + sys.argv[1], "owned_by": "vllm"}]}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(b)
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def test_windows_child_server_stays_separate_card(monkeypatch, no_proc, tmp_path):
    monkeypatch.setattr(procs, "HAS_PGRP", False)
    script = tmp_path / "server.py"
    script.write_text(SERVER_PY, encoding="utf-8")
    p1, p2 = _free_port(), _free_port()
    p = subprocess.Popen([sys.executable, str(script), str(p1), "vllm", str(p2)])
    try:
        _wait_http(f"http://127.0.0.1:{p1}/v1/models")
        _wait_http(f"http://127.0.0.1:{p2}/v1/models")
        rows = live_engines.LiveEngines().scan(force=True)
        cards = [r for r in rows if p1 in r["ports"] or p2 in r["ports"]]
        assert len(cards) == 2 and all(r["pid"] for r in cards)
    finally:
        for c in psutil.Process(p.pid).children(recursive=True):
            c.kill()
        p.kill()
        p.wait(5)


TREE_PY = """
import http.server, json, socket, subprocess, sys, time
mode = sys.argv[1]
if mode == "launcher":   # vllm-like API server -> unclassified helper -> worker
    subprocess.Popen([sys.executable, __file__, "helper", sys.argv[3]])
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            b = json.dumps({"data": [{"id": "m", "owned_by": "vllm"}]}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(b)
    http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
elif mode == "helper":
    subprocess.Popen([sys.executable, __file__, "worker", sys.argv[2], "vllm"])
    time.sleep(600)
else:                    # worker with an internal, non-OpenAI listener
    s = socket.socket(); s.bind(("127.0.0.1", int(sys.argv[2]))); s.listen()
    time.sleep(600)
"""


def test_windows_worker_behind_helper_with_internal_port_joins_launcher(
        monkeypatch, no_proc, tmp_path):
    monkeypatch.setattr(procs, "HAS_PGRP", False)
    script = tmp_path / "tree.py"
    script.write_text(TREE_PY, encoding="utf-8")
    p1, p2 = _free_port(), _free_port()
    p = subprocess.Popen([sys.executable, str(script), "launcher", str(p1), str(p2), "vllm"])
    try:
        _wait_http(f"http://127.0.0.1:{p1}/v1/models")
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", p2), 1).close()
                break
            except OSError:
                time.sleep(0.1)
        worker = next(c.pid for c in psutil.Process(p.pid).children(recursive=True)
                      if "worker" in c.cmdline())
        le = live_engines.LiveEngines()
        rows = [r for r in le.scan(force=True) if r["pid"] in (p.pid, worker)]
        assert len(rows) == 1 and rows[0]["pid"] == p.pid
        assert worker in rows[0]["pids_extra"]
        assert le.stop(worker)["ok"]
        p.wait(10)
    finally:
        if p.poll() is None:
            for c in psutil.Process(p.pid).children(recursive=True):
                c.kill()
            p.kill()


CHAIN_PY = """
import http.server, json, subprocess, sys, time
mode = sys.argv[1]
if mode == "a":        # classified engine A
    subprocess.Popen([sys.executable, __file__, "b", sys.argv[2]])
    time.sleep(600)
elif mode == "b":      # unclassified OpenAI server B (port-only row)
    subprocess.Popen([sys.executable, __file__, "c", "vllm"])
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            b = json.dumps({"data": [{"id": "m", "owned_by": "x"}]}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(b)
    http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
else:                  # classified engine C under B
    time.sleep(600)
"""


def test_windows_unclassified_server_is_a_boundary(monkeypatch, no_proc, tmp_path):
    monkeypatch.setattr(procs, "HAS_PGRP", False)
    script = tmp_path / "chain.py"
    script.write_text(CHAIN_PY, encoding="utf-8")
    port = _free_port()
    a = subprocess.Popen([sys.executable, str(script), "a", str(port), "vllm"])
    try:
        _wait_http(f"http://127.0.0.1:{port}/v1/models")
        deadline = time.time() + 10
        c = None
        while c is None and time.time() < deadline:
            c = next((p.pid for p in psutil.Process(a.pid).children(recursive=True)
                      if p.cmdline()[2:3] == ["c"]), None)
            time.sleep(0.05)
        le = live_engines.LiveEngines()
        rows = le.scan(force=True)
        assert {r["pid"] for r in rows if r["pid"] in (a.pid, c)} == {a.pid, c}
        assert le.stop(c)["ok"]
        time.sleep(0.5)
        assert a.poll() is None   # A survives
    finally:
        for p in psutil.Process(a.pid).children(recursive=True) if a.poll() is None else []:
            p.kill()
        a.kill()
