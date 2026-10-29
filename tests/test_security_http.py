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


def test_one_query_string_cannot_ask_for_every_sample_ever_stored():
    """`?seconds=` reaches `store.history()`/`snapshot()` unfiltered otherwise, and those read
    under the store lock the sampler writes through: one LAN request with seconds=1e18 was a
    sampler stall and an OOM on a big database. The cap is retention + slack, so the answer a
    user gets is unchanged — only the cost is bounded."""
    from app.server import _window_seconds
    cfg = {"persist_seconds": 3600}
    assert _window_seconds({"seconds": ["1e18"]}, cfg) == 3900.0
    assert _window_seconds({"seconds": ["900"]}, cfg) == 900.0        # a real window passes through
    assert _window_seconds({}, cfg) == 3900.0                          # default = everything we keep
    for bad in ("abc", "-5", "0", "nan", "inf"):
        with pytest.raises(ValueError):
            _window_seconds({"seconds": [bad]}, cfg)


def test_the_ring_endpoint_refuses_garbage_instead_of_500ing(live):
    """Both ring readers used to call float()/int() on query values inline, so a browser
    extension or a scanner sending ?seconds=abc produced a traceback and a 500 instead of a 400."""
    for path in ("/api/history?seconds=abc", "/api/history?seconds=-1", "/api/savings-history?days=x"):
        code, body = _req(live, "127.0.0.1", "GET", path)
        assert code in (200, 400), f"{path} -> {code} {body!r} (expected 400, never 500)"
    code, _ = _req(live, "127.0.0.1", "GET", "/api/history?seconds=1e18")
    assert code == 200, "a huge window must be clamped, not refused"


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


# ------------------------------------------- GHSA-p9fw / GHSA-cwj4 / GHSA-gjf8 --
# Three reports from the private intake, each reproduced against a throwaway
# instance before being fixed, and pinned here afterwards. Two layers again:
# pure logic on the guard's helpers, then the headers a browser or a rebinding
# page actually sends, over a real socket.

def test_split_host_understands_ipv6_and_ports():
    assert srv._split_host("127.0.0.1:8951") == ("127.0.0.1", "8951")
    assert srv._split_host("[::1]:8951") == ("::1", "8951")
    assert srv._split_host("[::1]") == ("::1", None)
    assert srv._split_host("LocalHost") == ("localhost", None)
    assert srv._split_host("") == ("", None)
    assert srv._split_host(None) == ("", None)


def test_origin_tuple_refuses_the_values_the_old_check_skipped_on():
    """`urlparse("null").hostname` is None, so the old `if oh and ...` guard fell
    through and let a sandboxed iframe or a data: page post as localhost."""
    assert srv.origin_tuple("null") is None
    assert srv.origin_tuple("http://null") is None
    assert srv.origin_tuple("") is None
    assert srv.origin_tuple("ftp://127.0.0.1") is None
    assert srv.origin_tuple("http://127.0.0.1:8951") == ("http", "127.0.0.1", 8951)
    assert srv.origin_tuple("http://[::1]:8951") == ("http", "::1", 8951)
    assert srv.origin_tuple("https://limcars.home") == ("https", "limcars.home", 443)


def test_host_allowlist_covers_loopback_bind_and_operator_names():
    got = srv.host_allowlist({"host": "0.0.0.0", "allowed_hosts": ["limcars.home"]})
    assert {"localhost", "127.0.0.1", "::1", "limcars.home"} <= got
    # the address this machine answers on must be in there, or LAN access breaks
    assert srv.own_addresses() <= got
    assert "evil.example" not in got
    bound = srv.host_allowlist({"host": "10.1.2.3"})
    assert "10.1.2.3" in bound and "0.0.0.0" not in bound


def _guarded(ip, headers, cfg=None):
    h = _handler(ip, _Auth(enabled=False))
    h.app.cfg = cfg if cfg is not None else {"host": "127.0.0.1"}
    h.headers = _Hdrs(headers)
    return h


def test_guard_refuses_a_host_that_is_not_this_machine():
    h = _guarded(LOCAL, {"Host": ["evil.example:8951"]})
    assert h._guard_request(unsafe=False) is False
    assert h.responses and h.responses[0][0] == 421, h.responses


def test_guard_refuses_a_missing_host_header():
    h = _guarded(LOCAL, {})
    assert h._guard_request(unsafe=False) is False
    assert h.responses[0][0] == 400, h.responses


def test_guard_refuses_rebinding_where_host_equals_origin():
    """The exact pair the old check let through: both headers invented by the
    caller, both identical, TCP peer loopback."""
    h = _guarded(LOCAL, {"Host": ["rebind.example:8951"],
                         "Origin": ["http://rebind.example:8951"]})
    assert h._guard_request(unsafe=True) is False
    assert h.responses[0][0] in (421, 403), h.responses


def test_guard_refuses_null_origin_and_cross_port_localhost_origin():
    for origin in ("null", "http://127.0.0.1:1234", "http://localhost"):
        h = _guarded(LOCAL, {"Host": ["127.0.0.1:8951"], "Origin": [origin],
                             "Content-Type": ["application/json"], "Content-Length": ["2"]})
        assert h._guard_request(unsafe=True) is False, origin
        assert h.responses[0][0] == 403, (origin, h.responses)


def test_guard_demands_json_for_a_body_but_not_for_body_less_verbs():
    h = _guarded(LOCAL, {"Host": ["127.0.0.1:8951"], "Origin": ["http://127.0.0.1:8951"],
                         "Content-Type": ["text/plain"], "Content-Length": ["14"]})
    assert h._guard_request(unsafe=True) is False
    assert h.responses[0][0] == 415, h.responses          # the shape a cross-site form sends
    ok = _guarded(LOCAL, {"Host": ["127.0.0.1:8951"], "Origin": ["http://127.0.0.1:8951"],
                          "Content-Type": ["application/json"], "Content-Length": ["14"]})
    assert ok._guard_request(unsafe=True) is True and ok.responses == []
    start = _guarded(LOCAL, {"Host": ["127.0.0.1:8951"], "Origin": ["http://127.0.0.1:8951"]})
    assert start._guard_request(unsafe=True) is True, "start/stop send no body"
    cli = _guarded(LOCAL, {"Host": ["127.0.0.1:8951"], "Content-Type": ["application/json"],
                           "Content-Length": ["14"]})
    assert cli._guard_request(unsafe=True) is True, "curl and agents send no Origin"


def _req_raw(port, method, path, headers=None, body=None):
    """A request whose Host/Origin we choose ourselves -- http.client only fills
    Host in when the caller does not supply it."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()[:300].decode("utf-8", "replace")
    c.close()
    return r.status, data


def test_a_rebinding_pair_cannot_add_a_recipe(live):
    port = live
    H = {"Host": f"rebind.example:{port}", "Origin": f"http://rebind.example:{port}",
         "Content-Type": "application/json"}
    code, body = _req_raw(port, "POST", "/api/recipes", H,
                          json.dumps({"name": "rebind-poc", "cmd": "touch /tmp/limcars-rebind-poc"}))
    assert code in (421, 403), f"rebinding write was accepted: {code} {body}"
    code, body = _req_raw(port, "GET", "/api/recipes", {"Host": f"rebind.example:{port}"})
    assert code == 421, f"GET must fail the Host check too (command lines leak there): {code}"
    code, body = _req_raw(port, "GET", "/api/recipes", {"Host": f"127.0.0.1:{port}"})
    assert "rebind-poc" not in body, "the refused recipe must not have been stored"


def test_a_null_origin_form_cannot_add_a_recipe(live):
    port = live
    for headers in ({"Origin": "null", "Content-Type": "text/plain"},
                    {"Origin": f"http://127.0.0.1:1234", "Content-Type": "application/json"}):
        h = dict(headers, Host=f"127.0.0.1:{port}")
        code, body = _req_raw(port, "POST", "/api/recipes", h,
                              json.dumps({"name": "form-poc", "cmd": "touch /tmp/limcars-form-poc"}))
        assert code in (403, 415), f"{headers} was accepted: {code} {body}"
    # the real dashboard, same origin + json, must still be able to write
    ok = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
          "Content-Type": "application/json"}
    code, body = _req_raw(port, "POST", "/api/recipes", ok,
                          json.dumps({"name": "legit", "cmd": "true"}))
    assert code == 200, f"a same-origin write from the UI was locked out: {code} {body}"


@pytest.mark.skipif(_lan_ip() is None, reason="no non-loopback IPv4 on this box")
def test_the_first_password_cannot_be_created_from_the_lan(live):
    """GHSA-cwj4-xcmw-gpxp. /api/auth/set only asked for the current password once
    one existed, so before that any LAN peer could set it -- then own every gated
    write, while the owner found the network side locked with a password they
    never chose."""
    port, lan = live, _lan_ip()
    code, body = _req(port, lan, "POST", "/api/auth/set", {"new": "attacker-pw"})
    assert code == 428, f"remote first-password accepted: {code} {body}"
    assert "password" in body.lower() and "box" in body.lower(), f"refusal must say what to do: {body}"
    # and the gate it protects is still fail-closed for that peer
    code, body = _req(port, lan, "POST", "/api/recipes", {"name": "x", "cmd": "true"})
    assert code == 428, f"remote write without a password must stay closed: {code}"
    # the box itself may still bootstrap
    code, body = _req(port, "127.0.0.1", "POST", "/api/auth/set", {"new": "owner-pw"})
    assert code == 200, f"loopback bootstrap broke: {code} {body}"


@pytest.mark.skipif(_lan_ip() is None, reason="no non-loopback IPv4 on this box")
def test_reaching_the_dashboard_by_lan_address_still_works(live):
    """The regression that gets a Host allowlist reverted: it must accept the
    addresses this machine actually owns, not just the word 'localhost'."""
    port, lan = live, _lan_ip()
    code, body = _req_raw(port, "GET", "/api/health", {"Host": f"{lan}:{port}"})
    assert code == 200, f"LAN access by address broke: {code} {body}"


# ------------------------------------------------- control-plane password gate --
# Engine Stop and the price-table save are password-gated server-side, but their
# frontend call sites used plain fetch() and never attached X-Limcars-Pw. Every
# remote operator was therefore told "wrong password" about a password that was
# never compared, while the same button worked on the box itself (localhost is the
# admin domain). Pinned at both ends so neither half can regress silently.

def _gated(ip="192.168.1.50", pw="s3cret", good="s3cret"):
    h = _handler(ip, _Auth(enabled=True, good=good))
    h.headers = _Hdrs({"X-Limcars-Pw": [pw]} if pw is not None else {})
    return h


def test_remote_control_with_the_right_password_is_not_refused():
    h = _gated()
    assert h._require_control() is True, "the correct password must open the control gate"
    assert h.responses == [], h.responses


def test_remote_control_with_a_wrong_password_is_401_wrong_password():
    h = _gated(pw="nope")
    assert h._require_control() is False
    assert h.responses[0][0] == 401, h.responses
    assert "wrong password" in str(h.responses[0][1]), h.responses


def test_a_missing_password_says_none_was_sent_instead_of_wrong_password():
    """The wording that made this bug look like a bad password: an absent header is
    not a failed comparison, and must not spend the lockout budget either."""
    for gate, what in (("_require_control", "Engine and service control"),
                       ("_require_write", "Editing recipes"),
                       ("_require_read_traces", "Reading traces")):
        h = _gated(pw=None)
        call = getattr(h, gate)
        verdict = call({"pw": []}) if gate == "_require_read_traces" else call()
        assert verdict is False, gate
        code, payload = h.responses[0]
        assert code == 401, (gate, h.responses)
        text = str(payload)
        assert "wrong password" not in text, (gate, text)
        assert "needs the dashboard password" in text and what in text, (gate, text)
        assert h.app.auth.attempts == [], \
            f"{gate}: an absent password must never even reach the verifier"


def test_frontend_attaches_the_password_on_every_gated_verb():
    """A plain fetch() on a gated route is invisible to a server-side test suite —
    which is precisely how this shipped."""
    js = open(os.path.join(os.path.dirname(__file__), "..", "web", "js", "dashboard.js"),
              encoding="utf-8").read()
    assert "authFetch(`/api/engines-live/${pid}/stop`" in js, \
        "engine Stop must go through authFetch or remote operators cannot stop an engine"
    assert "authFetch('/api/config'" in js, \
        "the price-table save is gated by _require_write and must send the header"
