"""HTTP server: stdlib ThreadingHTTPServer + a tiny router.

Routes
  GET  /                      dashboard (web/dashboard.html)
  GET  /api/health            {ok, version, uptime}
  GET  /api/snapshot          engines + hw latest + rings (cold-start paint)
  GET  /api/history?engine=&seconds=   sqlite downsample history
  GET  /api/events            SSE: topics engine:*, hw, requests, events
  POST /api/services/<id>/start|stop   whitelisted systemd/docker actions
  GET  /static/*              assets

SSE framing: `id:` per event so a dropped tab resumes with Last-Event-ID.
`?topics=engine:flashnext,hw` filters subscriptions client-side server-side.
"""
from __future__ import annotations

import json
import hmac
import logging
import mimetypes
import posixpath
import re
import threading
import time
import urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .sse import Broker

VERSION = "1.1.0"
_START_T = time.time()

# Panel settings — same spirit/keys as old LIMCARS dash_settings.json:
# server-side persisted, shared across all browsers.
SETTINGS_DEFAULTS = {
    "bubbles_main": True,       # the ordered main bubble strip
    "bubbles_live": True,       # (legacy keys kept harmless)
    "bubbles_latency": True,
    "bubbles_spec": True,
    "bubbles_savings": True,    # cost-savings bubbles
    "bubbles_potential": True,  # 'lost potential' wide bubble under savings
    "charts_infer": True,       # KV / prefill / decode chart row
    "charts_prom": True,        # TTFT / e2e / hit% chart row
    "chart_per_client": True,    # per-client context chart
    "chart_per_model": True,     # stats-by-model table
    "chart_savings_daily": True,  # daily money-saved chart (real usage)
    "requests_feed": True,      # recent completed requests table
    "refresh_s": 1.0,           # base UI refresh interval (0.5-30 s)
    "stats_scope": "all",       # all | active engine
    # theme + palette are NOT server settings: per-browser cookie (limcars_ui)
}


# probe result cache: key -> (monotonic_time, result, ttl_seconds)
_PROBE_LOCK = threading.Lock()
_PROBE_CACHE: dict[str, tuple[float, dict, float]] = {}


class _Settings:
    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self.lock = threading.Lock()
        self.data = dict(SETTINGS_DEFAULTS)
        try:
            self.data.update(json.loads(self.path.read_text()))
        except (OSError, json.JSONDecodeError):
            pass

    def all(self) -> dict:
        with self.lock:
            return dict(self.data)

    def update(self, patch: dict) -> None:
        with self.lock:
            self.data.update(patch)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            tmp.replace(self.path)


class Limcars:
    """Dependency container shared with handlers."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.broker: Broker = Broker()
        from .store import Store
        self.store = Store(cfg["db_path"], float(cfg.get("persist_seconds", 3600)))
        from .sampler import EngineManager
        self.engines = EngineManager(cfg, self.broker, self.store)
        from .hw import HwSampler
        self.hw = HwSampler(cfg, self.broker, self.store)
        self.hw.start()
        from .services import Services
        self.services = Services(cfg)
        from .recipes import Recipes
        self.recipes = Recipes(str(config.DATA))
        from .live_engines import LiveEngines
        self.live_engines = LiveEngines()
        self._savhist_cache: dict[int, tuple] = {}   # days -> (ts, payload)
        self._lostpot_cache: dict[str, tuple] = {}   # 'all' -> (ts, payload)
        from .clients import ClientSampler
        self.clients = ClientSampler(self.broker)
        self.clients.start()
        self.settings = _Settings(cfg.get("settings_path", "data/settings.json"))
        from .auth import AuthGate
        self.auth = AuthGate(cfg.get("auth_path") or str(config.DATA / "auth.json"))

    def settings_all(self) -> dict:
        return self.settings.all()

    def settings_update(self, patch: dict) -> None:
        self.settings.update(patch)


def _sse_format(topic: str, sid: int, data: str) -> bytes:
    return f"id: {topic}:{sid}\nevent: {topic}\ndata: {data}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    # audit: thread-per-connection with no socket timeout let slow-loris
    # sockets pin threads forever. 30 s per read; SSE disables it (write-only).
    timeout = 30
    server_version = f"limcars-next/{VERSION}"
    app: Limcars  # set on the class by serve()

    # ---------------- plumbing ----------------

    def log_message(self, format: str, *args) -> None:  # quieter default log
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    def _write(self, body: bytes) -> None:
        # HEAD must answer like GET but with no body. BaseHTTPRequestHandler
        # ships no do_HEAD, so HEAD used to fall through and get the HTML page
        # — which is how 'curl -I /js/dashboard.js' called a script text/html.
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _err(self, code: int, msg: str) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _authorized(self) -> bool:
        tok = self.app.cfg.get("auth_token")
        if not tok:
            return True
        given = self.headers.get("X-Limcars-Token") or \
            self.headers.get("Authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(given or "", tok)

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _pw_from_headers(self, default: str = "") -> str:
        """Read the shared password from headers. A duplicated X-Limcars-Pw
        header is never legitimate and is treated as absent (audit: harden
        against request-smuggling style header splits)."""
        hdrs = self.headers.get_all("X-Limcars-Pw") or []
        if len(hdrs) > 1:
            return default
        return (hdrs[0] if hdrs else "") or self.headers.get(
            "Authorization", "").removeprefix("Bearer ") or default

    def _is_local(self) -> bool:
        return self._client_ip() in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _require_write(self) -> bool:
        """Gate recipe add/edit/delete: password + escalating per-IP lockout.
        Localhost is exempt (the box itself is the admin domain). Sends the
        401/423 response itself; returns True when the request may proceed."""
        if self._is_local() or not self.app.auth.enabled:
            return True
        ip = self._client_ip()
        pw = self._pw_from_headers()
        # single atomic gate (lockout check + verify + ladder in one lock hold)
        ok, rem = self.app.auth.attempt(ip, pw)
        if ok:
            return True
        if rem > 0:
            self._json({"ok": False, "error": "locked",
                        "retry_after": round(rem, 1)}, code=423)
        else:
            self._json({"ok": False, "error": "wrong password"}, code=401)
        return False

    def _require_read_traces(self, qs: dict) -> bool:
        """Gate Hermes Traces data endpoints (transcripts are private).
        Same password + lockout ladder as recipe edits; localhost exempt.
        The vendored UI can't set headers from an iframe URL, so ?pw= is
        accepted too and the parent dashboard forwards it."""
        if self._is_local() or not self.app.auth.enabled:
            return True
        ip = self._client_ip()
        pw = self._pw_from_headers((qs.get("pw") or [""])[0])
        ok, rem = self.app.auth.attempt(ip, pw)
        if ok:
            return True
        if rem > 0:
            self._json({"ok": False, "error": "locked",
                        "retry_after": round(rem, 1)}, code=423)
        else:
            self._json({"ok": False, "error": "wrong password"}, code=401)
        return False

    # ---------------- dispatch ----------------

    def do_HEAD(self) -> None:  # noqa: N802
        # monitoring, link checkers and curl -I all ask; answer like GET
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path.startswith("/api/"):
                if not self._authorized():
                    return self._err(401, "unauthorized")
                return self._api_get(path, qs)
            if path.startswith("/subagent-watch"):
                # static shell is harmless; transcript DATA needs the password
                if path not in ("/subagent-watch", "/subagent-watch/",
                                "/subagent-watch/index.html",
                                "/subagent-watch/app.js",
                                "/subagent-watch/style.css",
                                "/subagent-watch/favicon.ico"):
                    if not self._require_read_traces(qs):
                        return
                return self._swatch(path, qs)
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            # audit: never echo exception text to remote clients (internals
            # leak); the full traceback goes to the server log only
            logging.getLogger("limcars").exception("GET %s failed",
                                                  self.path.split("?", 1)[0])
            try:
                self._err(500, "internal error")
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:  # noqa: N802
        # audit: an unhandled exception in a POST branch used to escape to
        # BaseHTTPRequestHandler (dropped connection, no response) — every
        # client now gets a clean 500 and the traceback goes to the log
        # audit: DNS-rebinding / cross-site forms would arrive with peer
        # 127.0.0.1 and ride the localhost exemption straight past the recipe
        # gate. A browser always sends Origin on cross-site POSTs — reject
        # any whose host is not the host the client asked for. (CLI/agents
        # send no Origin and are unaffected.)
        org = self.headers.get("Origin")
        if org:
            try:
                oh = urllib.parse.urlparse(org).hostname or ""
            except ValueError:
                oh = "?"
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if oh and host and oh not in ("localhost", "127.0.0.1", "::1") \
                    and oh != host:
                return self._err(403, "cross-origin write refused")
        try:
            self._dispatch_post()
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            logging.getLogger("limcars").exception("POST %s failed",
                                                  self.path.split("?", 1)[0])
            try:
                self._err(500, "internal error")
            except Exception:  # noqa: BLE001
                pass

    def _dispatch_post(self) -> None:
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._err(404, "not found")
        if not self._authorized():
            return self._err(401, "unauthorized")
        m = re.fullmatch(r"/api/auth/set/?", u.path)
        if m:
            body = self._body_json()
            if body is None:
                return
            new = (body.get("new") or "").strip()
            cur = body.get("current")
            gate = self.app.auth
            if gate.enabled and not self._is_local():
                # changing an existing password over the network proves
                # knowledge of it first (atomic lockout ladder via attempt())
                ok, rem = gate.attempt(self._client_ip(), cur or "")
                if rem > 0:
                    return self._json({"ok": False, "error": "locked",
                                       "retry_after": round(rem, 1)}, code=423)
                if not ok:
                    return self._json({"ok": False, "error": "wrong password"},
                                      code=401)
            return self._json({"ok": gate.set_password(new or None, cur)})
        # Password gate: recipe CONTENT only (add/edit/delete). Start/Stop,
        # engine stop, services, settings, pricing stay open for convenience
        # — commands can only ever enter the system through these two doors.
        if re.fullmatch(r"/api/recipes/?", u.path) or \
                re.fullmatch(r"/api/recipes/[\w-]+/(update|delete)", u.path):
            if not self._require_write():
                return
        m = re.fullmatch(r"/api/services/([\w-]+)/(start|stop)", u.path)
        if m:
            out = self.app.services.action(m.group(1), m.group(2))
            return self._json(out)
        m = re.fullmatch(r"/api/recipes/?", u.path)
        if m:
            body = self._body_json()
            if body is None:
                return
            return self._json(self.app.recipes.add(body.get("name"), body.get("cmd")))
        m = re.fullmatch(r"/api/recipes/([\w-]+)/(update|delete|start|stop)", u.path)
        if m:
            rid, act = m.group(1), m.group(2)
            if act == "update":
                body = self._body_json()
                if body is None:
                    return
                return self._json(self.app.recipes.update(
                    rid, name=body.get("name"), cmd=body.get("cmd")))
            if act == "delete":
                return self._json(self.app.recipes.delete(rid))
            return self._json(getattr(self.app.recipes, act)(rid))
        m = re.fullmatch(r"/api/engines-live/(\d+)/stop", u.path)
        if m:
            return self._json(self.app.live_engines.stop(int(m.group(1))))
        m = re.fullmatch(r"/api/probe/?", u.path)
        if m:
            from . import probe as probe_mod
            smp = next(iter(self.app.engines.all()), None)
            if not smp:
                return self._err(400, "no engine configured")
            # A probe is a real 1300-word generation on the engine. Cache the
            # result (success 5 min / failure 30 s) so refresh loops and
            # multiple tabs can't hammer the box (audit: no budget existed).
            key = f"{smp.url}|{smp.model}"
            now = time.time()
            with _PROBE_LOCK:
                hit = _PROBE_CACHE.get(key)
                if hit and now - hit[0] < hit[2]:
                    return self._json(hit[1])
            try:
                res = probe_mod.probe(smp.url, smp.model)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)[:300]}
            with _PROBE_LOCK:
                _PROBE_CACHE[key] = (now, res, 300 if res.get("ok") else 30)
            return self._json(res)
        m = re.fullmatch(r"/api/config/?", u.path)
        if m:
            return self._config_patch()
        m = re.fullmatch(r"/api/settings/?", u.path)
        if m:
            return self._settings_patch()
        return self._err(404, "not found")

    _CONFIG_ALLOWED = {"pricing", "window_s", "live_hz", "hw_hz"}
    SETTING_DEFAULTS = SETTINGS_DEFAULTS

    def _body_json(self) -> dict | None:
        """Read+parse a JSON POST body; sends 400 and returns None on failure."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            # audit: unbounded body read = one-POST OOM of the whole monitor
            if n < 0 or n > 1_000_000:
                self._err(413, "body too large")
                return None
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._err(400, "bad json")
            return None
        if not isinstance(body, dict):
            self._err(400, "json object expected")
            return None
        return body

    def _config_patch(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 1_000_000:  # audit: bypassed the _body_json cap
                return self._err(400, "bad body size")
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._err(400, "bad json")
        if not isinstance(body, dict) or not (set(body) <= self._CONFIG_ALLOWED):
            return self._err(400, f"only {sorted(self._CONFIG_ALLOWED)} are editable")
        # audit: keys were whitelisted, VALUES were not — pricing:"x" poisoned
        # every $ endpoint and tripped the sampler's fake engine_down path;
        # window_s:"x" persisted and crashed boot on the next restart.
        if "pricing" in body and body["pricing"] is not None:
            pv = body["pricing"]
            if not isinstance(pv, dict) or any(
                    isinstance(v, bool) or not isinstance(v, (int, float))
                    for v in pv.values()):
                return self._err(400, "pricing must be a dict of numbers")
        for k in ("window_s", "live_hz", "hw_hz"):
            if k in body:
                try:
                    v = float(body[k])
                except (TypeError, ValueError):
                    return self._err(400, f"{k} must be a number")
                lo, hi = (1.0, 86400.0) if k == "window_s" else (0.05, 20.0)
                if not lo <= v <= hi:
                    return self._err(400, f"{k} must be within {lo}..{hi}")
        app = self.app
        if "live_hz" in body:
            body["live_hz"] = min(4.0, max(1.0, float(body["live_hz"])))
        app.cfg.update(body)
        # persist to data/config.json (merge)
        from . import config as cfg_mod
        f = cfg_mod.DATA / "config.json"
        disk = {}
        if f.exists():
            try:
                disk = json.loads(f.read_text())
            except ValueError:
                disk = {}
        disk.update({k: v for k, v in body.items() if k in self._CONFIG_ALLOWED})
        f.write_text(json.dumps(disk, indent=2))
        self._json({"ok": True, "config": {k: app.cfg.get(k) for k in body}})

    # ---------------- API ----------------

    def _api_get(self, path: str, qs: dict) -> None:
        app = self.app
        if path == "/api/health":
            return self._json({"ok": True, "version": VERSION,
                               "uptime_s": round(time.time() - _START_T, 1)})
        if path == "/api/auth/status":
            return self._json({"enabled": app.auth.enabled,
                               "locked_for_you": round(app.auth.locked_for(self._client_ip()), 1)})
        if path == "/api/snapshot":
            secs = (qs.get("seconds") or [None])[0]
            try:
                secs = float(secs) if secs else None
            except (TypeError, ValueError):
                secs = None
            if secs is not None:
                # audit: seconds=1e18 scanned the whole samples table under
                # the store lock (sampler stall + OOM per request)
                import math as _m
                if not _m.isfinite(secs) or secs <= 0:
                    secs = None
                else:
                    secs = min(secs, float(self.app.cfg.get(
                        "persist_seconds", 3600)) + 300)
            eng_snap = app.engines.snapshots(secs)
            _model = next(iter(eng_snap.values()), {}).get("model")
            from . import pricing as _pr
            return self._json({
                "ok": True, "version": VERSION,
                "ts": time.time(), "uptime_s": round(time.time() - _START_T, 1),
                "pricing": self.app.cfg.get("pricing") or _pr.resolve(_model),
                "engines": eng_snap,
                "hw": app.hw.snapshot(secs),
                "services": app.services.status_all(),
            })
        if path == "/api/history":
            eng = (qs.get("engine") or [None])[0]
            secs = float((qs.get("seconds") or ["900"])[0])
            if eng:
                return self._json({"ok": True, "rows": app.store.history("engine_samples", eng, secs)})
            return self._json({"ok": True, "rows": app.store.history("hw_samples", None, secs)})
        if path == "/api/events":
            return self._sse(qs)
        if path == "/api/savings-history":
            # daily money-saved chart data (real persisted usage, per model)
            from . import counters as counters_mod
            try:
                days = int((qs.get("days") or ["30"])[0])
            except ValueError:
                return self._err(400, "bad days")
            if days not in (7, 30, 92, 365):
                days = 30
            # 60 s TTL cache: the chart re-renders every 60 s per open tab;
            # day buckets move at most once per minute anyway (audit F4)
            cache = getattr(app, "_savhist_cache", None)
            if cache is None:
                cache = app._savhist_cache = {}
            hit = cache.get(days)
            now = time.time()
            if hit and now - hit[0] < 60.0:
                return self._json(hit[1])
            engines = [{"id": s.id, "model": s.model} for s in app.engines.all()]
            payload = {"ok": True, **counters_mod.daily_savings(
                app.store, engines, app.cfg.get("pricing"), days)}
            cache[days] = (now, payload)
            return self._json(payload)
        if path == "/api/lost-potential":
            # 'lost potential' bubble: peak day/month/year vs recorded averages
            from . import counters as counters_mod
            cache = getattr(app, "_lostpot_cache", None)
            if cache is None:
                cache = app._lostpot_cache = {}
            hit = cache.get("all")
            now = time.time()
            if hit and now - hit[0] < 60.0:
                return self._json(hit[1])
            engines = [{"id": s.id, "model": s.model, "alive": bool(s.alive)}
                       for s in app.engines.all()]
            # machine occupancy: only set this in config to PIN the hours/day;
            # left unset, the dash uses the hours a day it actually measured
            duty = app.cfg.get("duty_hours_per_day")
            payload = counters_mod.lost_potential(
                app.store, engines, app.cfg.get("pricing"), duty_hours=duty)
            cache["all"] = (now, payload)
            return self._json(payload)
        m = re.fullmatch(r"/api/counters/?", path)
        if m:
            from . import counters as counters_mod
            smp = next(iter(self.app.engines.all()), None)
            if not smp or not self.app.store:
                return self._json({"empty": True})
            win = (qs.get("window") or ["all"])[0]
            return self._json(counters_mod.report(
                self.app.store, smp.id, win, smp.model,
                override=self.app.cfg.get("pricing")))
        m = re.fullmatch(r"/api/engine-info/?", path)
        if m:
            from . import engine_info
            smp = next(iter(self.app.engines.all()), None)
            if not smp:
                return self._json({"healthy": False, "error": "no engine"})
            return self._json(engine_info.info(smp.url))
        m = re.fullmatch(r"/api/settings/?", path)
        if m:
            return self._json(self.app.settings_all())
        m = re.fullmatch(r"/api/session-tokens/?", path)
        if m:
            from . import session_tokens
            smp = next(iter(self.app.engines.all()), None)
            want = (qs.get("model") or [""])[0] or (smp.model if smp else None)
            return self._json(session_tokens.today_sessions(want))
        m = re.fullmatch(r"/api/clients/?", path)
        if m:
            win = min(86400.0, max(60.0, float((qs.get("window_s") or ["1800"])[0])))
            return self._json(self.app.clients.window(win))
        m = re.fullmatch(r"/api/recipes/?", path)
        if m:
            return self._json({"recipes": self.app.recipes.list()})
        m = re.fullmatch(r"/api/engines-live/?", path)
        if m:
            return self._json({"engines": self.app.live_engines.scan(
                force=(qs.get("refresh") == ["1"])), "at": time.time()})
        return self._err(404, "not found")

    # ---------------- panel settings (old dash_settings.json parity) ----------------

    def _settings_patch(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 1_000_000:  # audit: bypassed the _body_json cap
                return self._err(400, "bad body size")
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._err(400, "bad json")
        if not isinstance(body, dict):
            return self._err(400, "object required")
        allowed = set(self.SETTING_DEFAULTS)
        if not (set(body) <= allowed):
            return self._err(400, f"only {sorted(allowed)} are editable")
        for k in ("refresh_s",):
            if k in body:
                body[k] = min(30.0, max(0.5, float(body[k])))
        if "stats_scope" in body and body["stats_scope"] not in ("all", "active"):
            return self._err(400, "stats_scope: all|active")
        # audit: settings must stay JSON scalars — nested values would crash
        # the store's sort keys / sparklines with 500s for every reader
        for k, v in body.items():
            if v is not None and not isinstance(v, (str, int, float, bool)):
                return self._err(400, f"{k}: scalar value expected")
        self.app.settings_update(body)
        self._json({"ok": True, "settings": self.app.settings_all()})

    def _sse(self, qs: dict) -> None:
        if getattr(self, "_head_only", False):
            # HEAD on the event stream must not block waiting for events
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.timeout = None  # audit: the event stream only writes
        topics_wanted = (qs.get("topics") or [""])[0].split(",")
        wanted = {t for t in topics_wanted if t}
        # audit: never create broker topics from client input — only names that
        # are legitimately part of this deployment. The old test compared against
        # topics that had ALREADY been published to, so on a fresh start or with
        # no engine connected 'requests' and 'events' did not exist yet, the
        # browser's own topic list was rejected, and the page retried a 400 every
        # 2 s forever (user: "the web ui keeps refreshing itself very often").
        import re as _re
        known = set(self.app.broker._topics.keys())
        core = {"hw", "requests", "events"}
        allowed = known | core | {f"engine:{s.id}" for s in self.app.engines.all()}
        bad = [t for t in wanted
               if not _re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", t) or t not in allowed]
        if len(wanted) > 32 or bad:
            return self._err(400, "bad topics")
        # Subscribe to what the broker already carries; a valid topic that has
        # simply not published yet is created on subscribe rather than punished.
        wanted = {t for t in wanted if t in known} or (wanted & allowed)
        last = self.headers.get("Last-Event-ID")  # "topic:seq"
        since: dict[str, int] = {}
        if last:
            t_, _, s_ = last.partition(":")
            try:
                since[t_] = int(s_)
            except ValueError:
                pass
        app = self.app
        subs = []
        try:
            for name in wanted:
                topic = app.broker.topic(name)
                q, backlog = topic.subscribe(since.get(name))
                subs.append((name, topic, q, backlog))
        except RuntimeError as exc:            # MAX_SUBS reached
            for item in subs:
                item[1].unsubscribe(item[2])
            return self._err(503, str(exc))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        for name, topic, q, backlog in subs:
            for sid, data in backlog:
                self.wfile.write(_sse_format(name, sid, data))
        self.wfile.flush()
        idle = 0.0
        try:
            while True:
                got = False
                for name, topic, q, _bl in subs:
                    try:
                        while True:
                            sid, data = q.get_nowait()
                            self.wfile.write(_sse_format(name, sid, data))
                            got = True
                    except Exception:  # noqa: BLE001 — queue.Empty
                        pass
                if got:
                    idle = 0.0
                    self.wfile.flush()
                else:
                    time.sleep(0.1)
                    idle += 0.1
                    if idle > 15:  # keepalive comment defeats proxy idle timeouts
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        idle = 0.0
        finally:
            for _, topic, q, _bl in subs:
                topic.unsubscribe(q)

    # ---------------- static ----------------

    # ---------------- Hermes Traces (/subagent-watch/*) ----------------

    def _swatch(self, path: str, qs: dict) -> None:
        from . import swatch
        m = re.fullmatch(r"/subagent-watch/batches/?", path)
        if m:
            return self._json(swatch.batches())
        m = re.fullmatch(r"/subagent-watch/main-sessions/?", path)
        if m:
            return self._json(swatch.main_sessions())
        m = re.fullmatch(r"/subagent-watch/manifest/([\w.-]+)/?", path)
        if m:
            r = swatch.manifest(m.group(1))
            return self._json(r) if r is not None else self._err(404, "no batch")
        m = re.fullmatch(r"/subagent-watch/main-transcript/([\w.-]+)/([\w.-]+)/?", path)
        if m:
            limit = int((qs.get("limit") or ["200"])[0])
            r = swatch.main_transcript(m.group(1), m.group(2), limit)
            return self._json(r) if r else self._err(404, "no session")
        m = re.fullmatch(r"/subagent-watch/transcript/([\w.-]+)/([\w.-]+)/?", path)
        if m:
            return self._serve_text(swatch.transcript_file(m.group(1), m.group(2)))
        m = re.fullmatch(r"/subagent-watch/summary/([\w.-]+)/?", path)
        if m:
            return self._serve_text(swatch.summary_file(m.group(1)))
        return self._static(path)   # page + assets

    def _serve_text(self, f) -> None:
        """text/plain with HTTP Range (bytes=-N) — the trace UI's tail poll."""
        from . import swatch as _sw
        if f is None:
            return self._err(404, "not found")
        try:
            size = f.stat().st_size
            data = f.read_bytes()
        except OSError:
            return self._err(404, "not found")
        try:
            rng = _sw.parse_range(self.headers.get("Range"), size)
        except Exception:  # noqa: BLE001
            rng = None
        if rng:
            start, end = rng
            body = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(body)

    def _static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/dashboard.html"
        if path in ("/subagent-watch", "/subagent-watch/"):
            path = "/subagent-watch/index.html"
        safe = posixpath.normpath(path).lstrip("/")
        if safe.startswith(".."):
            return self._err(403, "forbidden")
        f = (config.WEB / safe).resolve()
        # audit: normpath alone still followed a symlink placed under web/
        if not str(f).startswith(str(config.WEB.resolve()) + "/"):
            return self._err(403, "forbidden")
        if not f.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # no-store: dashboards get iterated constantly; a tab resurrected
        # from bfcache running yesterday's JS caused stale prompt() flows
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self._write(data)


def serve(cfg: dict) -> None:
    app = Limcars(cfg)
    Handler.app = app
    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    httpd.daemon_threads = True
    print(f"[limcars-next] serving on http://{cfg['host']}:{cfg['port']} "
          f"(engines: {[s.id for s in app.engines.all()]})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in app.engines.all():
            s.stop()
        app.hw.stop()
