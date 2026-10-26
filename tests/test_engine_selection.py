"""The selected engine must drive the money endpoints.

Regression lock for a defect that shipped in an earlier contribution: the
header switcher existed and the client looked correct, but the client sent
?engine= only on /api/snapshot. /api/counters, /api/engine-info and
/api/session-tokens each resolved the engine with next(iter(engines.all())), so
with two or more engines configured the dashboard printed one engine's name
above another engine's tokens, savings and lost-potential figures. Nothing
crashed; the numbers were simply attributed to the wrong engine.

These tests exercise the real handler rather than the helper alone, because a
helper nobody calls is exactly how the original bug survived review.
"""
import inspect
import pathlib

from app.server import Handler


class _Sampler:
    def __init__(self, sid, model, url):
        self.id, self.model, self.url, self.label = sid, model, url, sid


class _Engines:
    def __init__(self, samplers):
        self._s = samplers

    def all(self):
        return list(self._s)


class _App:
    def __init__(self, samplers):
        self.engines = _Engines(samplers)
        self.store = None
        self.cfg = {}


def _picked(app, engine_param):
    qs = {"engine": [engine_param]} if engine_param is not None else {}
    h = Handler.__new__(Handler)
    h.app = app
    return h._picked_sampler(qs)


def test_selection_is_honoured_when_named():
    a, b = _Sampler("a", "ModelA", "http://x:1"), _Sampler("b", "ModelB", "http://x:2")
    app = _App([a, b])
    assert _picked(app, "b") is b
    assert _picked(app, "a") is a


def test_falls_back_to_first_sampler_when_unspecified():
    """Single-engine installs must behave exactly as before this fix."""
    a, b = _Sampler("a", "ModelA", "http://x:1"), _Sampler("b", "ModelB", "http://x:2")
    app = _App([a, b])
    assert _picked(app, None) is a
    assert _picked(app, "") is a


def test_unknown_selection_does_not_raise():
    a = _Sampler("a", "ModelA", "http://x:1")
    app = _App([a])
    assert _picked(app, "nope") is a          # degrade to first, never crash
    assert _picked(_App([]), "a") is None     # nothing configured


def test_money_endpoints_do_not_pin_the_first_sampler():
    """Read the source of the handlers: the class of bug that survived review
    because the helper existed but the call sites kept using next(iter(...))."""
    import app.server as srv
    src = inspect.getsource(srv.Handler._api_get)
    # every remaining first-sampler pin inside the GET dispatch must be one of
    # the two places that legitimately has no selection context
    offenders = [ln.strip() for ln in src.splitlines()
                 if "next(iter(self.app.engines.all())" in ln]
    assert not offenders, (
        "GET endpoint resolves the first sampler again; it must use "
        f"_picked_sampler(qs). Offenders: {offenders}")


def test_client_names_the_engine_on_every_money_call():
    """Server-side selection is worthless if the browser never says which engine
    it is displaying -- that asymmetry was the original defect."""
    js = (pathlib.Path(__file__).resolve().parents[1] / "web" / "js" / "dashboard.js"
          ).read_text(encoding="utf-8")
    for ep in ("/api/counters", "/api/lost-potential", "/api/savings-history",
               "/api/session-tokens"):
        idx = js.find(f"fetch('{ep}")
        if idx < 0:
            idx = js.find(f'fetch("{ep}')
        assert idx >= 0, f"{ep} fetch not found"
        window = js[idx:idx + 160]
        assert "engine=" in window, f"{ep} fetched without naming the selected engine"
