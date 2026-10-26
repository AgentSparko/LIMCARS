"""The security model, tested at the layer an attacker actually reaches.

The rest of the suite tests the pieces -- the auth ladder, the ssh-option
allowlist, the escaping contract -- but nothing here asserted the thing the
project actually promises: that a machine on your LAN cannot add a recipe, stop
your engine, rewrite your pricing or read your traces while no password is set.
That gap was found by an independent review of the test suite, and it is the
worst kind, because the gate was verified by an external script that lived
outside the repo and therefore proved nothing to anyone cloning this.

Two layers of test, deliberately:

1. Gate logic, deterministic and network-free. A Handler built with __new__ and a
   spoofed client_address exercises exactly the branch that decides whether a
   request may proceed. This runs anywhere, on any CI box, with no second IP.

2. Real HTTP over a real socket, using this host's non-loopback address. This is
   what catches "the gate is right but the route never calls it" -- a class of bug
   unit tests cannot see. It skips itself if the machine has no non-loopback IPv4,
   and says so loudly rather than passing silently.

Depends on the security lanes (fail-closed writes, control-plane auth, pricing
gate, trace-read gate) being present, so it lands after them.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import server as srv                    # noqa: E402
from app.nodes import NodeSampler, sanitise_ssh_opts   # noqa: E402


# --------------------------------------------------------------- gate logic ----
class _Auth:
    def __init__(self, enabled=False, good=""):
        self.enabled = enabled
        self._good = good
        self.attempts = []

    def attempt(self, ip, pw):
        self.attempts.append((ip, pw))
        return (bool(self._good) and pw == self._good), 0


class _App:
    def __init__(self, auth):
        self.auth = auth
        self.cfg = {}


class _Hdrs:
    """Stand-in for http.client's message: the gate reads get_all() so a
    duplicated header can be detected, and get() for the Authorization form."""

    def __init__(self, values=None):
        self._v = values or {}

    def get_all(self, name):
        return self._v.get(name, [])

    def get(self, name, default=""):
        return self._v.get(name, [default])[0] if name in self._v else default


def _handler(ip, auth, pw=None):
    """A Handler with just enough surface for the gate helpers. Built with
    __new__ so no socket is bound and no BaseHTTPRequestHandler constructor runs."""
    h = srv.Handler.__new__(srv.Handler)
    h.client_address = (ip, 5150)
    h.app = _App(auth)
    h.headers = _Hdrs({"X-Limcars-Pw": [pw]} if pw is not None else {})
    h.responses = []
    # the gate answers itself: refusals go out through _json(payload, code=...),
    # so that is what a test must observe -- stubbing only _err would let a real
    # refusal happen with the wrong status and still look green.
    h._err = lambda code, msg, **kw: (h.responses.append((code, msg)), None)[1]
    h._json = lambda payload=None, code=200, **kw: (h.responses.append((code, payload)), None)[1]
    return h


REMOTE, LOCAL = "203.0.113.9", "127.0.0.1"


def test_no_password_still_refuses_a_remote_write():
    """The default posture: nobody configured a password, so a LAN peer gets
    nothing. This is the fail-closed rule -- 'auth is unset' must not mean 'open'."""
    h = _handler(REMOTE, _Auth(enabled=False))
    assert h._require_write() is False, "remote write allowed with no password configured"
    assert h.responses and h.responses[0][0] == 428, h.responses
    assert "password" in str(h.responses[0][1]).lower(), "the refusal must say what to do"


def test_control_plane_verbs_need_the_password_too():
    """Stopping a KV-warm engine is an outage, not a toggle, and it goes through a
    separate gate -- which is exactly why it needs its own test: a route can call
    one gate and forget the other."""
    h = _handler(REMOTE, _Auth(enabled=False))
    assert h._require_control() is False, "remote control-plane verb allowed with no password"
    assert h.responses and h.responses[0][0] == 428, h.responses
    h2 = _handler(LOCAL, _Auth(enabled=False))
    assert h2._require_control() is True and h2.responses == []


def test_loopback_admin_works_without_a_password():
    """The regression that makes security fixes get reverted: if the operator
    cannot configure their own box, the gate is removed within a week."""
    h = _handler(LOCAL, _Auth(enabled=False))
    assert h._require_write() is True
    assert h.responses == []


def test_remote_write_with_password_still_works():
    h = _handler(REMOTE, _Auth(enabled=True, good="hunter2"), pw="hunter2")
    assert h._require_write() is True, "correct password must not be refused"


def test_wrong_password_is_refused_and_recorded():
    auth = _Auth(enabled=True, good="hunter2")
    h = _handler(REMOTE, auth, pw="wrong")
    assert h._require_write() is False
    assert auth.attempts == [(REMOTE, "wrong")], "the attempt must reach the lockout ladder"


def test_duplicated_password_header_is_not_a_bypass():
    """A split/duplicated X-Limcars-Pw is treated as absent, never as 'first one
    wins', so request smuggling cannot smuggle a password through."""
    class _Hdrs:
        def get_all(self, name):
            return ["hunter2", "attacker"] if name == "X-Limcars-Pw" else []

        def get(self, name, default=""):
            return default
    auth = _Auth(enabled=True, good="hunter2")
    h = _handler(REMOTE, auth)
    h.headers = _Hdrs()
    assert h._require_write() is False, "duplicated header must not authenticate"


def test_loopback_detection_uses_the_peer_address_only():
    """X-Forwarded-For must not be able to make a remote peer look local."""
    h = _handler(REMOTE, _Auth(enabled=True, good="x"))
    h.headers = _Hdrs({"X-Forwarded-For": ["127.0.0.1"]})
    assert h._is_local() is False
    assert h._require_write() is False


# ------------------------------------------------------ ssh exec-boundary ------
class _Broker:
    def publish(self, *a, **k):
        pass


def _node(opts):
    return NodeSampler({"id": "n1", "ssh": "peer.example"}, {}, _Broker()) if opts is None \
        else NodeSampler({"id": "n1", "ssh": "peer.example", "ssh_opts": opts}, {}, _Broker())


def test_vetted_options_cannot_reach_the_ssh_command_line():
    n = _node(["-o", "ProxyCommand=nc -X connect evil %h %p", "-o", "BatchMode=yes"])
    argv = n.argv()
    joined = " ".join(argv)
    assert "ProxyCommand" not in joined, f"refused option reached argv: {argv}"
    assert n.error and "refused unsafe ssh_opts" in n.error


def test_safe_options_are_spliced_before_the_double_dash():
    """Position matters: options after "--" would be handed to the remote shell
    instead of ssh, and a name before it that ssh parses as an option is the
    execution path the allowlist exists to block."""
    n = _node(["-o", "ConnectTimeout=5", "-v"])
    argv = n.argv()
    assert "--" in argv, argv
    sep = argv.index("--")
    assert "ConnectTimeout=5" in argv[:sep], f"vetted option missing from the option block: {argv}"
    assert "-v" in argv[:sep]
    assert argv[sep + 1] == "peer.example", "target must follow the separator"


def test_refused_options_never_spawn_ssh(monkeypatch):
    import subprocess as sp
    called = []
    monkeypatch.setattr(sp, "run", lambda *a, **k: called.append(a))
    n = _node(["-o", "LocalCommand=touch /tmp/limcars-pwned"])
    n._tick()
    assert called == [], "subprocess.run was reached despite refused options"


def test_allowlist_is_an_allowlist_not_a_blocklist():
    """Anything unknown is refused: 'we did not think it dangerous' is not a
    security property, which is how ProxyCommand got through the first time."""
    kept, rejected = sanitise_ssh_opts(["--oProxyCommand=x", "-X11", "$(id)", "-v"])
    assert kept == ["-v"], kept
    assert len(rejected) == 3


# ------------------------------------------------------- real HTTP behaviour ---
def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


@pytest.fixture(scope="module")
def live():
    """Boot the real app in a subprocess with a throwaway data dir. If this
    cannot start, the tests below fail -- they do not skip into silence."""
    import tempfile
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = tempfile.mkdtemp(prefix="limcars-sectest-")
    port = 8990 + os.getpid() % 90
    env = dict(os.environ, LIMCARS_PORT=str(port), LIMCARS_HOST="0.0.0.0",
               # NOT just the database: this fixture boots with cwd=<repo>, so
               # config.json, auth.json and recipes.json would otherwise be the
               # real files next to the running dashboard. A test here once
               # POSTed /api/auth/set and put a live password on production.
               LIMCARS_DATA=data,
               LIMCARS_DB=os.path.join(data, "h.db"))
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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
        out = (proc.stdout.read().decode("utf-8", "replace") if proc.stdout else "")[:800]
        proc.kill()
        pytest.fail(f"dashboard did not come up for the security suite:\n{out}")
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def _req(port, host, method, path, body=None):
    c = http.client.HTTPConnection(host, port, timeout=5)
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=hdrs)
    r = c.getresponse()
    data = r.read()[:300].decode("utf-8", "replace")
    c.close()
    return r.status, data


def test_remote_writes_are_refused_over_a_real_socket(live):
    """End-to-end proof of the flagship gate. A unit test can get the predicate
    right while the route forgets to call it; only a socket catches that."""
    ip = _lan_ip()
    if not ip:
        pytest.skip("no non-loopback IPv4 on this host -- remote path untestable here "
                    "(deliberately loud: this is the test that proves the default is safe)")
    st, body = _req(live, ip, "POST", "/api/recipes", {"name": "x", "cmd": "true"})
    assert st == 428, f"remote recipe write returned {st}: {body}"
    st, _ = _req(live, ip, "POST", "/api/engines-live/1/stop", {})
    assert st == 428, "remote engine stop must be refused"
    st, _ = _req(live, ip, "POST", "/api/services/x/stop", {})
    assert st == 428, "remote service stop must be refused"
    st, _ = _req(live, ip, "POST", "/api/config", {"pricing": {"gpt-a": 0.0}})
    assert st == 428, "remote pricing edit must be refused"
    st, _ = _req(live, ip, "GET", "/api/session-tokens")
    assert st in (401, 428), "trace reads must require the password remotely"


def test_loopback_administration_still_works(live):
    st, _ = _req(live, "127.0.0.1", "POST", "/api/recipes", {"name": "ok", "cmd": "true"})
    assert st == 200, f"loopback admin must keep working with no password: {st}"


def test_pricing_values_are_validated_not_trusted(live):
    """pricing became web-editable, and every $ figure derives from it; a string
    there poisons the money endpoints and once crashed boot."""
    st, _ = _req(live, "127.0.0.1", "POST", "/api/config", {"pricing": {"m": "free"}})
    assert st == 400, "non-numeric price must be rejected"
    st, _ = _req(live, "127.0.0.1", "POST", "/api/config", {"pricing": {"m": 2.5}})
    assert st == 200, "valid price must be accepted"
